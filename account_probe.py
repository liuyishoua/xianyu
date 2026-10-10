"""Read-only account capability checks. Never invokes automatic reply handlers."""
import argparse
import asyncio
import base64
import json
import re
import time

from run_local import load_api, LOCAL
from goofish_live import XianyuLive
from utils.goofish_utils import generate_mid, get_session_cookies_str, decrypt
import websockets


def summary(result):
    data = result.get('data', {})
    return {'ret': result.get('ret'), 'data_keys': list(data) if isinstance(data, dict) else []}


async def check_ws(api, token_response, cid=None):
    live = XianyuLive(get_session_cookies_str(api.session))
    live.device_id = api.device_id
    live.xianyu = api
    report = {'connected': False, 'registered': False, 'push_frames': 0,
              'decoded_sync_entries': 0, 'history': 'not tested: no conversation ID'}
    # Reuse the token already obtained instead of making blocking HTTP requests in init().
    original = api.get_token
    api.get_token = lambda: token_response
    headers = {'Cookie': get_session_cookies_str(api.session),
               'User-Agent': 'Mozilla/5.0', 'Origin': 'https://www.goofish.com'}
    deadline = time.monotonic() + 25
    history_mid = None
    try:
        async with websockets.connect(live.base_url, extra_headers=headers, open_timeout=15) as ws:
            report['connected'] = True
            registration_mid = None
            class InitSocket:
                async def send(self, frame):
                    nonlocal registration_mid
                    request = json.loads(frame)
                    if request.get('lwp') == '/reg':
                        registration_mid = request['headers']['mid']
                    await ws.send(frame)
            await live.init(InitSocket())
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), min(8, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                lwp = msg.get('lwp', '')
                head = msg.get('headers', {})
                if lwp == '/reg' or head.get('mid') == registration_mid:
                    report['registration_code'] = msg.get('code')
                    report['registered'] = msg.get('code') == 200
                if lwp == '/s/vulcan':
                    report['sync_ready'] = True
                body = msg.get('body', {})
                if isinstance(body, dict):
                    push = body.get('syncPushPackage', {})
                    for entry in push.get('data', []):
                        report['push_frames'] += 1
                        report.setdefault('push_schemas', []).append(
                            {k: type(v).__name__ for k, v in entry.items()})
                        try:
                            encoded = entry.get('data', '')
                            try:
                                decoded = json.loads(encoded)
                            except (ValueError, TypeError):
                                try:
                                    decoded = json.loads(base64.b64decode(encoded))
                                except (ValueError, UnicodeError):
                                    decoded = json.loads(decrypt(encoded))
                            report['decoded_sync_entries'] += 1
                            if isinstance(decoded, dict):
                                report.setdefault('decoded_shapes', []).append({
                                    k: (list(v.keys()) if isinstance(v, dict) else type(v).__name__)
                                    for k, v in decoded.items()})
                            candidate = decoded.get('1', {}).get('2', '')
                            candidate = candidate or decoded.get('sessionId', '')
                            if not cid and isinstance(candidate, str) and re.fullmatch(r'\d+(?:@goofish)?', candidate):
                                cid = candidate.split('@')[0]
                                report['conversation_id_found'] = True
                            operation = decoded.get('operation', {})
                            if isinstance(operation, dict) and isinstance(operation.get('content'), dict):
                                report.setdefault('content_shapes', []).append(list(operation['content']))
                        except Exception as exc:
                            detail = {'type': type(exc).__name__, 'biz_type': entry.get('bizType'),
                                      'object_type': entry.get('objectType')}
                            for known in ('Invalid character', 'Unexpected end', 'Data read',
                                          'Invalid property', 'ReferenceError', 'Unknown MessagePack'):
                                if known in str(exc):
                                    detail['reason'] = known
                            try:
                                wire = base64.b64decode(encoded)
                                detail['wire_prefix_hex'] = wire[:4].hex()
                                detail['wire_size'] = len(wire)
                            except Exception:
                                pass
                            report.setdefault('decode_errors', []).append(detail)
                if lwp.startswith('/s/'):
                    ack = {'code': 200, 'headers': {k: head[k] for k in ('mid', 'sid', 'app-key', 'ua', 'dt') if k in head}}
                    await ws.send(json.dumps(ack))
                if cid and not history_mid and report.get('sync_ready'):
                    history_mid = generate_mid()
                    await ws.send(json.dumps({'lwp': '/r/MessageManager/listUserMessages',
                        'headers': {'mid': history_mid},
                        'body': [f'{cid}@goofish', False, 9007199254740991, 20, False]}))
                    report['history'] = 'requested'
                if history_mid and head.get('mid') == history_mid:
                    report['history'] = {'code': msg.get('code'),
                        'count': len(body.get('userMessageModels', [])), 'has_more': body.get('hasMore'),
                        'data_keys': list(body)}
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    finally:
        api.get_token = original
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--item-id')
    p.add_argument('--cid')
    args = p.parse_args()
    report = {}
    try:
        api = load_api()
        token = api.get_token()
        report['im_token'] = summary(token)
        report['im_token']['ok'] = bool(token.get('data', {}).get('accessToken'))
        for name, operation in [('login_user', api.refresh_token),
                                ('location', api.get_default_location),
                                ('category_recommendation', lambda: api.get_public_channel('二手图书', []))]:
            try:
                report[name] = summary(operation())
            except Exception as exc:
                report[name] = {'error_type': type(exc).__name__}
        if args.item_id:
            try:
                report['item_detail'] = summary(api.get_item_info(args.item_id))
            except Exception as exc:
                report['item_detail'] = {'error_type': type(exc).__name__}
        if report['im_token']['ok']:
            report['websocket'] = asyncio.run(check_ws(api, token, args.cid))
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    print(json.dumps(report, ensure_ascii=False, indent=2))
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    (LOCAL / 'capabilities.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
