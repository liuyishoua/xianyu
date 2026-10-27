from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from web.phone_login import (PhoneLoginError, create_phone_login, sms_failure_reason,
    sms_response_data, sms_response_diagnostic)
from web.phone_login_browser import BrowserPhoneLogin, official_url, browser_proxy


class BrowserPhoneTests(unittest.TestCase):
    def fixture(self, directory, result):
        adapter = BrowserPhoneLogin(directory,{'official_response_wait_seconds':0})
        context = Mock()
        context.storage_state.return_value = {'cookies': [], 'origins': []}
        page = Mock()
        response = Mock()
        response.status = 200
        response.json.return_value = {'content': {'data': result}}
        response.request.post_data = 'loginId=13800000000&ua=official-script&bx-ua=send-signature'
        page.expect_response.return_value.__enter__ = Mock(return_value=Mock(value=response))
        page.expect_response.return_value.__exit__ = Mock(return_value=False)

        @contextmanager
        def browser(state=None,config=None):
            yield context, page
        adapter.browser = Mock(side_effect=browser)
        return adapter, context, page

    def test_start_preserves_official_context_and_resumes_verified_identity(self):
        with tempfile.TemporaryDirectory() as d:
            adapter, context, page = self.fixture(d, {'smsToken': 'fixture-token'})
            self.assertEqual(adapter.start('request', '13800000000')['state'], 'waiting')
            path = adapter.path('request')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['engine'], 'browser')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            context.cookies.return_value = [{'name': 'unb', 'value': 'owner', 'domain': '.goofish.com',
                'path': '/', 'secure': True, 'expires': -1}]
            page.evaluate.side_effect = ['/newlogin/sms/login.do?appName=xianyu&fromSite=77',
                {'ua': 'fresh-official-context', 'navUserAgent': 'official-browser'},
                {'content': {'data': {'asyncUrls': ['https://www.goofish.com/session-handoff']}}}]
            context.request.get.return_value.status = 200
            context.request.get.return_value.headers = {}
            api = adapter.confirm('request', '13800000000', '123456', 'owner')
            self.assertEqual(api.session.cookies.get_dict()['unb'], 'owner')
            self.assertFalse(path.exists())
            args = page.evaluate.call_args.args[1]
            self.assertEqual(args['form']['ua'], 'fresh-official-context')
            self.assertNotIn('bx-ua', args['form'])
            self.assertEqual(args['form']['smsToken'], 'fixture-token')
            self.assertEqual(args['form']['smsCode'], '123456')
            self.assertEqual(context.request.get.call_count, 1)

    def test_send_challenge_preserves_specific_error_and_never_marks_waiting(self):
        for data in ({'isCheckCodeShowed': True}, {'resultCode': 1007, 'titleMsg': '操作频繁，请稍后重试'}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as d:
                adapter, context, page = self.fixture(d, data)
                result = adapter.start('request', '13800000000')
                self.assertEqual(result['state'], 'needs_user')
                self.assertEqual(result['reason'], sms_failure_reason(data, '发码'))
                self.assertFalse(adapter.path('request').exists())
                context.storage_state.assert_not_called()

    def test_transport_failure_after_click_is_uncertain_without_resending(self):
        with tempfile.TemporaryDirectory() as d:
            adapter, context, page = self.fixture(d, {})
            page.locator.return_value.click.side_effect = TimeoutError()
            self.assertEqual(adapter.start('request', '13800000000')['state'], 'uncertain')
            page.locator.return_value.click.assert_called_once()
            self.assertFalse(adapter.path('request').exists())
            diagnostic=json.loads(adapter.path('request').with_suffix('.diagnostic.json').read_text())
            self.assertEqual(diagnostic['failed_phase'],'send_response')
            self.assertTrue(diagnostic['send_intent'])

    def test_browser_proxy_is_explicit_and_never_falls_back_to_direct_launch(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=BrowserPhoneLogin(d,{'browser_proxy':'socks5://127.0.0.1:18890'})
            with patch('playwright.sync_api.sync_playwright') as driver:
                chromium=driver.return_value.__enter__.return_value.chromium
                with adapter.browser():pass
                self.assertEqual(chromium.launch.call_args.kwargs['proxy'],{'server':'socks5://127.0.0.1:18890'})
                chromium.launch.reset_mock();chromium.launch.side_effect=RuntimeError('Proxy unavailable')
                with self.assertRaises(RuntimeError):
                    with adapter.browser():pass
                chromium.launch.assert_called_once()
        for value in ('socks5://user:secret@127.0.0.1:18890','http://evil.example:8080',
                'socks5://127.0.0.1:invalid','socks5://127.0.0.1:18890/path'):
            with self.subTest(value=value),self.assertRaises(PhoneLoginError):browser_proxy({'browser_proxy':value})

    def test_confirmation_keeps_original_network_after_configuration_changes(self):
        with tempfile.TemporaryDirectory() as d:
            adapter,context,page=self.fixture(d,{'smsToken':'fixture-token'})
            adapter.config['browser_proxy']='socks5://127.0.0.1:18890'
            adapter.start('request','13800000000')
            adapter.config['browser_proxy']='socks5://127.0.0.1:19999'
            page.evaluate.side_effect=['/newlogin/sms/login.do',{'navUserAgent':'browser'},
                {'content':{'data':{'loginSuccess':True}}}]
            context.cookies.return_value=[{'name':'unb','value':'owner','domain':'.goofish.com',
                'path':'/','secure':True,'expires':-1}]
            api=adapter.confirm('request','13800000000','123456','owner')
            self.assertEqual(adapter.browser.call_args.kwargs['config']['browser_proxy'],'socks5://127.0.0.1:18890')
            self.assertEqual(api.session.proxies['https'],'socks5h://127.0.0.1:18890')
            self.assertFalse(adapter.path('request').exists())

    def test_failed_confirmation_consumes_challenge_without_saving_wrong_identity(self):
        for result in ({'isCheckCodeShowed': True}, {'smsRegToken': 'must-not-register'}, {}):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as d:
                adapter, context, page = self.fixture(d, {'smsToken': 'fixture-token'})
                adapter.start('request', '13800000000')
                page.evaluate.side_effect = ['/newlogin/sms/login.do', {'navUserAgent': 'browser'},
                    {'content': {'data': result}}]
                context.cookies.return_value = [{'name': 'unb', 'value': 'another-owner', 'domain': '.goofish.com'}]
                with self.assertRaises(PhoneLoginError):
                    adapter.confirm('request', '13800000000', '123456', 'owner')
                self.assertFalse(adapter.path('request').exists())

    def test_expired_browser_challenge_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as d:
            adapter, context, page = self.fixture(d, {'smsToken': 'fixture-token'})
            adapter.start('request', '13800000000')
            path = adapter.path('request');saved = json.loads(path.read_text())
            saved['expires'] = 0;path.write_text(json.dumps(saved));adapter.browser.reset_mock()
            with self.assertRaises(ValueError):adapter.confirm('request', '13800000000', '123456', 'owner')
            adapter.browser.assert_not_called()

    def test_factory_and_official_destination_validation(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'phone-login-config.json').write_text(json.dumps({'engine': 'browser'}))
            self.assertIsInstance(create_phone_login(d), BrowserPhoneLogin)
        for url in ('https://evil.example/login', 'http://www.goofish.com/login', 'https://user@www.goofish.com/login'):
            with self.assertRaises(PhoneLoginError):official_url(url)
        with self.assertRaises(PhoneLoginError):official_url('/newlogin/sms/reg.do', '/newlogin/sms/login.do')

    def test_error_does_not_expose_phone_code_or_token(self):
        message = sms_failure_reason({'titleMsg': '手机号13800000000 验证码654321 token=abcdefghijklmnopqrstu https://example.org/private'}, '发码')
        for secret in ('13800000000', '654321', 'abcdefghijklmnopqrstu', 'https://example.org/private'):
            self.assertNotIn(secret, message)

    def test_cached_cookie_is_not_proof_of_successful_sms_login(self):
        with tempfile.TemporaryDirectory() as d:
            adapter, context, page = self.fixture(d, {'smsToken': 'fixture-token'})
            adapter.start('request', '13800000000')
            page.evaluate.side_effect = ['/newlogin/sms/login.do', {'navUserAgent': 'browser'},
                {'content': {'data': {}}}]
            context.cookies.return_value = [{'name': 'unb', 'value': 'owner', 'domain': '.goofish.com'}]
            with self.assertRaises(PhoneLoginError):adapter.confirm('request', '13800000000', '123456', 'owner')
            context.cookies.assert_not_called()

    def test_access_challenge_outside_login_envelope_is_classified_without_storing_secrets(self):
        payload = {'rgv587_flag': 'sm', 'url': 'https://passport.goofish.com/punish?token=private-token'}
        data = sms_response_data(payload)
        self.assertTrue(data['isCheckCodeShowed'])
        diagnostic = sms_response_diagnostic(payload, 200)
        self.assertTrue(diagnostic['requires_verification'])
        self.assertNotIn('private-token', json.dumps(diagnostic))
        self.assertEqual(sms_response_data({'content': {'data': {'smsToken': 'fixture'}}})['smsToken'], 'fixture')

    def test_official_followup_response_completes_send_without_second_click(self):
        with tempfile.TemporaryDirectory() as d:
            adapter, context, page = self.fixture(d, {'isCheckCodeShowed':True})
            adapter.config['official_response_wait_seconds']=1
            followup=Mock();followup.url='https://passport.goofish.com/newlogin/sms/send.do?appName=xianyu'
            followup.status=200;followup.json.return_value={'content':{'data':{'smsToken':'official-followup-token'}}}
            followup.request.post_data='loginId=13800000000&ua=official-followup-context'
            def official_callback(_):
                page.on.call_args.args[1](followup)
            page.wait_for_timeout.side_effect=official_callback
            self.assertEqual(adapter.start('request','13800000000')['state'],'waiting')
            saved=json.loads(adapter.path('request').read_text())
            self.assertEqual(saved['sms_token'],'official-followup-token')
            self.assertEqual(saved['form']['ua'],'official-followup-context')
            page.locator.return_value.click.assert_called_once()
            diagnostic=json.loads(adapter.path('request').with_suffix('.diagnostic.json').read_text())
            self.assertTrue(diagnostic['official_followup_observed']);self.assertEqual(diagnostic['response_count'],2)
            self.assertNotIn('official-followup-token',json.dumps(diagnostic))

    def test_uninitialized_official_runtime_does_not_send_sms(self):
        with tempfile.TemporaryDirectory() as d:
            adapter,context,page=self.fixture(d,{'smsToken':'must-not-be-used'})
            page.wait_for_function.side_effect=TimeoutError()
            self.assertEqual(adapter.start('request','13800000000')['state'],'needs_user')
            page.locator.return_value.click.assert_not_called()
            page.expect_response.assert_not_called()
            self.assertFalse(adapter.path('request').exists())
            diagnostic=json.loads(adapter.path('request').with_suffix('.diagnostic.json').read_text())
            self.assertEqual(diagnostic['failed_phase'],'sdk_ready')
            self.assertFalse(diagnostic['send_intent'])

    def test_official_callback_diagnostic_keeps_only_counts_and_boolean_context(self):
        with tempfile.TemporaryDirectory() as d:
            adapter,context,page=self.fixture(d,{'isCheckCodeShowed':True})
            adapter.config['diagnostic_mode']=True
            page.evaluate.return_value={'calls':1,'callbacks':0,'threw':False,'webdriver':True,
                'ua_ready':True,'umid_ready':False,'callback_token':'private-token',
                'phone':'13800000000','smsCode':'123456','cookie':'private-cookie'}
            result=adapter.start('request','13800000000')
            self.assertEqual(result['state'],'needs_user')
            diagnostic=json.loads(adapter.path('request').with_suffix('.diagnostic.json').read_text())
            self.assertEqual(diagnostic['official_check'],{'calls':1,'callbacks':0})
            self.assertTrue(diagnostic['request_context']['ua'])
            for secret in ('13800000000','123456','private-token','private-cookie','official-script'):
                self.assertNotIn(secret,json.dumps(diagnostic))
            page.locator.return_value.click.assert_called_once()

    def test_diagnostic_failure_does_not_change_successful_send_or_resend(self):
        with tempfile.TemporaryDirectory() as d:
            adapter,context,page=self.fixture(d,{'smsToken':'fixture-token'})
            adapter.config['diagnostic_mode']=True
            page.evaluate.side_effect=[None,RuntimeError('Observation unavailable')]
            self.assertEqual(adapter.start('request','13800000000')['state'],'waiting')
            page.locator.return_value.click.assert_called_once()
