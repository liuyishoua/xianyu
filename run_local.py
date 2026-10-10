"""Local checks and QR login. Does not send messages or publish items."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / '.local'


def smoke():
    from goofish_apis import XianyuApis
    from goofish_live import XianyuLive
    from message.types import make_text, make_image, message_adapter
    from utils.goofish_utils import generate_sign, generate_device_id, trans_cookies

    expected = hashlib.md5(b'test&1700000000000&34839810&{}').hexdigest()
    assert generate_sign('1700000000000', 'test', '{}') == expected
    assert trans_cookies('unb=123;_m_h5_tk=a=b; ') == {'unb': '123', '_m_h5_tk': 'a=b'}
    assert generate_device_id('123').endswith('-123')

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
        async def send(self, frame):
            self.sent.append(json.loads(frame))

    async def check_frames():
        import base64
        live = XianyuLive('unb=123; _m_h5_tk=test_0')
        ws = FakeWebSocket()
        for content in [make_text('本地验证'), make_image('https://example.com/image.png', 10, 20)]:
            message_adapter.validate_python(content)
            await live.send_msg(ws, 'test-cid', '456', content)
        assert len(ws.sent) == 2
        custom = ws.sent[0]['body'][0]['content']['custom']
        assert json.loads(base64.b64decode(custom['data']))['text']['text'] == '本地验证'
        assert ws.sent[1]['body'][0]['content']['custom']['type'] == 2
        assert ws.sent[0]['body'][1]['actualReceivers'] == ['456@goofish', '123@goofish']

    asyncio.run(check_frames())
    print('PASS: imports, Node signature, device ID, cookies, text/image frames (fake WebSocket only)')


def probe():
    import requests
    targets = {
        'web': 'https://www.goofish.com/',
        'passport': 'https://passport.goofish.com/mini_login.htm?appName=xianyu',
        'mtop': 'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/',
    }
    ok = True
    for name, url in targets.items():
        try:
            r = requests.get(url, timeout=15)
            print(f'{name}: HTTP {r.status_code}, content-type={r.headers.get("Content-Type", "")}')
            ok = ok and r.ok
        except requests.RequestException as exc:
            print(f'{name}: {type(exc).__name__}')
            ok = False
    return 0 if ok else 1


def login(timeout):
    import qrcode
    from goofish_apis import qrcode_login
    LOCAL.mkdir(mode=0o700, exist_ok=True)

    def display(url):
        path = LOCAL / 'login-qr.png'
        qrcode.make(url).save(path)
        print(f'QR_IMAGE={path}', flush=True)

    api = qrcode_login(timeout=timeout, show_qrcode=False, on_qrcode=display)
    cookies = [dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                    secure=c.secure, expires=c.expires) for c in api.session.cookies]
    if not any(c['name'] == 'unb' and c['value'] for c in cookies):
        raise RuntimeError('No logged-in user cookie returned')
    target = LOCAL / 'session.json'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'device_id': api.device_id, 'cookies': cookies}, f)
    print('Session saved locally with mode 600; checking IM token (no messaging).', flush=True)
    return check(None)


def load_api():
    import requests
    from goofish_apis import XianyuApis
    data = json.loads((LOCAL / 'session.json').read_text())
    api = XianyuApis({}, data['device_id'])
    for c in data['cookies']:
        api.session.cookies.set_cookie(requests.cookies.create_cookie(**c))
    # Bound each HTTP request; upstream methods have no requests timeout.
    original = api.session.request
    request_count = 0
    def bounded(method, url, **kwargs):
        nonlocal request_count
        request_count += 1
        if request_count > 6:
            raise RuntimeError('Upstream token refresh exceeded local retry limit')
        kwargs.setdefault('timeout', 20)
        return original(method, url, **kwargs)
    api.session.request = bounded
    return api


def check(item_id):
    api = load_api()
    result = api.get_token()
    token_ok = bool(result.get('data', {}).get('accessToken'))
    print(json.dumps({'im_token_ok': token_ok, 'ret': result.get('ret')}, ensure_ascii=False))
    if item_id:
        result = api.get_item_info(item_id)
        print(json.dumps({'item_id': item_id, 'ret': result.get('ret'),
                          'data_keys': list(result.get('data', {}).keys())}, ensure_ascii=False))
    return 0 if token_ok else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('smoke', help='Offline import/signature/message encoding checks')
    sub.add_parser('probe', help='Unauthenticated HTTP connectivity check')
    lp = sub.add_parser('login', help='QR login and save session locally; no replies/publishing')
    lp.add_argument('--timeout', type=float, default=120)
    cp = sub.add_parser('check', help='Read-only IM token check with saved session')
    cp.add_argument('--item-id')
    args = p.parse_args()
    try:
        if args.command == 'smoke':
            smoke()
            return 0
        if args.command == 'probe':
            return probe()
        if args.command == 'login':
            return login(args.timeout)
        return check(args.item_id)
    except Exception as exc:
        # Third-party HTTP exceptions may contain login tokens in URLs.
        print(f'FAILED: {type(exc).__name__}; credentials/response body omitted', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
