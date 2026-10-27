import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from web.app import create_app
from web.platform import Platform,PlatformError,private_json
from web.session_import import browser_session


OWNER='1111111111'
COOKIE='unb='+OWNER+'; cookie2=fixture-private; _m_h5_tk=fixture-token_99999'


class SessionImportTests(unittest.TestCase):
    def test_cookie_parser_rejects_wrong_owner_missing_session_and_duplicates(self):
        for value in (None,'unb=2222222222; cookie2=fixture; _m_h5_tk=fixture',
                'unb='+OWNER,'unb='+OWNER+'; unb='+OWNER,
                COOKIE+'\nsecret','x'*65537):
            with self.subTest(value_type=type(value).__name__),self.assertRaises(ValueError):
                browser_session(value,OWNER)

    def fixture(self,directory,auth=False):
        path=Path(directory)/'session.json'
        private_json(path,{'device_id':'fixture-device','cookies':[{'name':'unb','value':OWNER,'domain':'.goofish.com','path':'/'}]})
        platform=Mock(spec=Platform);platform.demo=False;platform.session_path=path
        platform.identity.return_value={'id':OWNER,'name':'fixture','status':'connected'}
        platform.check.return_value={'can_message':True}
        app=create_app(directory,platform=platform,auth=auth)
        self.addCleanup(app.extensions['monitor'].close)
        self.addCleanup(app.extensions['accounts'].close)
        service=app.extensions['accounts'].resolve(OWNER)
        for uid in (OWNER,'2222222222'):
            for surface,enabled in (('dm',True),('comment',False)):
                service.store.put(uid,'monitor',{'enabled':enabled,'blocked_auth':True,'state':'error',
                    'error':'SESSION_EXPIRED','last_poll':100,'automatic':False,'route_id':'',
                    'agent_explicit_empty':True},surface)
        return app,service

    def test_verified_import_restores_only_original_enabled_listeners_and_erases_staging(self):
        with tempfile.TemporaryDirectory() as d:
            app,service=self.fixture(d)
            api=browser_session(COOKIE,OWNER)
            with patch('web.session_import.Platform') as probe:
                probe.return_value.check.return_value={'can_message':True}
                probe.return_value.api.return_value=api
                r=app.test_client().post('/api/accounts/'+OWNER+'/browser-session',
                    headers={'X-App-Request':'1'},json={'cookie':COOKIE})
            self.assertEqual(r.status_code,200);self.assertEqual(r.json['state'],'connected')
            self.assertFalse(list(Path(d).glob('.session-import-*')))
            for surface,enabled in (('dm',True),('comment',False)):
                config=service.store.get(OWNER,'monitor',surface)
                self.assertFalse(config['blocked_auth']);self.assertEqual(config['enabled'],enabled)
                self.assertEqual(config['state'],'waiting' if enabled else 'stopped')
                self.assertTrue(service.store.get('2222222222','monitor',surface)['blocked_auth'])
            service.platform.socket.assert_not_called()
            self.assertNotIn('fixture-private',r.get_data(as_text=True))

    def test_failed_staging_check_keeps_original_session_and_masks_error(self):
        with tempfile.TemporaryDirectory() as d:
            app,service=self.fixture(d);original=service.platform.session_path.read_bytes()
            with patch('web.session_import.Platform') as probe:
                probe.return_value.check.side_effect=PlatformError('private-cookie fixture-private')
                r=app.test_client().post('/api/accounts/'+OWNER+'/browser-session',
                    headers={'X-App-Request':'1'},json={'cookie':COOKIE})
            self.assertEqual(r.status_code,400)
            self.assertEqual(service.platform.session_path.read_bytes(),original)
            self.assertTrue(service.store.get(OWNER,'monitor','dm')['blocked_auth'])
            self.assertNotIn('fixture-private',r.get_data(as_text=True))
            self.assertFalse(list(Path(d).glob('.session-import-*')))

    def test_wrong_identity_never_probes_or_replaces_session(self):
        with tempfile.TemporaryDirectory() as d:
            app,service=self.fixture(d);original=service.platform.session_path.read_bytes()
            with patch('web.session_import.Platform') as probe:
                r=app.test_client().post('/api/accounts/'+OWNER+'/browser-session',
                    headers={'X-App-Request':'1'},json={'cookie':COOKIE.replace(OWNER,'2222222222')})
                probe.assert_not_called()
            self.assertEqual(r.status_code,400);self.assertEqual(service.platform.session_path.read_bytes(),original)

    def test_import_requires_workspace_auth_and_same_origin(self):
        with tempfile.TemporaryDirectory() as d:
            app,_=self.fixture(d,auth=True);client=app.test_client()
            r=client.post('/api/accounts/'+OWNER+'/browser-session',headers={'X-App-Request':'1'},json={'cookie':COOKIE})
            self.assertEqual(r.status_code,401)
            with client.session_transaction() as session:session['authenticated']=True
            r=client.post('/api/accounts/'+OWNER+'/browser-session',
                headers={'X-App-Request':'1','Origin':'https://evil.example'},json={'cookie':COOKIE})
            self.assertEqual(r.status_code,403)
