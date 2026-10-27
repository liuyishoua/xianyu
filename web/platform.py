"""Bounded adapters over the existing HTTP and WebSocket implementations."""
import asyncio
from contextlib import asynccontextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import requests
import socks
import websockets
from goofish_apis import XianyuApis, qrcode_login, _MTOP_HEADERS
from goofish_live import XianyuLive
from message import make_text, make_image
from utils.goofish_utils import generate_mid, get_session_cookies_str, decrypt, generate_sign


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
    data = {'device_id': api.device_id, 'cookies': [
        dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
             secure=c.secure, expires=c.expires) for c in api.session.cookies]}
    proxy = api.session.proxies.get('https')
    if proxy:
        from .phone_login_browser import browser_proxy
        data['network_config'] = {'browser_proxy': browser_proxy({'browser_proxy': proxy.replace('socks5h://', 'socks5://', 1)})}
    private_json(path, data)


@asynccontextmanager
async def message_connection(uri, session, **options):
    proxy = session.proxies.get('https')
    if not proxy:
        async with websockets.connect(uri, **options) as ws:
            yield ws
        return
    from .phone_login_browser import browser_proxy
    proxy = urlsplit(browser_proxy({'browser_proxy': proxy.replace('socks5h://', 'socks5://', 1)}))
    target = urlsplit(uri)
    sock = await asyncio.to_thread(socks.create_connection, (target.hostname, target.port or 443),
        proxy_type=socks.SOCKS5 if proxy.scheme == 'socks5' else socks.HTTP,
        proxy_addr=proxy.hostname, proxy_port=proxy.port, proxy_rdns=True, timeout=15)
    try:
        async with websockets.connect(uri, sock=sock, **options) as ws:
            yield ws
    finally:
        sock.close()


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


def conversation_projection(row, uid):
    """Project only the supported single-chat fields from the official WS model."""
    wrapper = row.get('singleChatUserConversation', {})
    conversation = wrapper.get('singleChatConversation', {})
    cid = str(conversation.get('cid') or '')
    if not re.fullmatch(r'\d+(?:@goofish)?', cid): return None
    members = [str(conversation.get(k) or '').split('@')[0] for k in ('pairFirst', 'pairSecond')]
    if str(uid) not in members: return None
    peer = next((x for x in members if x != str(uid) and x.isdigit()), None)
    if not peer: return None
    extension = conversation.get('extension') or {}
    latest = (wrapper.get('lastMessage') or {}).get('message') or {}
    ext = latest.get('extension') or {}
    name = ext.get('reminderTitle') if str(ext.get('senderUserId')) == peer else None
    return {'id': cid.split('@')[0], 'peer_id': peer, 'item_id': str(extension.get('itemId') or ''),
            'name': name or '用户 ' + peer[-4:]}


