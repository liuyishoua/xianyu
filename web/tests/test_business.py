import tempfile
import time
import unittest
from web.app import create_app
from web.demo import DemoPlatform, seed
from web.service import Service
from web.business import Business

class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.app=create_app(self.tmp.name,platform=DemoPlatform(),auth=False)
        self.accounts=self.app.extensions['accounts']; self.s=self.accounts.primary;seed(self.s)
        second=Service(self.tmp.name,DemoPlatform('second','第二账号'),store=self.s.store);seed(second);self.accounts.services['second']=second
        self.b=self.app.extensions['business']; self.c=self.app.test_client(); self.h={'X-App-Request':'1','Origin':'http://localhost'}
        self.grant()
    def tearDown(self):
        self.accounts.close();self.tmp.cleanup()
    def grant(self,revision=1,enabled=True,ops=None,account='demo-seller'):
        return self.b.authority('goal',{'project_id':'project','revision':revision,'enabled':enabled,'expires_at':time.time()+30,'grants':[{'account_id':account,'operations':ops or ['product.read','message.read','dm.reply'],'scope':{'conversation_ids':['900000']}}]})
    def payload(self,op='product.read',**change):
        p={'origin':'headquarters','request_id':'request-0001','goal_id':'goal','project_id':'project','action_id':'action','account_id':'demo-seller','operation':op,'input':{},'authorization_revision':1};p.update(change);return p
    def wait(self,t):
        for _ in range(100):
            self.b.tick();r=self.b.get(t['id'])
            if r['status'] in ('succeeded','cancelled','failed','uncertain'):return r
            time.sleep(.01)
        self.fail('task timeout')
    def test_cache_task_dedup_and_hash(self):
        p=self.payload();t=self.b.submit(p);self.assertEqual(t['id'],self.b.submit(p)['id'])
        with self.assertRaises(ValueError):self.b.submit({**p,'input':{'item_id':'1000000001'}})
        result=self.wait(t);self.assertEqual(result['status'],'succeeded');self.assertEqual(result['result']['evidence']['source'],'authenticated_workbench_cache')
        events=self.b.events();self.assertTrue(all(x['project_id']=='project' and x['task_id']==t['id'] for x in events['items']))
        self.assertEqual(self.b.events(events['cursor'])['items'],[])
    def test_publication_preparation_is_owned_and_redacts_storage_paths(self):
        self.s.store.put(self.s.uid(),'media',{'filename':'private.png','width':100,'height':100,'url':'/owned'},'owned-media')
        self.s.store.put('second','media',{'filename':'foreign.png','width':100},'foreign-media')
        self.s.store.put(self.s.uid(),'settings',{'items':[{'city':'真实城市','poi':'真实地点'}]},'locations')
        draft=self.s.draft({'description':'准备中','media_ids':['owned-media']})
        result=self.wait(self.b.submit(self.payload()))['result']['evidence']['publication_context']
        self.assertIn(draft['id'],[d['id'] for d in result['drafts']])
        self.assertEqual([m['id'] for m in result['media']],['owned-media'])
        self.assertNotIn('filename',result['media'][0])
        self.assertIn('真实城市',result['locations'][0]['label'])
        self.assertTrue(result['draft_can_be_saved_incomplete'])

    def test_cross_account_and_scope_denied(self):
        with self.assertRaises(ValueError):self.b.submit(self.payload(account_id='second'))
        with self.assertRaises(ValueError):self.b.submit(self.payload('dm.reply',input={'conversation_id':'900001','text':'您好'}))
    def test_revoke_queued_before_execution(self):
        t=self.b.submit(self.payload('dm.reply',input={'conversation_id':'900000','text':'您好'}));self.grant(2,False)
        self.assertEqual(self.wait(t)['status'],'cancelled')
        self.assertFalse(any(j['kind']=='send' for j in self.s.store.jobs(self.s.uid())))
    def test_lease_expiry_and_same_revision_immutability(self):
        with self.assertRaises(ValueError):self.grant(1,ops=['draft.create'])
        with self.assertRaises(ValueError):self.b.authority('other',{'project_id':'project','revision':1,'enabled':True,'expires_at':time.time()+60,'grants':[]})
        t=self.b.submit(self.payload());self.b.clock=lambda:time.time()+60
        self.assertEqual(self.wait(t)['status'],'cancelled')
    def test_queued_restart_recovers(self):
        t=self.b.submit(self.payload());self.b=Business(self.accounts,self.app.extensions['monitor'])
        self.assertEqual(self.wait(t)['status'],'succeeded')
    def test_original_unknown_write_not_retried(self):
        calls=[]
        def socket(op,**kw):calls.append(op);raise TimeoutError()
        self.s.platform.socket=socket
        t=self.b.submit(self.payload('dm.reply',input={'conversation_id':'900000','text':'您好'}));r=self.wait(t)
        self.assertEqual(r['status'],'uncertain');self.assertIsNone(r['result']['platform_action_performed']);self.b=Business(self.accounts,self.app.extensions['monitor']);self.b.tick()
        self.assertEqual(calls,['send'])
    def test_execution_guard_checks_after_job_queued(self):
        with self.s.lock:
            t=self.b.submit(self.payload('dm.reply',input={'conversation_id':'900000','text':'您好'}));self.b.tick();self.grant(2,False)
        self.assertEqual(self.wait(t)['status'],'failed')
        self.assertFalse(any(x.get('local_sent') for x in self.s.store.list(self.s.uid(),'message')))
    def test_new_message_only_linked_conversation(self):
        t=self.b.submit(self.payload('message.read',input={'conversation_id':'900000'}));self.wait(t)
        self.s.store.put(self.s.uid(),'message',{'id':'fresh','cid':'900000','text':'你好','outgoing':False,'timestamp':time.time()})
        self.s.store.put(self.s.uid(),'message',{'id':'unbound','cid':'900001','text':'你好','outgoing':False,'timestamp':time.time()})
        events=[e for e in self.b.events()['items'] if e['kind']=='message.received']
        self.assertEqual([e['payload']['message']['id'] for e in events],['fresh']);self.assertEqual(events[0]['task_id'],t['id'])
    def test_auth_origin_and_no_account_dispatch(self):
        self.assertEqual(self.c.post('/api/business/tasks',json=self.payload()).status_code,403)
        r=self.c.post('/api/business/tasks',json=self.payload(),headers={**self.h,'X-Account-ID':'second','X-Execution-Account':'second'})
        self.assertEqual(r.status_code,202,r.json);self.assertEqual(r.json['account_id'],'demo-seller')
    def created_grant(self,goal='goal',account='demo-seller',include=True):
        return self.b.authority(goal,{'project_id':'project','revision':2,'enabled':True,'expires_at':time.time()+30,'grants':[{'account_id':account,'operations':['draft.create','product.publish','dm.initiate','dm.reply'],'scope':{'resource_ids':['1000000001'],'contact_ids':['800000'],'include_goal_created':include}}]})
    def new_draft(self):
        self.s.store.put(self.s.uid(),'media',{'id':'media','filename':'sample.png'})
        (self.s.media_dir/'sample.png').write_bytes(b'demo-local-only')
        p=self.payload('draft.create',request_id='create-draft',authorization_revision=2,input={'description':'测试商品','price':'10','media_ids':['media'],'location_index':0})
        r=self.wait(self.b.submit(p));self.assertEqual(r['status'],'succeeded')
        return r['result']['evidence']['draft']['id']
    def test_created_draft_publish_without_new_grant(self):
        self.created_grant();draft=self.new_draft()
        p=self.payload('product.publish',request_id='publish-draft',authorization_revision=2,input={'draft_id':draft})
        r=self.wait(self.b.submit(p));self.assertEqual(r['status'],'succeeded');self.assertTrue(r['result']['evidence']['item_id'])
    def test_created_draft_two_goal_account_isolation(self):
        self.created_grant();draft=self.new_draft();self.created_grant('other');self.created_grant('second-goal','second')
        for goal,account in [('other','demo-seller'),('second-goal','second')]:
            with self.assertRaises(ValueError):self.b.submit(self.payload('product.publish',request_id='isolation-'+goal,goal_id=goal,account_id=account,authorization_revision=2,input={'draft_id':draft}))
    def test_created_scope_flag_not_all_existing_drafts(self):
        self.created_grant(include=False);draft=self.new_draft()
        with self.assertRaises(ValueError):self.b.submit(self.payload('product.publish',request_id='denied-created',authorization_revision=2,input={'draft_id':draft}))
        self.created_grant('other')
        existing=self.s.store.list(self.s.uid(),'draft')[0]['id']
        with self.assertRaises(ValueError):self.b.submit(self.payload('product.publish',request_id='denied-existing',goal_id='other',authorization_revision=2,input={'draft_id':existing}))
    def test_created_conversation_keeps_allowed_contact(self):
        self.created_grant();self.s.platform.item=lambda item:{'id':item,'seller_id':'800000'}
        create=self.payload('dm.initiate',request_id='contact-create',authorization_revision=2,input={'item_id':'1000000001','peer_id':'800000'})
        r=self.wait(self.b.submit(create));self.assertEqual(r['status'],'succeeded');cid=r['result']['evidence']['id']
        reply=self.payload('dm.reply',request_id='contact-reply',authorization_revision=2,input={'conversation_id':cid,'text':'您好'})
        self.assertEqual(self.wait(self.b.submit(reply))['status'],'succeeded')
        with self.assertRaises(ValueError):self.b.submit({**create,'request_id':'different-peer','input':{'item_id':'1000000001','peer_id':'800001'}})

    def test_revoked_created_contact_cannot_reply(self):
        self.created_grant();self.s.platform.item=lambda item:{'id':item,'seller_id':'800000'}
        r=self.wait(self.b.submit(self.payload('dm.initiate',request_id='create-contact',authorization_revision=2,input={'item_id':'1000000001','peer_id':'800000'})))
        cid=r['result']['evidence']['id']
        self.b.authority('goal',{'project_id':'project','revision':3,'enabled':True,'expires_at':time.time()+30,'grants':[{'account_id':'demo-seller','operations':['dm.reply'],'scope':{'include_goal_created':True,'contact_ids':[]}}]})
        with self.assertRaises(ValueError):self.b.submit(self.payload('dm.reply',request_id='revoked-peer',authorization_revision=3,input={'conversation_id':cid,'text':'您好'}))
    def test_model_poll_transport_failure_retains_original_event(self):
        from web.monitoring import DemoChannel
        from web.channel_client import ChannelError
        class Intermittent(DemoChannel):
            def __init__(self):self.reads=0;self.sends=0
            def send(self,*args,**kwargs):self.sends+=1;return super().send(*args,**kwargs)
            def status(self,event_id,account_id):
                self.reads+=1
                if self.reads==1:raise ChannelError('temporary read failure')
                return DemoChannel.send(self,account_id,event_id)
        client=Intermittent();self.app.extensions['monitor'].client=lambda service:client
        self.grant(2,ops=['reply.draft']);t=self.b.submit(self.payload('reply.draft',authorization_revision=2,input={'conversation_id':'900000'}))
        self.b.tick();self.b.tick();self.assertEqual(self.b.get(t['id'])['status'],'waiting_model')
        self.b.tick();self.assertEqual(self.b.get(t['id'])['status'],'succeeded');self.assertEqual(client.sends,1)

    def test_content_model_cannot_smuggle_conversation_scope(self):
        self.grant(2,ops=['content.analyze'])
        with __import__('unittest.mock',fromlist=['patch']).patch('web.channel_bridge.submit_analysis',return_value=({'id':'event'},202)) as submit:
            self.b.submit(self.payload('content.analyze',authorization_revision=2,input={'conversation_id':'900001','draft_description':'商品事实','message_id':'forged'}))
            self.b.tick();data=submit.call_args.args[2]
            self.assertNotIn('conversation_id',data);self.assertEqual(data['message_id'],'business-request-0001')

    def test_real_channel_succeeded_protocol_and_invalid_outputs(self):
        from web.monitoring import DemoChannel
        self.grant(2,ops=['reply.draft','content.analyze'])
        fixtures=[('reply.draft',{'text':'JSON envelope','structured':{'decision':'reply','reply_text':'您好'}},'succeeded'),
          ('content.analyze',{'text':'JSON envelope','structured':{'reply':'真实商品内容结论'}},'succeeded'),
          ('reply.draft',{'text':'JSON envelope','structured':{'status':'needs_context','reply_text':'需要资料'}},'failed'),
          ('content.analyze',{'text':'JSON envelope','structured':{'status':'blocked','reply':'不可用'}},'failed'),
          ('content.analyze',{'text':'JSON envelope','structured':{'status':'error','summary':'失败'}},'failed'),
          ('reply.draft',{'text':'JSON envelope','structured':{'summary':'并没有最终回复'}},'failed'),
          ('content.analyze',{'text':'JSON envelope','structured':{'summary':'只有摘要没有内容'}},'failed'),
          ('reply.draft',{'text':'','structured':None},'failed'),
          ('content.analyze',{'status':'needs_context','text':'未能读取资料'},'failed')]
        for index,(op,output,expected) in enumerate(fixtures):
            class Protocol(DemoChannel):
                def status(self,event_id,account_id):return {'id':event_id,'state':'succeeded','result':{'output':output}}
            client=Protocol();self.app.extensions['monitor'].client=lambda service:client
            inp={'conversation_id':'900000'} if op=='reply.draft' else {'draft_description':'已知事实'}
            result=self.wait(self.b.submit(self.payload(op,request_id='protocol-'+str(index),authorization_revision=2,input=inp)))
            self.assertEqual(result['status'],expected,(index,result));self.assertFalse(result['result']['platform_action_performed'])
        self.assertFalse(any(j['kind']=='send' for j in self.s.store.jobs(self.s.uid())))

    def test_grant_expiry_does_not_stop_other_account(self):
        now=time.time()
        authority={'project_id':'project','revision':2,'enabled':True,'expires_at':now+30,'grants':[{'account_id':'demo-seller','operations':['product.read'],'expires_at':now+5,'scope':{}},{'account_id':'second','operations':['product.read'],'expires_at':now+60,'scope':{}}]}
        self.b.authority('goal',authority)
        first=self.b.submit(self.payload(request_id='first-account',authorization_revision=2))
        second=self.b.submit(self.payload(request_id='second-account',account_id='second',authorization_revision=2))
        self.b.clock=lambda:now+6
        self.assertEqual(self.wait(first)['status'],'cancelled')
        self.assertEqual(self.wait(second)['status'],'succeeded')
        self.b.authority('goal',{**authority,'expires_at':now+31})
        with self.assertRaises(ValueError):self.b.submit(self.payload(request_id='expired-account',authorization_revision=2))

    def test_project_binding_denied(self):
        with self.assertRaises(ValueError):self.b.submit(self.payload(project_id='another-project'))

    def test_outbox_and_task_state_commit_atomically(self):
        original=self.b.emit_status
        def crash(db,ident):
            original(db,ident)
            raise RuntimeError('simulated crash before commit')
        self.b.emit_status=crash
        with self.assertRaises(RuntimeError):self.b.submit(self.payload())
        with self.s.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM business_tasks').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM business_events').fetchone()[0],0)
        self.b.emit_status=original;t=self.b.submit(self.payload())
        self.b.emit_status=crash
        with self.assertRaises(RuntimeError):self.b.update(t['id'],'running')
        self.b=Business(self.accounts,self.app.extensions['monitor'])
        self.assertEqual(self.b.get(t['id'])['status'],'queued')
        self.assertEqual([x['payload']['status'] for x in self.b.events()['items']],['queued'])
        r=self.wait(t);self.assertEqual(r['status'],'succeeded')
        self.assertEqual(self.b.events()['items'][-1]['payload']['status'],'succeeded')

    def test_model_draft_is_not_platform_send(self):
        self.grant(2,ops=['reply.draft'])
        t=self.b.submit(self.payload('reply.draft',authorization_revision=2,input={'conversation_id':'900000'}))
        r=self.wait(t);self.assertEqual(r['status'],'succeeded')
        self.assertEqual(r['result']['source'],'channel_model_draft');self.assertFalse(r['result']['platform_action_performed'])
        self.assertTrue(r['result']['evidence']['output']['text'])
        self.assertFalse(any(j['kind']=='send' for j in self.s.store.jobs(self.s.uid())))

    def test_cancel_and_unavailable(self):
        t=self.b.submit(self.payload());self.assertEqual(self.b.cancel(t['id'])['status'],'cancelled')
        self.grant(2,ops=['public.search'])
        with self.assertRaises(ValueError):self.b.submit(self.payload('public.search',authorization_revision=2))
