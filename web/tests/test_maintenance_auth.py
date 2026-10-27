import json,tempfile,unittest
from pathlib import Path
from web.app import create_app
from web.demo import DemoPlatform

class MaintenanceAuthTests(unittest.TestCase):
 def test_machine_auth_does_not_bypass_identity_or_demo(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'account-maintenance.json').write_text(json.dumps({'key':'fixture-key','source':'xianyu'}))
   app=create_app(p,platform=DemoPlatform(),auth=True);client=app.test_client()
   self.assertEqual(client.get('/maintenance/accounts').status_code,401)
   self.assertEqual(client.get('/maintenance/accounts',headers={'Authorization':'Bearer fixture-key','Origin':'https://evil.example'}).status_code,401)
   h={'Authorization':'Bearer fixture-key'};r=client.get('/maintenance/accounts',headers=h)
   self.assertEqual(r.status_code,200);a=r.json['items'][0]
   self.assertEqual(client.post('/maintenance/start',headers=h,json={'request_id':'test','account_id':a['id'],'external_id':a['id'],'phone':'13800000000'}).status_code,400)
   app.extensions['accounts'].close()

 def test_listener_repair_respects_disabled_config_and_unblocks_authorized_check(self):
  from unittest.mock import Mock
  from web.platform import Platform,private_json
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'account-maintenance.json').write_text(json.dumps({'key':'fixture-key','source':'xianyu'}))
   session=p/'session.json';private_json(session,{'device_id':'fixture','cookies':[{'name':'unb','value':'fixture-owner','domain':'.goofish.com','path':'/'}]})
   platform=Mock(spec=Platform);platform.demo=False;platform.session_path=session
   platform.identity.return_value={'id':'fixture-owner','name':'fixture','status':'connected'};platform.check.return_value={'can_message':True}
   app=create_app(p,platform=platform,auth=True);client=app.test_client();body={'account_id':'fixture-owner','external_id':'fixture-owner','component':'listener.dm'};h={'Authorization':'Bearer fixture-key'}
   self.assertEqual(client.post('/maintenance/repair-listener',headers=h,json=body).json['state'],'inactive');platform.check.assert_not_called()
   service=app.extensions['workspace'];service.store.put('fixture-owner','monitor',{'enabled':True,'blocked_auth':True,'automatic':False,'route_id':'','agent_explicit_empty':True},'dm')
   self.assertEqual(client.post('/maintenance/repair-listener',headers=h,json=body).json['state'],'connected')
   restored=service.store.get('fixture-owner','monitor','dm')
   self.assertFalse(restored['blocked_auth']);self.assertEqual(restored['state'],'waiting')
   self.assertTrue(service.store.get('fixture-owner','settings','account-check')['can_message'])
   platform.socket.assert_not_called();app.extensions['monitor'].close();app.extensions['accounts'].close()

 def test_sms_confirmation_restores_only_verified_account_and_preserves_listener_switches(self):
  from unittest.mock import Mock,patch
  from web.platform import Platform,PlatformError,private_json
  cases=[('success',None,True,'confirmed'),('verification',PlatformError('USER_VALIDATE'),True,'needs_user'),
         ('uncertain',TimeoutError(),True,'uncertain'),('no_message_auth',None,False,'needs_user')]
  for name,failure,can_message,expected in cases:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as d:
    p=Path(d);(p/'account-maintenance.json').write_text(json.dumps({'key':'fixture-key'}))
    session=p/'session.json';private_json(session,{'device_id':'fixture','cookies':[{'name':'unb','value':'fixture-owner','domain':'.goofish.com','path':'/'}]})
    platform=Mock(spec=Platform);platform.demo=False;platform.session_path=session
    platform.identity.return_value={'id':'fixture-owner','name':'fixture','status':'connected'}
    platform.check.return_value={'can_message':can_message};platform.check.side_effect=failure
    with patch('web.account_maintenance.create_phone_login') as login:
     login.return_value.start.return_value={'state':'waiting','remote_id':'test'}
     app=create_app(p,platform=platform,auth=True);client=app.test_client();service=app.extensions['workspace']
     try:
      for uid in ('fixture-owner','other-owner'):
       service.store.put(uid,'settings',{'can_message':False},'account-check')
       for surface,enabled in (('dm',True),('comment',False)):
        service.store.put(uid,'monitor',{'enabled':enabled,'blocked_auth':True,'state':'error','error':'SESSION_EXPIRED','last_poll':100,'automatic':False,'route_id':'','agent_explicit_empty':True},surface)
      body={'request_id':'test','account_id':'fixture-owner','external_id':'fixture-owner','phone':'13800000000'};h={'Authorization':'Bearer fixture-key'}
      self.assertEqual(client.post('/maintenance/start',headers=h,json=body).json['state'],'waiting')
      with patch.object(app.extensions['accounts'],'register') as register:
       payload={**body,'code':'123456'}
       self.assertEqual(client.post('/maintenance/confirm',headers=h,json=payload).json['state'],expected)
       self.assertEqual(client.post('/maintenance/confirm',headers=h,json=payload).json['state'],expected)
       register.assert_called_once_with(login.return_value.confirm.return_value,expected='fixture-owner')
       login.return_value.confirm.assert_called_once_with('test','13800000000','123456','fixture-owner')
      for surface,enabled in (('dm',True),('comment',False)):
       config=service.store.get('fixture-owner','monitor',surface)
       self.assertEqual(config['enabled'],enabled)
       self.assertEqual(config['blocked_auth'],expected!='confirmed')
       self.assertEqual(config['state'],('waiting' if enabled else 'stopped') if expected=='confirmed' else 'error')
       if expected=='confirmed':self.assertEqual(config['error'],'');self.assertIsNone(config['last_poll'])
       self.assertTrue(service.store.get('other-owner','monitor',surface)['blocked_auth'])
      self.assertEqual(service.store.get('fixture-owner','settings','account-check')['can_message'],expected=='confirmed')
      platform.socket.assert_not_called()
     finally:app.extensions['monitor'].close();app.extensions['accounts'].close()