class Platform:
    supports_publish_progress = True
    supports_comments = True
    supports_comment_replies = True
    def __init__(self, session_path):
        self.session_path = Path(session_path)
        self._message_auth = None
        self._session_discovery = None

    def identity(self):
        if not self.session_path.exists(): return None
        data = json.loads(self.session_path.read_text())
        cookies = {c['name']: c['value'] for c in data['cookies']}
        uid = cookies.get('unb')
        if not uid: return None
        from urllib.parse import unquote
        name = unquote(cookies.get('tracknick', '闲鱼账号'))
        profile_path = self.session_path.with_name('profile.json')
        if profile_path.exists():
            profile = json.loads(profile_path.read_text())
            if profile.get('id') == uid and profile.get('name'): name = profile['name']
        return {'id': uid, 'name': name, 'status': 'connected'}

    def mtop_read(self, api, name, version, data, app_key='34839810'):
        previous = hashlib.sha256(self.session_path.read_bytes()).digest()
        payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        def operation():
            stamp = str(int(time.time() * 1000))
            token = api.session.cookies.get('_m_h5_tk', '').split('_')[0]
            params = {'jsv': '2.7.2', 'appKey': app_key, 't': stamp,
                'sign': hashlib.md5((token + '&' + stamp + '&' + app_key + '&' + payload).encode()).hexdigest(), 'v': version, 'api': name,
                'type': 'originaljson', 'dataType': 'json', 'sessionOption': 'AutoLoginOnly'}
            return api.session.post(f'https://h5api.m.goofish.com/h5/{name}/{version}/',
                params=params, data={'data': payload}, headers=_MTOP_HEADERS).json()
        result = succeeded(self.read(api, operation))
        save_session(self.session_path, api)
        if self._message_auth and self._message_auth['fingerprint'] == previous:
            self._message_auth['api'] = api
            self.persist_message_auth(self._message_auth)
        return result

    def profile(self):
        data = self.mtop_read(self.api(), 'mtop.idle.web.user.page.nav', '1.0', {})
        base = data.get('module', {}).get('base', {})
        if not base.get('displayName'): raise PlatformError('未读取到闲鱼昵称')
        profile = {'id': self.identity()['id'], 'name': base['displayName']}
        private_json(self.session_path.with_name('profile.json'), profile)
        return profile

    def comments(self, item_ids=()):
        """Read the same bounded, paginated list used by the official mobile item page."""
        # https://g.alicdn.com/idleFish-F2e/fish-mini-pha/1.4.9/detail.js
        api, rows = self.api(), []
        uid = (self.identity() or {}).get('id')
        for item_id in list(dict.fromkeys(str(x) for x in item_ids))[:10]:
            if not re.fullmatch(r'\d{6,24}', item_id): raise ValueError('商品 ID 无效')
            for page in range(1, 4):
                data = self.mtop_read(api, 'mtop.taobao.idle.comment.list', '5.0',
                    {'bizType': 0, 'itemId': item_id, 'pageNumber': page, 'rowsPerPage': 10}, app_key='12574478')
                for comment in data.get('items', []):
                    for row in [comment, *(comment.get('reply') or [])]:
                        mid = str(row.get('commentId') or '')
                        if not mid.isdigit() or str(row.get('itemId', item_id)) != item_id: continue
                        try:
                            stamp = datetime.strptime(row['reportTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
                        except (KeyError, TypeError, ValueError):
                            raise PlatformError('留言时间格式未识别，未将其视为新留言')
                        rows.append({'id': mid, 'item_id': item_id, 'parent_id': str(comment.get('commentId', '')),
                            'reply_to_id': str(row.get('replyCommentId') or (comment.get('commentId') if mid != str(comment.get('commentId')) else '') or ''),
                            'sender_id': str(row.get('reporterId') or ''), 'sender_name': row.get('reporterNick', ''),
                            'parent_sender_id': str(comment.get('reporterId') or ''), 'parent_sender_name': comment.get('reporterNick', ''),
                            'outgoing': row.get('reportOwner') is True or (uid is not None and str(row.get('reporterId')) == uid), 'kind': 'text',
                            'text': row.get('content', ''), 'timestamp': stamp,
                            'url': f'https://h5.m.goofish.com/item?id={item_id}'})
                if not data.get('nextPage'): break
        return rows

    def mtop_write(self, api, name, version, data, app_key='34839810'):
        """Exactly one signed submission; ambiguous responses are never retried."""
        previous = hashlib.sha256(self.session_path.read_bytes()).digest()
        payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        stamp = str(int(time.time() * 1000))
        token = api.session.cookies.get('_m_h5_tk', '').split('_')[0]
        params = {'jsv': '2.7.2', 'appKey': app_key, 't': stamp,
            'sign': hashlib.md5((token + '&' + stamp + '&' + app_key + '&' + payload).encode()).hexdigest(),
            'v': version, 'api': name, 'type': 'originaljson', 'dataType': 'json',
            'sessionOption': 'AutoLoginOnly'}
        try:
            response = api.session.post(f'https://h5api.m.goofish.com/h5/{name}/{version}/',
                params=params, data={'data': payload}, headers=_MTOP_HEADERS)
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            raise UncertainError('留言提交结果待核查，不能自动重试') from exc
        if not isinstance(result, dict) or not isinstance(result.get('ret'), list) or not result['ret']:
            raise UncertainError('留言接口未返回可核验回执，不能自动重试')
        value = succeeded(result)
        save_session(self.session_path, api)
        if self._message_auth and self._message_auth['fingerprint'] == previous:
            self._message_auth['api'] = api; self.persist_message_auth(self._message_auth)
        return value

    def reply_comment(self, comment, text):
        item_id, comment_id = str(comment.get('item_id', '')), str(comment.get('id', ''))
        parent_id = str(comment.get('parent_id') or comment_id)
        if not all(re.fullmatch(r'\d{6,24}', value) for value in (item_id, comment_id, parent_id)):
            raise ValueError('商品或留言 ID 无效')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 3000:
            raise ValueError('留言回复正文无效')
        # Refresh the canonical target using the official mobile app key; the PC key masks IDs.
        current = next((r for r in self.comments([item_id]) if r['id'] == comment_id), None)
        if current is None or current.get('outgoing'): raise ValueError('目标留言不存在或属于当前账号')
        comment = current
        parent_id = comment['parent_id']
        if not str(comment['sender_id']).isdigit(): raise PlatformError('留言用户身份尚未读取完整，未发送')
        item = self.item(item_id)
        identity = self.identity()
        if item['seller_id'] != identity['id']: raise ValueError('仅能回复当前账号自有商品的留言')
        # Official Android CommentReplyRequest ApiConfig: comment.reply / 2.0.
        data = {'bizType': 0, 'content': text.strip(), 'itemId': item_id,
            'sellerId': identity['id'], 'sellerNick': identity['name'], 'title': item['title'],
            'replyCommentId': comment_id, 'parentCommentId': parent_id,
            'parentCommenterId': comment.get('parent_sender_id') or comment.get('sender_id', ''),
            'parentCommenterNick': comment.get('parent_sender_name') or comment.get('sender_name', ''),
            'beReplierId': comment.get('sender_id', ''), 'beReplierNick': comment.get('sender_name', '')}
        result = self.mtop_write(self.api(), 'mtop.taobao.idle.comment.reply', '2.0', data, app_key='12574478')
        posted = (result.get('comment') or {}).get('reply') or []
        prefix = '回复@' + comment.get('sender_name', '') + ':'
        matching = [r for r in posted if str(r.get('reporterId')) == identity['id'] and
            str(r.get('replyCommentId')) == comment_id and r.get('content') in (text.strip(), prefix + text.strip())]
        if len(matching) != 1 or not str(matching[0].get('commentId', '')).isdigit():
            raise UncertainError('平台已接收，但未取得唯一回复 ID，请核对原留言，不能重发')
        return {'accepted': True, 'message_id': str(matching[0]['commentId']),
                'item_id': item_id, 'parent_id': parent_id, 'reply_to_id': comment_id}

    def api(self):
        if not self.identity(): raise PlatformError('请先扫码连接闲鱼账号')
        data = json.loads(self.session_path.read_text())
        api = XianyuApis({}, data['device_id'])
        if data.get('network_config'):
            from .phone_login_browser import browser_proxy
            proxy = browser_proxy(data['network_config'])
            if proxy:
                proxy = proxy.replace('socks5://', 'socks5h://', 1)
                api.session.proxies.update({'http': proxy, 'https': proxy})
                api.session.trust_env = False
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
        # A user-requested check must verify the current login, not cached credentials.
        self._message_auth = None
        self._session_discovery = None
        self.session_path.with_name('message-auth.json').unlink(missing_ok=True)
        api = self.api()
        result = self.read(api, api.get_token)
        succeeded(result)
        if not result.get('data', {}).get('accessToken'): raise PlatformError('未获取到消息凭证')
        save_session(self.session_path, api)
        self._message_auth = self.new_message_auth(api, result)
        self.persist_message_auth(self._message_auth)
        self.maintenance_event('connected')
        return {'checked_at': time.time(), 'can_message': True}

    def new_message_auth(self, api, token):
        duration = token.get('data', {}).get('accessTokenExpiredTime')
        try: duration = float(duration) / 1000
        except (ValueError, TypeError): duration = 20 * 60 + 60
        if not 60 < duration <= 86400: duration = 20 * 60 + 60
        return {'api': api, 'token': token, 'expires': time.time() + duration - 60}

    def persist_message_auth(self, cached):
        cached['fingerprint'] = hashlib.sha256(self.session_path.read_bytes()).digest()
        private_json(self.session_path.with_name('message-auth.json'), {
            'uid': self.identity()['id'], 'fingerprint': cached['fingerprint'].hex(),
            'token': cached['token'], 'expires': cached['expires']})

    def load_message_auth(self, fingerprint):
        try:
            saved = json.loads(self.session_path.with_name('message-auth.json').read_text())
            if saved['uid'] == self.identity()['id'] and saved['fingerprint'] == fingerprint.hex() and saved['expires'] > time.time():
                return {'api': self.api(), 'token': saved['token'], 'expires': saved['expires'], 'fingerprint': fingerprint}
        except (OSError, ValueError, KeyError, TypeError): pass

    def read(self, api, operation):
        """MTOP can issue a fresh cookie while rejecting the expired first signature."""
        before = api.session.cookies.get('_m_h5_tk', '')
        result = operation()
        codes = {str(value).split('::')[0] for value in result.get('ret', [])}
        after = api.session.cookies.get('_m_h5_tk', '')
        fresh = any(c.name == '_m_h5_tk' and c.value == after and not c.is_expired()
                    for c in api.session.cookies)
        if codes & {'FAIL_SYS_TOKEN_EMPTY', 'FAIL_SYS_TOKEN_EXPIRED', 'FAIL_SYS_TOKEN_EXOIRED'} and after != before and fresh:
            # Only retry this explicitly read-only operation, once, using the issued cookie.
            result = operation()
        codes = {str(value).split('::')[0] for value in result.get('ret', [])}
        return result

    def maintenance_event(self,state,component='account'):
        from .account_maintenance import Bridge
        identity=self.identity()
        if not identity:return
        try:
            directory=self.session_path.parents[2] if self.session_path.parent.parent.name=='accounts' else self.session_path.parent
            revision=hashlib.sha256(self.session_path.read_bytes()).hexdigest()
            Bridge(directory).notify(identity['id'],identity['id'],state,revision,component)
        except Exception:pass

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
        result = self.read(api, lambda: api.get_item_info(item_id))
        succeeded(result)
        save_session(self.session_path, api)
        return item_projection(result)

    def locations(self):
        api = self.api()
        data = succeeded(self.read(api, api.get_default_location))
        save_session(self.session_path, api)
        return data.get('commonAddresses', [])

    def recommend(self, description, media):
        api = self.api()
        data = succeeded(self.read(api, lambda: api.get_public_channel(description, media)))
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

    def publish(self, draft, files, location, progress=None):
        api = self.api()
        api.publish_progress=progress
        api.get_default_location = lambda: {'data': {'commonAddresses': [location] if location else []}}
        try:
            result = api.public(files, draft['description'],
                                {'current_price': float(draft['price']), 'original_price': float(draft.get('original_price') or 0)},
                                {'choice': draft['delivery'], 'post_price': float(draft.get('postage') or 0),
                                 'can_self_pickup': bool(draft.get('self_pickup'))})
        except Exception as exc:
            if not getattr(api,'publish_request_started',False):
                raise PlatformError('商品发布准备失败，最终发布接口尚未调用（'+getattr(api,'publish_preparation_stage','preparation')+'）') from exc
            raise
        if draft.get('id'):
            receipt_name=hashlib.sha256(str(draft['id']).encode()).hexdigest()+'.json'
            private_json(self.session_path.parent/'publication-receipts'/receipt_name,
                {'draft_id':draft['id'],'received_at':time.time(),'response':result})
        data = succeeded(result)
        save_session(self.session_path, api)
        item_id = str(data.get('itemId') or '')
        if not item_id: raise UncertainError('发布接口返回成功，但未返回商品 ID，请在闲鱼核查')
        return {'item_id': item_id, 'url': f'https://www.goofish.com/item?id={item_id}'}

    def socket(self, operation, **kwargs):
        try:
            return self._message_operation(operation, **kwargs)
        except Exception as exc:
            failure=str(exc) if isinstance(exc,PlatformError) else ''
            if 'SESSION_EXPIRED' in failure or 'SESSION_EXOIRED' in failure:self.maintenance_event('expired','listener.dm')
            elif 'USER_VALIDATE' in failure or 'RGV587_ERROR' in failure:self.maintenance_event('risk','listener.dm')
            # Neither a rejected login nor an uncertain write reuses the failed connection.
            self._message_auth = None
            self._session_discovery = None
            self.session_path.with_name('message-auth.json').unlink(missing_ok=True)
            raise

    def _message_operation(self, operation, **kwargs):
        fingerprint = hashlib.sha256(self.session_path.read_bytes()).digest()
        cached = self._message_auth or self.load_message_auth(fingerprint)
        if cached and cached['fingerprint'] == fingerprint and time.time() < cached['expires']:
            # A fresh local HTTP session resets the per-operation bound and picks up
            # renewed website cookies without requesting a new IM credential.
            api, token = self.api(), cached['token']
            cached['api'] = api
        else:
            self._message_auth = None
            self._session_discovery = None
            api = self.api()
            token = self.read(api, api.get_token)
            succeeded(token)
            if not token.get('data', {}).get('accessToken'): raise PlatformError('消息登录态已失效')
            cached = self.new_message_auth(api, token)
        if operation == 'sync':
            # Discover recent sessions explicitly instead of depending only on initial WS pushes.
            name = 'mtop.taobao.idlemessage.pc.session.sync'
            discovery = self._session_discovery
            if discovery and time.time() < discovery['expires']:
                ids = discovery['ids']
            else:
                sessions = self.mtop_read(api, name, '3.0', {'sessionTypes': '[1]', 'fetchNum': 30})
                ids = []
                for summary in sessions.get('sessions', []):
                    cid = str(summary.get('session', {}).get('sessionId') or '')
                    if re.fullmatch(r'\d+(?:@goofish)?', cid): ids.append(cid.split('@')[0])
                self._session_discovery = {'ids': ids, 'expires': time.time() + 60}
            kwargs['session_ids'] = ids
        result = asyncio.run(self._socket(api, token, operation, **kwargs))
        save_session(self.session_path, api)
        self.persist_message_auth(cached)
        self._message_auth = cached
        return result

    async def _socket(self, api, token, operation, **kwargs):
        live = XianyuLive(get_session_cookies_str(api.session))
        live.device_id, live.xianyu = api.device_id, api
        api.get_token = lambda: token
        conversations = {}
        for cid in kwargs.get('session_ids', []): conversations[cid] = {'id': cid}
        async with message_connection(live.base_url, api.session, extra_headers={
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
                    # The legacy initializer uses wall-clock time as a guessed sync cursor.
                    # Register first; sync below acknowledges only server-issued state.
                    if m.get('lwp') == '/r/SyncStatus/ackDiff': return
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
                                conversations.setdefault(cid.split('@')[0], {'id': cid.split('@')[0]})
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
                    sender = str(ext.get('senderUserId') or msg.get('sender', {}).get('uid') or '').split('@')[0]
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
                mid = generate_mid()
                await ws.send(json.dumps({'lwp': '/r/SyncStatus/getState', 'headers': {'mid': mid},
                    'body': [{'topic': 'sync'}]}))
                sync_state = await wait(mid)
                if not isinstance(sync_state, dict) or not sync_state:
                    raise PlatformError('未读取到消息同步状态，不能确认同步成功')
                mid = generate_mid()
                await ws.send(json.dumps({'lwp': '/r/SyncStatus/ackDiff', 'headers': {'mid': mid},
                    'body': [sync_state]}))
                await wait(mid)
                mid = generate_mid()
                await ws.send(json.dumps({'lwp': '/r/Conversation/listNewestPagination',
                    'headers': {'mid': mid}, 'body': [int(time.time() * 1000), 50]}))
                newest = await wait(mid)
                if not isinstance(newest, dict) or not isinstance(newest.get('userConvs'), list):
                    raise PlatformError('未读取到消息会话列表，不能确认同步成功')
                recent = {}
                for row in newest['userConvs']:
                    conv = conversation_projection(row, live.myid)
                    if conv: recent[conv['id']] = conv
                # Prefer the WS list: the PC HTTP list can omit active conversations.
                conversations = {**recent, **{k: v for k, v in conversations.items() if k not in recent}}
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    try: await receive(deadline - time.monotonic())
                    except asyncio.TimeoutError: break
                result = []
                for cid in list(conversations)[:50]:
                    page = await history(cid)
                    page['peer_id'] = page.get('peer_id') or conversations[cid].get('peer_id')
                    result.append({**conversations[cid], **page})
                return {'conversations': result, 'scope': '消息通道最近 50 个会话，结合网页列表及本次推送补充（最多 50 个）'}
            if operation == 'history': return await history(kwargs['cid'], kwargs.get('cursor'))
            if operation == 'create':
                await live.create_chat(capture, kwargs['peer_id'], kwargs['item_id'])
                body = await wait(captured_mid)
                conversation = body.get('singleChatConversation') or body
                cid = str(conversation.get('cid') or conversation.get('conversationId') or '')
                if not re.fullmatch(r'\d+(?:@goofish)?', cid):
                    raise UncertainError('创建会话已响应，但未返回有效会话 ID，请同步列表核查，不要重复创建')
                return {'id': cid.split('@')[0], 'peer_id': kwargs['peer_id'], 'item_id': kwargs['item_id']}
            if operation == 'send':
                content = make_image(**kwargs['image']) if kwargs.get('image') else make_text(kwargs['text'])
                await live.send_msg(capture, kwargs['cid'], kwargs['peer_id'], content)
                body = await wait(captured_mid)
                return {'accepted': True, 'message_id': str(body.get('messageId', '')), 'cid': kwargs['cid']}
        raise ValueError('不支持的消息操作')
