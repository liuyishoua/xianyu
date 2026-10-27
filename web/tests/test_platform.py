"""Recorded protocol shapes, with all network traffic replaced by a fake socket."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock, AsyncMock

import requests

from web.platform import Platform, PlatformError, UncertainError, private_json, save_session, conversation_projection


class HomeNetworkTests(unittest.TestCase):
    def api(self):
        api=SimpleNamespace(session=requests.Session(),device_id='fixture-device')
        api.session.cookies.set('unb','100001',domain='.goofish.com',path='/')
        api.session.proxies={'http':'socks5h://127.0.0.1:18890','https':'socks5h://127.0.0.1:18890'}
        api.session.trust_env=False
        return api

    def test_saved_account_reloads_its_original_home_network(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'session.json'
            save_session(path,self.api())
            api=Platform(path).api()
            self.assertEqual(api.session.proxies,self.api().session.proxies)
            self.assertFalse(api.session.trust_env)

    def test_saved_account_rejects_a_non_loopback_proxy(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'session.json';save_session(path,self.api())
            data=json.loads(path.read_text());data['network_config']={'browser_proxy':'socks5://example.com:1080'}
            private_json(path,data)
            with self.assertRaises(PlatformError):Platform(path).api()

    def test_message_connection_uses_remote_dns_and_closes_its_proxy_socket(self):
        api=self.api();sock=Mock()
        live=SimpleNamespace(base_url='wss://fixture.invalid/')
        with patch('socks.create_connection',return_value=sock) as connect, \
                patch('web.platform.XianyuLive',return_value=live), \
                patch('web.platform.websockets.connect',side_effect=RuntimeError('fixture handshake')) as ws:
            with self.assertRaisesRegex(RuntimeError,'fixture handshake'):
                asyncio.run(Platform(Path('unused'))._socket(api,{},'sync'))
            connect.assert_called_once()
            self.assertTrue(connect.call_args.kwargs['proxy_rdns'])
            self.assertEqual(connect.call_args.kwargs['proxy_addr'],'127.0.0.1')
            self.assertEqual(connect.call_args.kwargs['proxy_port'],18890)
            self.assertIs(ws.call_args.kwargs['sock'],sock)
            sock.close.assert_called_once()

    def test_message_connection_never_falls_back_when_home_network_fails(self):
        live=SimpleNamespace(base_url='wss://fixture.invalid/')
        with patch('socks.create_connection',side_effect=OSError('home offline')), \
                patch('web.platform.XianyuLive',return_value=live), \
                patch('web.platform.websockets.connect',side_effect=RuntimeError('unexpected direct connection')) as ws:
            with self.assertRaisesRegex(OSError,'home offline'):
                asyncio.run(Platform(Path('unused'))._socket(self.api(),{},'sync'))
            ws.assert_not_called()


class ConversationProtocolTests(unittest.TestCase):
    def create(self, response):
        class Socket:
            def __init__(self):
                self.frames = asyncio.Queue()

            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass

            async def send(self, frame):
                frame = json.loads(frame)
                body = {} if frame['lwp'] == '/reg' else response
                await self.frames.put(json.dumps({'code': 200, 'headers': frame['headers'], 'body': body}))

            async def recv(self): return await self.frames.get()

        class Live:
            def __init__(self, cookies):
                self.myid = '100001'
                self.base_url = 'wss://invalid.example/'

            async def init(self, ws):
                await ws.send(json.dumps({'lwp': '/reg', 'headers': {'mid': 'register'}}))

            async def create_chat(self, ws, peer_id, item_id):
                await ws.send(json.dumps({'lwp': '/r/SingleChatConversation/create', 'headers': {'mid': 'create'}}))

        api = SimpleNamespace(session=requests.Session(), device_id='test-device')
        with tempfile.TemporaryDirectory() as directory:
            platform = Platform(Path(directory) / 'unused.json')
            with patch('web.platform.websockets.connect', return_value=Socket()), patch('web.platform.XianyuLive', Live):
                return asyncio.run(platform._socket(api, {}, 'create', peer_id='100002', item_id='10000001'))

    def test_pc_create_reads_nested_conversation_and_keeps_item_context(self):
        result = self.create({'singleChatConversation': {'cid': '987654321@goofish',
            'pairFirst': '100002@goofish', 'pairSecond': '100001@goofish'}, 'lastMessage': None})
        self.assertEqual(result, {'id': '987654321', 'peer_id': '100002', 'item_id': '10000001'})

    def test_legacy_create_shape_remains_usable(self):
        self.assertEqual(self.create({'cid': '987654321'})['id'], '987654321')

    def test_success_without_usable_cid_is_uncertain_not_retryable_failure(self):
        with self.assertRaises(UncertainError): self.create({'singleChatConversation': {}})
        with self.assertRaises(UncertainError): self.create({'cid': 'invalid'})


class TokenRenewalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.json'
        self.platform = Platform(self.path)
        self.api = SimpleNamespace(session=requests.Session(), device_id='test-device')
        self.api.session.cookies.set('unb', '100001', domain='.goofish.com', path='/')
        self.api.session.cookies.set('_m_h5_tk', 'expired-fixture', domain='.goofish.com', path='/', expires=1)
        private_json(self.path, {'device_id': 'test-device', 'cookies': []})

    def renew(self):
        self.api.session.cookies.set('_m_h5_tk', 'fresh-fixture', domain='.goofish.com', path='/')

    def test_account_check_renews_short_lived_cookie_and_persists_session(self):
        from unittest.mock import Mock
        calls = []
        def response():
            calls.append(self.api.session.cookies.get('_m_h5_tk'))
            if len(calls) == 1:
                self.renew()
                return {'ret': ['FAIL_SYS_TOKEN_EMPTY::令牌为空']}
            return {'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'test-message-token'}}
        self.api.get_token = Mock(side_effect=response)
        with patch.object(self.platform, 'api', return_value=self.api):
            self.assertTrue(self.platform.check()['can_message'])
        self.assertEqual(calls, ['expired-fixture', 'fresh-fixture'])
        stored = json.loads(self.path.read_text())
        self.assertEqual(next(c['value'] for c in stored['cookies'] if c['name'] == '_m_h5_tk'), 'fresh-fixture')
        self.assertEqual(Platform(self.path).identity()['id'], '100001')

    def test_security_challenge_is_never_retried_even_if_cookie_changes(self):
        from unittest.mock import Mock
        def response():
            self.renew()
            return {'ret': ['FAIL_SYS_USER_VALIDATE::安全验证']}
        self.api.get_token = Mock(side_effect=response)
        with patch.object(self.platform, 'api', return_value=self.api):
            with self.assertRaises(PlatformError): self.platform.check()
        self.assertEqual(self.api.get_token.call_count, 1)

    def test_missing_fresh_cookie_does_not_loop(self):
        from unittest.mock import Mock
        self.api.get_token = Mock(return_value={'ret': ['FAIL_SYS_TOKEN_EMPTY::令牌为空']})
        with patch.object(self.platform, 'api', return_value=self.api):
            with self.assertRaises(PlatformError): self.platform.check()
        self.assertEqual(self.api.get_token.call_count, 1)

    def test_cookie_renewal_has_only_one_read_retry(self):
        from unittest.mock import Mock
        self.api.get_token = Mock(side_effect=[{'ret': ['FAIL_SYS_TOKEN_EMPTY']}, {'ret': ['FAIL_SYS_TOKEN_EMPTY']}])
        original = self.api.get_token
        def response():
            self.renew()
            return original()
        with patch.object(self.platform, 'api', return_value=self.api):
            self.api.get_token = response
            # Start with an expired value; the first response issues the new cookie.
            with self.assertRaises(PlatformError): self.platform.check()
        self.assertEqual(original.call_count, 2)


class MessageAuthenticationCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.json'
        self.platform = Platform(self.path)
        self.api = SimpleNamespace(session=requests.Session(), device_id='test-device')
        self.api.session.cookies.set('unb', '100001', domain='.goofish.com', path='/')
        self.api.session.cookies.set('_m_h5_tk', 'fixture', domain='.goofish.com', path='/')
        self.api.get_token = Mock(return_value={'ret': ['SUCCESS'], 'data': {'accessToken': 'fixture'}})
        save_session(self.path, self.api)
        self.factory = patch.object(self.platform, 'api', return_value=self.api)
        self.factory.start(); self.addCleanup(self.factory.stop)
        self.wire = patch.object(self.platform, '_socket', new_callable=AsyncMock)
        self.socket = self.wire.start(); self.socket.return_value = {'messages': []}
        self.addCleanup(self.wire.stop)

    def test_repeated_reads_reuse_auth_but_expiry_and_login_change_refresh_it(self):
        with patch('web.platform.time.time', return_value=100) as clock:
            self.platform.socket('history', cid='123456')
            self.platform.socket('history', cid='123456')
            self.assertEqual(self.api.get_token.call_count, 1)
            clock.return_value = 1401
            self.platform.socket('history', cid='123456')
            self.assertEqual(self.api.get_token.call_count, 2)
            self.api.session.cookies.set('cookie2', 'new-login', domain='.goofish.com', path='/')
            save_session(self.path, self.api)
            self.platform.socket('history', cid='123456')
            self.assertEqual(self.api.get_token.call_count, 3)

    def test_successful_check_warms_auth_and_survives_restart(self):
        self.platform.check()
        self.platform.socket('history', cid='123456')
        self.assertEqual(self.api.get_token.call_count, 1)
        fresh = Platform(self.path)
        with patch.object(fresh, 'api', return_value=self.api), patch.object(fresh, '_socket', new_callable=AsyncMock):
            fresh.socket('history', cid='123456')
        self.assertEqual(self.api.get_token.call_count, 1)
        self.assertEqual(self.path.with_name('message-auth.json').stat().st_mode & 0o777, 0o600)

    def test_platform_token_lifetime_is_respected(self):
        self.api.get_token.return_value['data']['accessTokenExpiredTime'] = '86400000'
        with patch('web.platform.time.time', return_value=100) as clock:
            self.platform.socket('history', cid='123456')
            clock.return_value = 3601
            self.platform.socket('history', cid='123456')
            self.assertEqual(self.api.get_token.call_count, 1)
            clock.return_value = 86500
            self.platform.socket('history', cid='123456')
            self.assertEqual(self.api.get_token.call_count, 2)

    def test_new_session_discovery_is_bounded_without_caching_message_history(self):
        response = Mock(); response.json.return_value = {'ret': ['SUCCESS'],
            'data': {'sessions': [{'session': {'sessionId': '123456'}}]}}
        with patch.object(self.api.session, 'post', return_value=response) as discovery, patch('web.platform.time.time', return_value=100) as clock:
            self.platform.socket('sync'); self.platform.socket('sync')
            self.assertEqual(discovery.call_count, 1)
            self.assertEqual(self.socket.await_count, 2)
            self.assertEqual(self.socket.call_args.kwargs['session_ids'], ['123456'])
            clock.return_value = 161
            self.platform.socket('sync')
            self.assertEqual(discovery.call_count, 2)
            self.assertEqual(self.api.get_token.call_count, 1)

    def test_uncertain_send_is_not_retried_and_invalidates_cached_auth(self):
        self.platform.socket('history', cid='123456')
        self.socket.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError): self.platform.socket('send', cid='123456', peer_id='100002', text='fixture')
        self.assertEqual(self.socket.await_count, 2)
        self.socket.side_effect = None
        self.platform.socket('history', cid='123456')
        self.assertEqual(self.api.get_token.call_count, 2)

    def test_manual_account_check_always_revalidates_cached_login(self):
        self.platform.socket('history', cid='123456')
        self.api.get_token.return_value = {'ret': ['FAIL_SYS_USER_VALIDATE']}
        with self.assertRaises(PlatformError): self.platform.check()
        self.assertEqual(self.api.get_token.call_count, 2)
        with self.assertRaises(PlatformError): self.platform.socket('history', cid='123456')
        self.assertEqual(self.socket.await_count, 1)



class CommentProtocolTests(unittest.TestCase):
    def test_official_pagination_nested_replies_and_china_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Platform(Path(directory) / 'session.json')
            a = {'commentId': 601541323730, 'itemId': '1089891189407', 'content': '私聊',
                 'reporterId': '2***6', 'reporterNick': '福***y', 'reportOwner': False,
                 'reportTime': '2026-10-11 15:52:10', 'reply': [{
                     'commentId': 601541323731, 'content': '你好', 'reportOwner': True,
                     'reportTime': '2026-10-11 15:53:10'}]}
            pages = [{'items': [a], 'nextPage': True}, {'items': [], 'nextPage': False}]
            with patch.object(p, 'api'), patch.object(p, 'mtop_read', side_effect=pages) as read:
                rows = p.comments(['1089891189407', '1089891189407'])
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['text'], '私聊')
            self.assertEqual(rows[0]['timestamp'], 1791705130.0)
            self.assertEqual(rows[0]['sender_id'], '2***6')
            self.assertFalse(rows[0]['outgoing'])
            self.assertTrue(rows[1]['outgoing'])
            self.assertEqual(rows[1]['item_id'], '1089891189407')
            self.assertEqual(read.call_args_list[1].args[3]['pageNumber'], 2)
            self.assertTrue(p.supports_comment_replies)
            self.assertEqual(read.call_args_list[0].kwargs['app_key'], '12574478')

    def test_reply_uses_canonical_mobile_identity_and_nested_reply_id(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Platform(Path(directory) / 'unused.json')
            canonical = {'id': '601541323730', 'parent_id': '601541323730', 'item_id': '1089891189407',
                         'sender_id': '2223202332036', 'sender_name': '福来day', 'outgoing': False}
            result = {'commentId': 601541323730, 'comment': {'reply': [{'commentId': 601542954113,
                'replyCommentId': 601541323730, 'reporterId': '3326894759', 'content': '回复@福来day:你好'}]}}
            with patch.object(p, 'comments', return_value=[canonical]), patch.object(p, 'item',
                return_value={'seller_id': '3326894759', 'title': '商品'}), patch.object(p, 'identity',
                return_value={'id': '3326894759', 'name': 'liuyishou'}), patch.object(p, 'api'), \
                patch.object(p, 'mtop_write', return_value=result) as write:
                receipt = p.reply_comment({**canonical, 'sender_id': '2***6'}, '你好')
            self.assertEqual(receipt['message_id'], '601542954113')
            self.assertEqual(write.call_args.args[1:3], ('mtop.taobao.idle.comment.reply', '2.0'))
            self.assertEqual(write.call_args.args[3]['beReplierId'], '2223202332036')
            self.assertEqual(write.call_args.kwargs['app_key'], '12574478')

    def test_mobile_numeric_sender_prevents_replying_to_own_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Platform(Path(directory) / 'unused.json')
            raw = {'commentId': 601542954113, 'itemId': '1089891189407', 'reporterId': '3326894759',
                   'reportOwner': False, 'reportTime': '2026-10-11 17:48:25', 'content': '回复@福来day:你好'}
            with patch.object(p, 'identity', return_value={'id': '3326894759'}), patch.object(p, 'api'), \
                patch.object(p, 'mtop_read', return_value={'items': [raw]}):
                self.assertTrue(p.comments(['1089891189407'])[0]['outgoing'])

    def test_comment_write_timeout_is_one_attempt_and_never_uses_read_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.json'; path.write_text('{}')
            p = Platform(path); api = SimpleNamespace(session=requests.Session())
            api.session.post = Mock(side_effect=requests.Timeout())
            with patch.object(p, 'read', side_effect=AssertionError('writes cannot use read retry')):
                with self.assertRaises(UncertainError):
                    p.mtop_write(api, 'mtop.taobao.idle.comment.reply', '2.0', {'content': 'hello'}, app_key='12574478')
            self.assertEqual(api.session.post.call_count, 1)

    def test_unrecognized_timestamp_cannot_trigger_a_new_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Platform(Path(directory) / 'session.json')
            with patch.object(p, 'api'), patch.object(p, 'mtop_read', return_value={
                    'items': [{'commentId': 123, 'content': 'hello', 'reportTime': 'unknown'}]}):
                with self.assertRaises(PlatformError): p.comments(['1089891189407'])

    def test_profile_display_name_is_scoped_to_cookie_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.json'
            private_json(path, {'device_id': 'fixture', 'cookies': [
                {'name': 'unb', 'value': '100001'}, {'name': 'tracknick', 'value': 'legacy-name'}]})
            p = Platform(path)
            with patch.object(p, 'api'), patch.object(p, 'mtop_read', return_value={
                    'module': {'base': {'displayName': '真实昵称'}}}):
                self.assertEqual(p.profile()['name'], '真实昵称')
            self.assertEqual(p.identity()['name'], '真实昵称')
            private_json(path.with_name('profile.json'), {'id': '100002', 'name': '另一个账号'})
            self.assertEqual(p.identity()['name'], 'legacy-name')

class SyncHandshakeTests(unittest.TestCase):
    def test_server_cursor_is_acknowledged_after_registration(self):
        sent = []
        state = {'topic': 'sync', 'pts': 123456, 'seq': 7, 'timestamp': 1791700000000}
        class Socket:
            def __init__(self): self.frames = asyncio.Queue()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, frame):
                m = json.loads(frame); sent.append(m)
                body = state if m['lwp'] == '/r/SyncStatus/getState' else {}
                if m['lwp'] == '/r/Conversation/listNewestPagination':
                    body = {'userConvs': [{'singleChatUserConversation': {'singleChatConversation': {
                        'cid': '987654321@goofish', 'pairFirst': '100001@goofish',
                        'pairSecond': '100002@goofish', 'extension': {'itemId': '1089891189407'}}}}]}
                if m['lwp'] == '/r/MessageManager/listUserMessages': body = {'userMessageModels': []}
                await self.frames.put(json.dumps({'code': 200, 'headers': m['headers'], 'body': body}))
            async def recv(self): return await self.frames.get()
        class Live:
            def __init__(self, cookies): self.myid, self.base_url = '100001', 'wss://invalid.example/'
            async def init(self, ws):
                await ws.send(json.dumps({'lwp': '/reg', 'headers': {'mid': 'register'}}))
                await ws.send(json.dumps({'lwp': '/r/SyncStatus/ackDiff', 'headers': {'mid': 'legacy'},
                    'body': [{'pts': 9999999999999999}]}))
        api = SimpleNamespace(session=requests.Session(), device_id='fixture')
        with tempfile.TemporaryDirectory() as directory:
            p = Platform(Path(directory) / 'session.json')
            with patch('web.platform.websockets.connect', return_value=Socket()), patch('web.platform.XianyuLive', Live):
                result = asyncio.run(p._socket(api, {}, 'sync', session_ids=[]))
        self.assertEqual(result['conversations'][0]['id'], '987654321')
        self.assertEqual(result['conversations'][0]['peer_id'], '100002')
        self.assertEqual(result['conversations'][0]['item_id'], '1089891189407')
        self.assertEqual([m['lwp'] for m in sent], ['/reg', '/r/SyncStatus/getState',
            '/r/SyncStatus/ackDiff', '/r/Conversation/listNewestPagination', '/r/MessageManager/listUserMessages'])
        self.assertEqual(sent[2]['body'], [state])

    def test_ws_conversation_cannot_cross_account_membership(self):
        row = {'singleChatUserConversation': {'singleChatConversation': {
            'cid': '67779403080@goofish', 'pairFirst': '3326894759@goofish',
            'pairSecond': '2223202332036@goofish', 'extension': {'itemId': '1089891189407'}},
            'lastMessage': {'message': {'extension': {'senderUserId': '2223202332036', 'reminderTitle': '测试买家'}}}}}
        self.assertEqual(conversation_projection(row, '3326894759')['name'], '测试买家')
        self.assertIsNone(conversation_projection(row, '100003'))

if __name__ == '__main__': unittest.main()


class PublicationStageTests(unittest.TestCase):
    def test_publish_location_matches_official_latitude_longitude_contract(self):
        from goofish_apis import XianyuApis
        api=XianyuApis({},'synthetic')
        api.get_public_channel=lambda *args:{'data':{'cardList':[],'categoryPredictResult':{'catId':'1','catName':'服务','channelCatId':'2','tbCatId':'3'}}}
        location={'area':'合成区','city':'合成市','divisionId':'synthetic','longitude':'116.4','latitude':'39.9','poiId':'synthetic','poi':'合成服务区域','prov':'合成省'}
        api.get_default_location=lambda:{'data':{'commonAddresses':[location]}}
        api.session.post=Mock(return_value=SimpleNamespace(json=lambda:{'ret':['SUCCESS'],'data':{'itemId':'synthetic'}}))
        api.public([],'合成服务',{'current_price':20,'original_price':0},{'choice':'无需邮寄','post_price':0,'can_self_pickup':False})
        payload=json.loads(api.session.post.call_args.kwargs['data']['data'])
        self.assertEqual(payload['itemAddrDTO']['gps'],'39.9,116.4')
        self.assertEqual(payload['itemAddrDTO']['divisionId'],'synthetic')

    def test_prepare_failure_is_definitive_and_final_timeout_uncertain(self):
        draft={'description':'服务','price':'20','delivery':'无需邮寄'}
        for started in (False,True):
            api=SimpleNamespace(publish_request_started=started,publish_preparation_stage='media_upload')
            def fail(*args):raise requests.Timeout('synthetic')
            api.public=fail
            p=Platform(Path('synthetic-unused'))
            with patch.object(p,'api',return_value=api):
                with self.assertRaises(requests.Timeout if started else PlatformError):
                    p.publish(draft,[],None)

    def test_low_level_stage_marks_before_final_write(self):
        from goofish_apis import XianyuApis
        api=XianyuApis({},'synthetic')
        stages=[];api.publish_progress=lambda stage,started:stages.append((stage,started))
        api.get_public_channel=lambda *args:{'data':{'cardList':[],'categoryPredictResult':{'catId':'1','catName':'服务','channelCatId':'2','tbCatId':'3'}}}
        api.get_default_location=lambda:{'data':{'commonAddresses':[]}}
        api.session.post=Mock(side_effect=requests.Timeout('synthetic-final'))
        with self.assertRaises(requests.Timeout):api.public([],'服务',{'current_price':20,'original_price':0},{'choice':'无需邮寄','post_price':0,'can_self_pickup':False})
        self.assertEqual(stages[-1],('final_publish',True))
        api.session.post.assert_called_once()
        api.upload_media=Mock(side_effect=requests.Timeout('synthetic-upload'))
        api.session.post.reset_mock()
        with self.assertRaises(requests.Timeout):api.public(['synthetic-file'],'服务',{'current_price':20,'original_price':0},{'choice':'无需邮寄','post_price':0,'can_self_pickup':False})
        self.assertFalse(api.publish_request_started)
        api.session.post.assert_not_called()

    def test_rejection_receipt_is_kept_private_before_safe_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Platform(Path(directory)/'session.json')
            reply={'ret':['FAIL_BIZ_STRONG_VALID_VERIFY_INFO::synthetic verification'],'data':{'verifyType':'synthetic-only'}}
            api=SimpleNamespace(public=lambda *args:reply)
            with patch.object(p,'api',return_value=api):
                with self.assertRaises(PlatformError):p.publish({'id':'synthetic-draft','description':'合成','price':'20','delivery':'无需邮寄'},[],None)
            files=list((Path(directory)/'publication-receipts').glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text())['response'],reply)
            self.assertEqual(files[0].stat().st_mode & 0o777,0o600)
