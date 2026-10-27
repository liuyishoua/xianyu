import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

import requests
from web.phone_login import PhoneLoginError,create_phone_login
from web.phone_login_browser import BrowserPhoneLogin
from web.phone_login_ssh import SshPhoneLogin
from web.sms_ssh_worker import SmsWorker


class SshSmsTests(unittest.TestCase):
    def config(self,directory):
        hosts=Path(directory)/'known-hosts';hosts.write_text('fixture verified host identity')
        return {'engine':'ssh','ssh':{'host':'owned-macbook','user':'owner','known_hosts':str(hosts)}}

    def test_worker_idempotency_and_identity_binding(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=Mock();adapter.start.return_value={'state':'waiting','remote_id':'request'}
            api=Mock();api.session=requests.Session();api.session.cookies.set('unb','owner',domain='.goofish.com')
            adapter.confirm.return_value=api;worker=SmsWorker(d,adapter=adapter)
            start={'action':'start','id':'request','phone':'13800000000','expected':'owner'}
            self.assertEqual(worker.dispatch(start)['state'],'waiting')
            self.assertEqual(worker.dispatch(start)['state'],'waiting');adapter.start.assert_called_once()
            with self.assertRaises(ValueError):worker.dispatch({**start,'expected':'another-owner'})
            confirm={**start,'action':'confirm','code':'123456'}
            self.assertEqual(worker.dispatch(confirm)['state'],'confirmed')
            self.assertEqual(worker.dispatch(confirm)['state'],'confirmed');adapter.confirm.assert_called_once()
            self.assertNotIn('123456',worker.path('request').read_text())
            self.assertEqual(worker.path('request').stat().st_mode&0o777,0o600)

    def test_interrupted_remote_send_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=Mock();adapter.start.side_effect=TimeoutError();worker=SmsWorker(d,adapter=adapter)
            start={'action':'start','id':'request','phone':'13800000000','expected':'owner'}
            with self.assertRaises(TimeoutError):worker.dispatch(start)
            self.assertEqual(worker.dispatch(start)['state'],'uncertain');adapter.start.assert_called_once()

    def test_interrupted_confirmation_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=Mock();adapter.start.return_value={'state':'waiting'};adapter.confirm.side_effect=TimeoutError()
            worker=SmsWorker(d,adapter=adapter);start={'action':'start','id':'request','phone':'13800000000','expected':'owner'}
            worker.dispatch(start);confirm={**start,'action':'confirm','code':'123456'}
            self.assertEqual(worker.dispatch(confirm)['state'],'uncertain')
            self.assertEqual(worker.dispatch(confirm)['state'],'uncertain');adapter.confirm.assert_called_once()

    def test_ssh_keeps_phone_and_code_out_of_command_line_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=SshPhoneLogin(d,self.config(d));payload={'action':'confirm','id':'request','phone':'13800000000','code':'123456'}
            with patch('web.phone_login_ssh.subprocess.run',return_value=Mock(returncode=0,stdout='{"state":"waiting"}')) as run:
                self.assertEqual(adapter.invoke(payload)['state'],'waiting')
                command=run.call_args.args[0];self.assertNotIn('13800000000',' '.join(command));self.assertNotIn('123456',' '.join(command))
                self.assertIn('StrictHostKeyChecking=yes',command);self.assertEqual(json.loads(run.call_args.kwargs['input']),payload)
            with patch('web.phone_login_ssh.subprocess.run',return_value=Mock(returncode=255,stderr='secret code 123456')):
                result=adapter.invoke(payload);self.assertEqual(result['state'],'uncertain');self.assertNotIn('123456',str(result))

    def test_transport_receipt_requires_original_identity(self):
        for returned_owner,success in (('owner',True),('other-owner',False)):
            with self.subTest(owner=returned_owner),tempfile.TemporaryDirectory() as d:
                adapter=SshPhoneLogin(d,self.config(d))
                with patch.object(adapter,'invoke',return_value={'state':'waiting'}):adapter.start('request','13800000000',expected='owner')
                cookie={'name':'unb','value':returned_owner,'domain':'.goofish.com','path':'/','secure':True,'expires':None}
                with patch.object(adapter,'invoke',return_value={'state':'confirmed','session':{'cookies':[cookie],'user_agent':'native-browser'}}):
                    if success:
                        api=adapter.confirm('request','13800000000','123456','owner');self.assertEqual(api.session.cookies.get_dict()['unb'],'owner')
                    else:
                        with self.assertRaises(PhoneLoginError):adapter.confirm('request','13800000000','123456','owner')
                self.assertFalse(adapter.path('request').exists())

    def test_wrong_binding_and_expiry_do_not_invoke_remote_browser(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=SshPhoneLogin(d,self.config(d))
            with patch.object(adapter,'invoke',return_value={'state':'waiting'}):adapter.start('request','13800000000',expected='owner')
            with patch.object(adapter,'invoke') as invoke:
                with self.assertRaises(ValueError):adapter.confirm('request','13800000000','123456','another-owner')
                path=adapter.path('request');saved=json.loads(path.read_text());saved['expires']=time.time()-1;path.write_text(json.dumps(saved))
                with self.assertRaises(ValueError):adapter.confirm('request','13800000000','123456','owner')
                invoke.assert_not_called()

    def test_previously_confirmed_remote_request_does_not_bypass_server_verification(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=SshPhoneLogin(d,self.config(d))
            with patch.object(adapter,'invoke',return_value={'state':'confirmed'}):
                self.assertEqual(adapter.start('request','13800000000',expected='owner')['state'],'needs_user')

    def test_macos_browser_does_not_require_xvfb(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=BrowserPhoneLogin(d,{'headless':False})
            with patch('web.phone_login_browser.sys.platform','darwin'),patch('web.phone_login_browser.subprocess.Popen') as launch:
                with adapter.display() as env:self.assertIsNone(env)
                launch.assert_not_called()

    def test_factory_selects_only_explicit_remote_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            config=self.config(d);Path(d,'phone-login-config.json').write_text(json.dumps(config))
            self.assertIsInstance(create_phone_login(d),SshPhoneLogin)

    def test_worker_health_does_not_open_browser_or_send_sms(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=Mock();adapter.config={'browser_executable':str(Path(d)/'missing-browser')}
            result=SmsWorker(d,adapter=adapter).dispatch({'action':'health'})
            self.assertEqual(result['state'],'ready');self.assertFalse(result['browser_installed'])
            adapter.start.assert_not_called();adapter.confirm.assert_not_called()
