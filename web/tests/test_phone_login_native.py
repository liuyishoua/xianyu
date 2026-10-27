import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from web.phone_login_native import NativePhoneLogin,NativeSession,POOL,POOL_LOCK
from web.phone_login import PhoneLoginError
from web.app import create_app
from web.demo import DemoPlatform
from web.platform import private_json


class NativeSmsTests(unittest.TestCase):
    def test_login_receipt_keeps_the_browser_home_proxy(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=NativePhoneLogin(d,{'browser_proxy':'socks5://127.0.0.1:18890'})
            s=NativeSession(adapter,'request','13800000000','owner')
            response=Mock();response.json.return_value={'content':{'data':{'loginSuccess':True}}}
            s.login_responses=[response];s.context=Mock()
            s.context.cookies.return_value=[{'name':'unb','value':'owner','domain':'.goofish.com',
                'path':'/','secure':True,'expires':-1}]
            api=s.receipt()
            self.assertEqual(api.session.proxies,{'http':'socks5h://127.0.0.1:18890',
                'https':'socks5h://127.0.0.1:18890'})
            self.assertFalse(api.session.trust_env)

    def session(self,d):
        adapter=NativePhoneLogin(d);session=NativeSession(adapter,'request','13800000000','owner')
        session.page=Mock();session.context=Mock();return session

    def test_only_successful_official_response_enters_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.session(d);s.state='verification';s.sync_waiting=Mock()
            response=Mock();response.json.return_value={'data':{},'ret':['RGV587_ERROR::SM']}
            s.responses=[response];s.inspect_send()
            self.assertEqual(s.state,'verification');s.sync_waiting.assert_not_called()
            response.json.return_value={'content':{'data':{'smsToken':'fixture-token'}}}
            s.inspect_send();self.assertEqual(s.state,'waiting');s.sync_waiting.assert_called_once()
            s.inspect_send();s.sync_waiting.assert_called_once()

    def test_cookie_presence_does_not_prove_official_login_success(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.session(d)
            with self.assertRaises(PhoneLoginError):s.receipt()
            s.context.cookies.assert_not_called()

    def test_same_browser_confirms_once_and_checks_original_identity(self):
        for uid,success in (('owner',True),('other-owner',False)):
            with self.subTest(uid=uid),tempfile.TemporaryDirectory() as d:
                s=self.session(d);s.state='waiting'
                s.expires=9999999999
                response=Mock();response.json.return_value={'content':{'data':{'loginSuccess':True}}}
                s.login_responses=[response]
                s.context.cookies.return_value=[{'name':'unb','value':uid,'domain':'.goofish.com','path':'/','secure':True,'expires':-1}]
                s.page.expect_response.return_value.__enter__=Mock()
                s.page.expect_response.return_value.__exit__=Mock(return_value=False)
                if success:
                    api=s.execute('confirm',{'code':'123456'})
                    self.assertEqual(api.session.cookies.get_dict()['unb'],'owner')
                else:
                    with self.assertRaises(PhoneLoginError):s.execute('confirm',{'code':'123456'})
                with self.assertRaises(PhoneLoginError):s.execute('confirm',{'code':'123456'})
                s.page.locator.return_value.click.assert_called_once()
                s.page.locator.return_value.fill.assert_called_once_with('123456')

    def test_native_verification_controls_reject_script_and_invalid_coordinates(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.session(d)
            for points in ([[1001,1]],[[1,-1]],[[True,1]],[[1,float('nan')]],[],[[1,1]]*101):
                with self.assertRaises(ValueError):s.execute('drag',{'points':points})
            s.page.mouse.down.assert_not_called()
            with self.assertRaises(ValueError):s.execute('eval',{'text':'document.cookie'})
            with self.assertRaises(ValueError):s.execute('text',{'text':'<script>'})

    def test_duplicate_start_preserves_binding_and_never_reopens_or_resends(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=NativePhoneLogin(d)
            def ready():
                session=adapter.selected('request');session.state='verification';session.ready.set()
            with patch('web.phone_login_native.threading.Thread') as thread:
                thread.return_value.start.side_effect=ready
                self.assertEqual(adapter.start('request','13800000000','owner')['state'],'uncertain')
                self.assertEqual(adapter.start('request','13800000000','owner')['state'],'uncertain')
                thread.return_value.start.assert_called_once()
                with self.assertRaises(ValueError):adapter.start('request','13900000000','owner')
                with self.assertRaises(ValueError):adapter.start('request','13800000000','other-owner')
            saved=adapter.path('request').read_text()
            self.assertNotIn('13800000000',saved)
            self.assertEqual(adapter.path('request').stat().st_mode&0o777,0o600)
            with POOL_LOCK:POOL.pop((str(adapter.directory),'request'))

    def test_native_screen_requires_authentication_and_controls_require_same_origin(self):
        with tempfile.TemporaryDirectory() as d:
            app=create_app(d,platform=DemoPlatform(),auth=True);client=app.test_client()
            try:
                self.assertEqual(client.get('/api/account/sms/request/frame').status_code,401)
                with client.session_transaction() as session:session['authenticated']=True
                r=client.post('/api/account/sms/request/control',headers={'X-App-Request':'1','Origin':'https://evil.example'},json={'kind':'drag','points':[[1,1]]})
                self.assertEqual(r.status_code,403)
            finally:app.extensions['monitor'].close();app.extensions['accounts'].close()

    def test_successful_send_updates_real_coordinator_and_cancel_revokes_browser(self):
        from web.account_maintenance import Bridge
        from maintenance.store import Store
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);directory=root/'xianyu/.local/web';directory.mkdir(parents=True)
            private_json(directory/'account-maintenance.json',{'source':'xianyu','key':'fixture-key'})
            center=Store(root/'account-maintenance/.local')
            center.sync('xianyu',[{'id':'owner','external_id':'owner','name':'fixture','kind':'web',
                'status':'expired','capability':'sms','url':'https://fixture.invalid'}])
            center.bind('xianyu','owner','13800000000')
            jid=center.manual('xianyu','owner','sms');center.next_job()
            bridge=Bridge(directory);bridge.reserve(jid,'owner','owner','13800000000',{'force_login':True})
            s=NativeSession(NativePhoneLogin(directory),jid,'13800000000','owner')
            s.state='waiting';s.sent_at=12345;s.sync_waiting()
            self.assertEqual(center.job(jid)['state'],'waiting')
            self.assertEqual(center.job(jid)['expires'],12525)
            self.assertEqual(bridge.attempt(jid)['state'],'waiting')
            self.assertTrue(s.authorized_attempt())
            center.cancel(jid);self.assertFalse(s.authorized_attempt())
            s.reason='结束';s.close_pending()
            self.assertEqual(center.job(jid)['state'],'cancelled')
