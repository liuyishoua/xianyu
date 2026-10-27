import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
import requests
from web.phone_login import PhoneLogin,rsa_mobile,create_phone_login
from web.platform import PlatformError

class PhoneTests(unittest.TestCase):
 def session(self,data):
  s=Mock();s.headers={};s.cookies=requests.cookies.RequestsCookieJar()
  html='window.viewData = '+json.dumps({'loginFormData':{'appName':'xianyu','_csrf_token':'fixture'}})+'; window.viewConfig = {};'
  s.get.return_value=Mock(text=html);s.post.return_value.json.return_value={'content':{'data':data}}
  return s
 def test_challenge_is_not_waiting(self):
  with tempfile.TemporaryDirectory() as d:
   s=self.session({'isCheckCodeShowed':True});adapter=PhoneLogin(d,lambda:s)
   self.assertEqual(adapter.start('request','13800000000')['state'],'needs_user');self.assertFalse(adapter.path('request').exists());self.assertEqual(s.post.call_count,1)

 def test_real_response_diagnostic_distinguishes_challenge_without_secrets(self):
  with tempfile.TemporaryDirectory() as d:
   s=self.session({});s.post.return_value.status_code=200
   s.post.return_value.json.return_value={'ret':['RGV587_ERROR::SM'],'data':{'url':'https://passport.goofish.com/punish?private=fixture-secret'}}
   adapter=PhoneLogin(d,lambda:s,{'diagnostic_mode':True})
   self.assertEqual(adapter.start('request','13800000000')['state'],'needs_user')
   p=adapter.path('request').with_suffix('.diagnostic.json');diag=json.loads(p.read_text())
   self.assertTrue(diag['requires_verification']);self.assertFalse(diag['sms_success_observed'])
   self.assertEqual(diag['http_status'],200)
   for secret in ('13800000000','fixture-secret','passport.goofish.com'):self.assertNotIn(secret,p.read_text())
 def test_waiting_survives_restart_and_wrong_identity_is_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   s=self.session({'smsToken':'fixture'});adapter=PhoneLogin(d,lambda:s)
   self.assertEqual(adapter.start('request','13800000000')['state'],'waiting')
   self.assertEqual(adapter.path('request').stat().st_mode & 0o777,0o600)
   s.cookies.set('unb','other-owner',domain='.goofish.com');s.post.return_value.json.return_value={'content':{'data':{'loginSuccess':True}}}
   with self.assertRaises(PlatformError):PhoneLogin(d,lambda:s).confirm('request','13800000000','123456','expected-owner')
   self.assertFalse(adapter.path('request').exists())
 def test_expired_request_does_not_submit(self):
  with tempfile.TemporaryDirectory() as d:
   s=self.session({'smsToken':'fixture'});adapter=PhoneLogin(d,lambda:s);adapter.start('request','13800000000')
   p=adapter.path('request');data=json.loads(p.read_text());data['expires']=0;p.write_text(json.dumps(data));s.post.reset_mock()
   with self.assertRaises(ValueError):adapter.confirm('request','13800000000','123456','owner')
   s.post.assert_not_called()

 def test_http_send_and_confirm_keep_configured_home_network(self):
  with tempfile.TemporaryDirectory() as d:
   Path(d,'phone-login-config.json').write_text(json.dumps({'engine':'requests','browser_proxy':'socks5://127.0.0.1:18890'}))
   adapter=create_phone_login(d);s=self.session({'smsToken':'fixture'});s.proxies={};adapter.session_factory=lambda:s
   self.assertEqual(adapter.start('request','13800000000')['state'],'waiting')
   self.assertEqual(s.proxies,{'http':'socks5h://127.0.0.1:18890','https':'socks5h://127.0.0.1:18890'})
   self.assertFalse(s.trust_env)
   adapter.config={'browser_proxy':'socks5://127.0.0.1:19990'};s.proxies={}
   s.cookies.set('unb','owner',domain='.goofish.com');s.post.return_value.json.return_value={'content':{'data':{'loginSuccess':True}}}
   adapter.confirm('request','13800000000','123456','owner')
   self.assertEqual(s.proxies['https'],'socks5h://127.0.0.1:18890')

 def test_official_rsa_form_encryption_round_trips(self):
  import subprocess
  with tempfile.TemporaryDirectory() as d:
   key=Path(d)/'test-private.pem'
   subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:1024','-out',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   modulus=subprocess.check_output(['openssl','rsa','-in',str(key),'-noout','-modulus'],stderr=subprocess.DEVNULL).decode().strip().split('=',1)[1]
   first=rsa_mobile('13800000000',modulus,'10001');second=rsa_mobile('13800000000',modulus,'10001');self.assertNotEqual(first,second)
   cipher=bytes.fromhex(first).rjust(128,b'\x00')
   plain=subprocess.check_output(['openssl','pkeyutl','-decrypt','-inkey',str(key),'-pkeyopt','rsa_padding_mode:pkcs1'],input=cipher,stderr=subprocess.DEVNULL)
   self.assertEqual(plain,b'13800000000')

 def test_official_boolean_form_values_match_browser_wire_encoding(self):
  from urllib.parse import parse_qs
  with tempfile.TemporaryDirectory() as d:
   s=self.session({'smsToken':'fixture'})
   s.get.return_value.text='window.viewData = '+json.dumps({'loginFormData':{'mainPage':False,'isMobile':False,'fixtureTrue':True}})+'; window.viewConfig = {};'
   adapter=PhoneLogin(d,lambda:s);adapter.start('request','13800000000')
   s.cookies.set('unb','owner',domain='.goofish.com');s.post.return_value.json.return_value={'content':{'data':{'loginSuccess':True}}}
   adapter.confirm('request','13800000000','123456','owner')
   for call in s.post.call_args_list:
    body=requests.Request('POST','https://passport.goofish.com/',data=call.kwargs['data']).prepare().body
    fields=parse_qs(body)
    self.assertEqual(fields['mainPage'],['false']);self.assertEqual(fields['isMobile'],['false']);self.assertEqual(fields['fixtureTrue'],['true'])
