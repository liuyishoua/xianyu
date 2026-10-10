"""Execute one explicitly specified account action; never auto-retry a write.

Usage: .venv/bin/python account_action.py /absolute/path/to/action.json
The JSON must specify action=send or action=publish and the required fields.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from run_local import load_api, LOCAL


def save_result(name, result):
    fd = os.open(LOCAL / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(result, f, ensure_ascii=False)


class CapturingSocket:
    def __init__(self, ws):
        self.ws = ws
        self.mid = None
    async def send(self, frame):
        self.mid = json.loads(frame)['headers']['mid']
        await self.ws.send(frame)


async def ack(ws, mid, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = json.loads(await asyncio.wait_for(ws.recv(), deadline-time.monotonic()))
        headers = result.get('headers', {})
        if result.get('lwp', '').startswith('/s/'):
            await ws.send(json.dumps({'code': 200, 'headers': {
                k: headers[k] for k in ('mid', 'sid', 'app-key', 'ua', 'dt') if k in headers}}))
        if headers.get('mid') == mid:
            save_result('last-action-ack.json', result)
            if result.get('code') != 200:
                raise RuntimeError(f'Action rejected: code={result.get("code")}')
            return result
    raise TimeoutError('No correlated acknowledgement; do not retry automatically')


async def send(api, config):
    import websockets
    from goofish_live import XianyuLive
    from utils.goofish_utils import get_session_cookies_str
    from message import make_text
    toid, text = str(config['toid']), config['text']
    if not toid.isdigit() or not isinstance(text, str) or not text.strip():
        raise ValueError('Explicit numeric recipient and nonempty text are required')
    if not config.get('cid') and not str(config.get('item_id', '')).isdigit():
        raise ValueError('An existing cid or the recipient item_id is required')
    live = XianyuLive(get_session_cookies_str(api.session))
    live.device_id = api.device_id
    live.xianyu = api
    token_result = api.get_token()
    if not token_result.get('data', {}).get('accessToken'):
        raise RuntimeError('IM token unavailable')
    api.get_token = lambda: token_result
    async with websockets.connect(live.base_url, extra_headers={
        'Cookie': get_session_cookies_str(api.session), 'Origin': 'https://www.goofish.com',
        'User-Agent': 'Mozilla/5.0'}, open_timeout=15) as ws:
        capture = CapturingSocket(ws)
        # init sends /reg first and then sync. Capture registration separately.
        registration = None
        original_send = capture.send
        async def init_send(frame):
            nonlocal registration
            if json.loads(frame).get('lwp') == '/reg':
                registration = json.loads(frame)['headers']['mid']
            await original_send(frame)
        capture.send = init_send
        await live.init(capture)
        await ack(ws, registration)
        capture.send = original_send
        cid = config.get('cid')
        if not cid:
            await live.create_chat(capture, toid, str(config['item_id']))
            result = await ack(ws, capture.mid)
            body = result.get('body', {})
            if isinstance(body, dict):
                cid = body.get('cid') or body.get('conversationId')
            if not cid:
                print(json.dumps({'chat_created_ack': True, 'body_keys': list(body) if isinstance(body, dict) else [],
                                  'message_sent': False}, ensure_ascii=False))
                return 1
        cid = str(cid).split('@')[0]
        await live.send_msg(capture, cid, toid, make_text(text))
        await ack(ws, capture.mid)
        print(json.dumps({'send_ack': 200, 'recipient': toid, 'cid': cid,
                          'note': 'Server accepted; recipient reading is not verified'}, ensure_ascii=False))
        return 0


def publish(api, config):
    from decimal import Decimal
    paths = config['images_path']
    desc, price, delivery, location = (config[k] for k in ('goods_desc', 'price', 'delivery', 'location'))
    if not isinstance(desc, str) or not desc.strip() or not paths:
        raise ValueError('Description and real product images are required')
    for path in paths:
        if not Path(path).is_file():
            raise ValueError('Image file does not exist')
    if Decimal(str(price['current_price'])) <= 0:
        raise ValueError('A positive explicit price is required')
    price.setdefault('original_price', 0)
    delivery.setdefault('can_self_pickup', False)
    if delivery['choice'] == '一口价' and Decimal(str(delivery['post_price'])) < 0:
        raise ValueError('Postage cannot be negative')
    for key in ('area', 'city', 'divisionId', 'longitude', 'latitude', 'poiId', 'poi', 'prov'):
        if key not in location:
            raise ValueError(f'Explicit location missing {key}')
    # Override the upstream fixed coordinates and automatic first-address selection.
    api.get_default_location = lambda: {'data': {'commonAddresses': [location]}}
    result = api.public(paths, desc, price, delivery)
    save_result('last-publish-result.json', result)
    data = result.get('data', {})
    print(json.dumps({'ret': result.get('ret'), 'data_keys': list(data) if isinstance(data, dict) else [],
                      'item_id': data.get('itemId') if isinstance(data, dict) else None}, ensure_ascii=False))
    return 0 if any(str(x).startswith('SUCCESS') for x in result.get('ret', [])) else 1


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    if config['action'] not in ('send', 'publish'):
        raise ValueError('Unknown action')
    api = load_api()
    if config['action'] == 'send':
        return asyncio.run(send(api, config))
    return publish(api, config)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'FAILED: {type(exc).__name__}; no automatic retry. If a write timed out, verify remotely before retrying.',
              file=sys.stderr)
        sys.exit(1)
