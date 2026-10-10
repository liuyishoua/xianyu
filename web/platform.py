"""Bounded adapters over the existing HTTP and WebSocket implementations."""
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests
import websockets
from goofish_apis import XianyuApis, qrcode_login
from goofish_live import XianyuLive
from message import make_text, make_image
from utils.goofish_utils import generate_mid, get_session_cookies_str, decrypt


class PlatformError(Exception):
    pass


class UncertainError(Exception):
    pass


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, ensure_ascii=False)
    os.replace(temporary, path)


def save_session(path, api):
    private_json(path, {'device_id': api.device_id, 'cookies': [
        dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
             secure=c.secure, expires=c.expires) for c in api.session.cookies]})


def succeeded(data):
    if not any(str(x).startswith('SUCCESS') for x in data.get('ret', [])):
        codes = [str(x).split('::')[0] for x in data.get('ret', [])]
        safe = ', '.join(x for x in codes if re.fullmatch(r'[A-Z_0-9: -]{1,100}', x))
        raise PlatformError('闲鱼接口未确认操作' + ('：' + safe if safe else '，请检查账号状态'))
    return data.get('data', {})


def item_projection(raw):
    d = raw.get('data', {})
    item, seller = d.get('itemDO', {}), d.get('sellerDO', {})
    if not item.get('itemId'):
        raise PlatformError('未读取到商品信息，请检查链接或商品状态')
    images = []
    for pic in item.get('imageInfos', []):
        if isinstance(pic, dict):
            url = pic.get('url') or pic.get('imageUrl') or ''
            if url.startswith('//'): url = 'https:' + url
            if url.startswith('https://'): images.append(url)
    cover = item.get('defaultPicture')
    if isinstance(cover, dict): cover = cover.get('url', '')
    if isinstance(cover, str) and cover.startswith('//'): cover = 'https:' + cover
    return {'id': str(item['itemId']), 'title': item.get('title', ''),
            'description': item.get('desc', ''), 'price': str(item.get('soldPrice', '')),
            'original_price': str(item.get('originalPrice', '')),
            'status': item.get('itemStatusStr', '未知'), 'cover': cover or (images[0] if images else ''),
            'images': images, 'views': item.get('browseCnt', 0), 'wants': item.get('wantCnt', 0),
            'collects': item.get('collectCnt', 0), 'seller_id': str(seller.get('sellerId', '')),
            'seller_name': seller.get('nick', ''), 'updated_at': time.time()}


class Platform:
    def __init__(self, session_path):
        self.session_path = Path(session_path)

    def identity(self):
        if not self.session_path.exists(): return None
        data = json.loads(self.session_path.read_text())
        cookies = {c['name']: c['value'] for c in data['cookies']}
        uid = cookies.get('unb')
        if not uid: return None
        from urllib.parse import unquote
        return {'id': uid, 'name': unquote(cookies.get('tracknick', '闲鱼账号')), 'status': 'connected'}

    def api(self):
        if not self.identity(): raise PlatformError('请先扫码连接闲鱼账号')
        data = json.loads(self.session_path.read_text())
        api = XianyuApis({}, data['device_id'])
        for cookie in data['cookies']:
            api.session.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
        original = api.session.request
        calls = 0
        def bounded(method, url, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 30: raise PlatformError('请求已达到本次操作上限，请检查登录状态')
            kwargs.setdefault('timeout', (10, 25))
            return original(method, url, **kwargs)
        api.session.request = bounded
        return api

    def check(self):
        api = self.api()
        result = api.get_token()
        succeeded(result)
        if not result.get('data', {}).get('accessToken'): raise PlatformError('未获取到消息凭证')
        save_session(self.session_path, api)
        return {'checked_at': time.time(), 'can_message': True}

    def resolve_item(self, value):
        value = str(value).strip()
        if re.fullmatch(r'\d{6,24}', value): return value
        match = re.search(r'https://[^\s「」<>]+', value)
        if not match: raise ValueError('请输入商品 ID 或闲鱼分享链接')
        url = match.group(0)
        parsed = urlsplit(url)
        if parsed.hostname not in ('m.tb.cn', 'www.goofish.com', 'h5.m.goofish.com', 'goofish.com'):
            raise ValueError('仅支持闲鱼商品链接或 m.tb.cn 分享短链')
        if parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError('商品链接格式不正确')
        match = re.search(r'(?:itemId|id)=(\d{6,24})', url)
        if match: return match.group(1)
        if parsed.hostname != 'm.tb.cn': raise ValueError('链接中没有商品 ID')
        response = requests.get(url, timeout=15, allow_redirects=False)
        source = response.text if response.status_code == 200 else response.headers.get('Location', '')
        match = re.search(r'(?:itemId|id)=(\d{6,24})', source)
        if not match: raise PlatformError('分享链接解析失败，请从闲鱼重新复制链接')
        return match.group(1)

    def item(self, value):
        item_id = self.resolve_item(value)
        api = self.api()
        result = api.get_item_info(item_id)
        succeeded(result)
        save_session(self.session_path, api)
        return item_projection(result)

    def locations(self):
        api = self.api()
        data = succeeded(api.get_default_location())
        save_session(self.session_path, api)
        return data.get('commonAddresses', [])

    def recommend(self, description, media):
        api = self.api()
        data = succeeded(api.get_public_channel(description, media))
        save_session(self.session_path, api)
        prediction = data.get('categoryPredictResult', {})
        return {'name': prediction.get('catName', ''), 'id': str(prediction.get('catId', '')),
                'cards': len(data.get('cardList', []))}

    def upload(self, path):
        api = self.api()
        result = api.upload_media(str(path))
        obj = result.get('object', {})
        if not obj.get('url'): raise PlatformError('图片上传未成功，请稍后重试')
        width, height = map(int, obj.get('pix', '0x0').split('x'))
        save_session(self.session_path, api)
        return {'url': obj['url'], 'width': width, 'height': height}

    def publish(self, draft, files, location):
        api = self.api()
        api.get_default_location = lambda: {'data': {'commonAddresses': [location]}}
        result = api.public(files, draft['description'],
                            {'current_price': float(draft['price']), 'original_price': float(draft.get('original_price') or 0)},
                            {'choice': draft['delivery'], 'post_price': float(draft.get('postage') or 0),
                             'can_self_pickup': bool(draft.get('self_pickup'))})
        data = succeeded(result)
        save_session(self.session_path, api)
        item_id = str(data.get('itemId') or '')
        if not item_id: raise UncertainError('发布接口返回成功，但未返回商品 ID，请在闲鱼核查')
        return {'item_id': item_id, 'url': f'https://www.goofish.com/item?id={item_id}'}

    def socket(self, operation, **kwargs):
        api = self.api()
        token = api.get_token()
        succeeded(token)
        if not token.get('data', {}).get('accessToken'): raise PlatformError('消息登录态已失效')
        result = asyncio.run(self._socket(api, token, operation, **kwargs))
        save_session(self.session_path, api)
        return result

    async def _socket(self, api, token, operation, **kwargs):
        live = XianyuLive(get_session_cookies_str(api.session))
        live.device_id, live.xianyu = api.device_id, api
        api.get_token = lambda: token
        conversations = {}
        async with websockets.connect(live.base_url, extra_headers={
            'Cookie': get_session_cookies_str(api.session), 'Origin': 'https://www.goofish.com',
            'User-Agent': 'Mozilla/5.0'}, open_timeout=15, close_timeout=3) as ws:
            registration_mid = None
            captured_mid = None
            class Capture:
                async def send(self, frame):
                    nonlocal registration_mid, captured_mid
                    m = json.loads(frame)
                    captured_mid = m['headers']['mid']
                    if m.get('lwp') == '/reg': registration_mid = captured_mid
                    await ws.send(frame)
            capture = Capture()

            async def receive(timeout=20):
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                h = m.get('headers', {})
                if m.get('lwp', '').startswith('/s/'):
                    await ws.send(json.dumps({'code': 200, 'headers': {k: h[k] for k in ('mid', 'sid', 'app-key', 'ua', 'dt') if k in h}}))
                body = m.get('body', {})
                if isinstance(body, dict):
                    for entry in body.get('syncPushPackage', {}).get('data', []):
                        try:
                            try: decoded = json.loads(entry['data'])
                            except (ValueError, TypeError):
                                try: decoded = json.loads(base64.b64decode(entry['data']))
                                except (ValueError, UnicodeError): decoded = json.loads(decrypt(entry['data']))
                            cid = decoded.get('sessionId') or decoded.get('1', {}).get('2', '')
                            if isinstance(cid, str) and re.fullmatch(r'\d+(?:@goofish)?', cid):
                                conversations[cid.split('@')[0]] = {'id': cid.split('@')[0]}
                        except Exception: pass
                return m

            async def wait(mid, timeout=20):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    m = await receive(deadline - time.monotonic())
                    if m.get('headers', {}).get('mid') == mid:
                        if m.get('code') != 200:
                            code = m.get('body', {}).get('code', '') if isinstance(m.get('body'), dict) else ''
                            if not re.fullmatch(r'[A-Z_0-9]{0,100}', str(code)): code = ''
                            raise PlatformError('消息接口拒绝请求' + ('：' + code if code else ''))
                        return m.get('body', {})
                raise TimeoutError()

            async def history(cid, cursor=None):
                mid = generate_mid()
                await ws.send(json.dumps({'lwp': '/r/MessageManager/listUserMessages', 'headers': {'mid': mid},
                    'body': [f'{cid}@goofish', False, cursor if cursor is not None else 9007199254740991, 20, False]}))
                body = await wait(mid)
                messages = []
                peer = None
                for row in body.get('userMessageModels', []):
                    msg = row.get('message', {})
                    ext = msg.get('extension', {})
                    sender = str(ext.get('senderUserId', ''))
                    if sender.isdigit() and sender != live.myid:
                        peer = sender
                    payload = {}
                    try: payload = json.loads(base64.b64decode(msg.get('content', {}).get('custom', {}).get('data', '')))
                    except Exception: pass
                    pics = payload.get('image', {}).get('pics', [])
                    text = payload.get('text', {}).get('text', '')
                    kind = 'text' if text else 'image' if pics else 'system'
                    message_id = str(msg.get('messageId') or msg.get('id') or row.get('messageId') or '')
                    timestamp = msg.get('createAt') or msg.get('createTime') or msg.get('sendTime') or msg.get('timestamp') or row.get('createTime') or 0
                    if timestamp and float(timestamp) > 1e11: timestamp = float(timestamp) / 1000
                    messages.append({'id': message_id, 'sender_id': sender, 'outgoing': sender == live.myid,
                        'kind': kind, 'text': text or ext.get('reminderContent', '系统通知'),
                        'images': [x.get('url', '') for x in pics if isinstance(x, dict)], 'timestamp': timestamp})
                messages.reverse()
                return {'messages': messages, 'peer_id': peer, 'has_more': bool(body.get('hasMore')),
                        'cursor': body.get('nextCursor')}

            await live.init(capture)
            await wait(registration_mid)
            if operation == 'sync':
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    try: await receive(deadline - time.monotonic())
                    except asyncio.TimeoutError: break
                result = []
                for cid in list(conversations)[:20]:
                    page = await history(cid)
                    result.append({'id': cid, **page})
                return {'conversations': result, 'scope': '本次同步发现的会话'}
            if operation == 'history': return await history(kwargs['cid'], kwargs.get('cursor'))
            if operation == 'create':
                await live.create_chat(capture, kwargs['peer_id'], kwargs['item_id'])
                body = await wait(captured_mid)
                cid = body.get('cid') or body.get('conversationId')
                if not cid: raise PlatformError('会话已响应，但未返回会话 ID，请回查列表')
                return {'id': str(cid).split('@')[0], 'peer_id': kwargs['peer_id']}
            if operation == 'send':
                content = make_image(**kwargs['image']) if kwargs.get('image') else make_text(kwargs['text'])
                await live.send_msg(capture, kwargs['cid'], kwargs['peer_id'], content)
                body = await wait(captured_mid)
                return {'accepted': True, 'message_id': str(body.get('messageId', '')), 'cid': kwargs['cid']}
        raise ValueError('不支持的消息操作')
