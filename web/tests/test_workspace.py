import io
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from PIL import Image
from web.app import create_app
from web.demo import DemoPlatform, seed
from web.platform import Platform


class CountingPlatform(DemoPlatform):
    def __init__(self): self.sends=0; self.publishes=0; self.fail_publish=False; self.uid='demo-seller'
    def identity(self): return {'id':self.uid,'name':'测试账号','status':'connected'}
    def socket(self, operation, **kwargs):
        if operation=='send': self.sends+=1
        return super().socket(operation,**kwargs)
    def publish(self,*args):
        self.publishes+=1
        if self.fail_publish: raise TimeoutError()
        return super().publish(*args)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.platform=CountingPlatform()
        self.app=create_app(self.temp.name,platform=self.platform,auth=False)
        self.app.config['TESTING']=True;self.s=self.app.extensions['workspace'];seed(self.s);self.client=self.app.test_client()
        self.headers={'X-App-Request':'1','Origin':'http://localhost'}
    def tearDown(self):
        self.s.executor.shutdown(wait=True);self.temp.cleanup()
    def post(self,path,data=None):return self.client.post('/api'+path,json=data or {},headers=self.headers)
    def wait(self,response):
        self.assertEqual(response.status_code,202,response.json)
        jid=response.json['id']
        for _ in range(100):
            r=self.client.get('/api/jobs/'+jid).json
            if r['status'] not in ('pending','running'):return r
            time.sleep(.01)
        self.fail('job timeout')
    def rid(self):return uuid.uuid4().hex
    def upload(self):
        b=io.BytesIO();Image.new('RGB',(20,20),'yellow').save(b,'PNG');b.seek(0)
        r=self.client.post('/api/media',data={'file':(b,'test.png')},headers=self.headers)
        self.assertEqual(r.status_code,200,r.json);return r.json['id']
    def draft(self,**kwargs):
        data={'description':'测试商品','price':'10.01','original_price':'0','delivery':'无需邮寄','postage':'0',
              'self_pickup':False,'location_index':0,'media_ids':[self.upload()]}
        data.update(kwargs);r=self.post('/drafts',data);self.assertEqual(r.status_code,200,r.json);return r.json
    def test_reads_do_not_write_platform(self):
        for path in ['/overview','/products','/conversations','/drafts','/templates','/account','/jobs','/locations']:
            self.assertEqual(self.client.get('/api'+path).status_code,200)
        self.assertEqual((self.platform.sends,self.platform.publishes),(0,0))
    def test_origin_and_request_header_required(self):
        self.assertEqual(self.client.post('/api/templates',json={'title':'t','text':'x'}).status_code,403)
        r=self.client.post('/api/templates',json={'title':'t','text':'x'},headers={'X-App-Request':'1','Origin':'https://attacker.example'})
        self.assertEqual(r.status_code,403)
    def test_draft_is_local_only(self):
        d=self.draft();self.assertEqual(d['status'],'draft');self.assertEqual(self.platform.publishes,0)
    def test_publish_duplicate_request_once(self):
        d=self.draft();payload={'request_id':self.rid()};path='/drafts/'+d['id']+'/publish'
        first=self.post(path,payload);second=self.post(path,payload)
        self.assertEqual(first.json['id'],second.json['id']);self.assertEqual(self.wait(first)['status'],'completed')
        self.assertEqual(self.platform.publishes,1)
        self.assertEqual(self.post(path,{'request_id':self.rid()}).status_code,400)
    def test_non_shipping_service_does_not_invent_location(self):
        d=self.draft(delivery='无需邮寄',location_index=None)
        self.app.extensions['workspace'].store.put('demo-seller','settings',{'items':[]},'locations')
        job=self.wait(self.post('/drafts/'+d['id']+'/publish',{'request_id':self.rid()}))
        self.assertEqual(job['status'],'completed')
        self.assertEqual(self.platform.publishes,1)

    def test_persisted_stage_distinguishes_prepare_and_final(self):
        self.platform.supports_publish_progress=True
        for started in (False,True):
            def fail(*args,progress):
                progress('final_publish' if started else 'media_upload',started)
                if started:raise TimeoutError()
                from web.platform import PlatformError
                raise PlatformError('准备失败，未调用最终发布')
            self.platform.publish=fail
            d=self.draft()
            job=self.wait(self.post('/drafts/'+d['id']+'/publish',{'request_id':self.rid()}))
            saved=self.app.extensions['workspace'].store.get('demo-seller','draft',d['id'])
            self.assertEqual(saved['publish_request_started'],started)
            self.assertEqual(job['status'],'unknown' if started else 'failed')
            self.assertEqual(saved['status'],'unknown' if started else 'draft')

    def test_publish_timeout_is_not_retried(self):
        self.platform.fail_publish=True;d=self.draft()
        first=self.post('/drafts/'+d['id']+'/publish',{'request_id':self.rid()});job=self.wait(first)
        self.assertEqual(job['status'],'unknown');self.assertEqual(self.platform.publishes,1)
        self.assertEqual(self.s.store.get(self.s.uid(),'draft',d['id'])['status'],'unknown')
        response = self.client.put('/api/drafts/'+d['id'], json=d, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post('/drafts/'+d['id']+'/publish', {'request_id':self.rid()}).status_code, 400)
    def test_invalid_money_prevents_publication(self):
        for price in ['NaN','Infinity','-1','0','1.001']:
            d=self.draft(price=price);r=self.post('/drafts/'+d['id']+'/publish',{'request_id':self.rid()})
            self.assertEqual(r.status_code,400,r.json)
        self.assertEqual(self.platform.publishes,0)
    def test_send_uses_stored_recipient_and_dedup_key(self):
        payload={'text':'测试消息','peer_id':'UNTRUSTED','request_id':self.rid()}
        first=self.post('/conversations/900000/send',payload);second=self.post('/conversations/900000/send',payload)
        self.assertEqual(first.json['id'],second.json['id']);self.assertEqual(self.wait(first)['status'],'completed')
        self.assertEqual(self.platform.sends,1)
        messages=self.client.get('/api/conversations/900000/messages').json['items']
        self.assertEqual(sum(m['text']=='测试消息' for m in messages),1)
    def test_empty_send_rejected(self):
        self.assertEqual(self.post('/conversations/900000/send',{'text':' ','request_id':self.rid()}).status_code,400)
    def test_connector_cannot_send_with_a_different_execution_account(self):
        response=self.post('/conversations/900000/send',{'text':'测试消息','request_id':self.rid(),'expected_account_id':'other-account'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.platform.sends,0)
    def test_connector_matching_account_keeps_request_deduplication(self):
        payload={'text':'合成连接器消息','request_id':self.rid(),'expected_account_id':self.s.uid()}
        first=self.post('/conversations/900000/send',payload)
        self.assertEqual(self.wait(first)['status'],'completed')
        self.assertEqual(self.post('/conversations/900000/send',payload).json['id'],first.json['id'])
        self.assertEqual(self.platform.sends,1)
        recovered=self.client.get('/api/operations/'+payload['request_id'],headers={'X-Execution-Account':self.s.uid()})
        self.assertEqual(recovered.json['id'],first.json['id'])
        self.platform.uid='other-account'
        self.assertEqual(self.client.get('/api/operations/'+payload['request_id']).status_code,404)
    def test_connector_account_binding_also_protects_message_reads(self):
        response=self.client.get('/api/conversations/900000/messages',headers={'X-Execution-Account':'other-account'})
        self.assertEqual(response.status_code,409)
        response=self.client.get('/api/conversations/900000/messages',headers={'X-Execution-Account':self.s.uid()})
        self.assertEqual(response.status_code,200)
    def test_send_to_unknown_conversation_rejected(self):
        self.assertEqual(self.post('/conversations/unknown/send',{'text':'x','request_id':self.rid()}).status_code,404)
    def test_cross_account_data_isolated(self):
        old=self.s.store.list(self.s.uid(),'product')[0]['id'];self.platform.uid='another-account'
        self.assertEqual(self.client.get('/api/products').json['items'],[])
        self.assertEqual(self.post('/products/'+old+'/refresh',{'request_id':self.rid()}).status_code,404)
    def test_uploaded_file_reencoded_and_protected(self):
        media_id=self.upload();response=self.client.get('/api/media/'+media_id);self.assertEqual(response.status_code,200);response.close()
        self.platform.uid='another-account';self.assertEqual(self.client.get('/api/media/'+media_id).status_code,404)
    def test_invalid_image_rejected(self):
        r=self.client.post('/api/media',data={'file':(io.BytesIO(b'<script>alert(1)</script>'),'x.png')},headers=self.headers)
        self.assertEqual(r.status_code,400)
    def test_image_message(self):
        mid=self.upload();r=self.wait(self.post('/conversations/900000/send',{'media_id':mid,'request_id':self.rid()}))
        self.assertEqual(r['status'],'completed');self.assertEqual(self.platform.sends,1)
    def test_templates_crud_and_html_is_data(self):
        r=self.post('/templates',{'title':'<script>x</script>','text':'hello'});tid=r.json['id'];self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.put('/api/templates/'+tid,json={'title':'update','text':'world'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.delete('/api/templates/'+tid,json={},headers=self.headers).status_code,200)
    def test_platform_echo_does_not_duplicate_local_send(self):
        uid=self.s.uid();now=time.time()
        self.s.store.put(uid,'message',{'id':'local-1','cid':'900000','sender_id':uid,'outgoing':True,
            'kind':'text','text':'echo-test','images':[],'timestamp':now,'local_sent':True})
        self.s.save_history(uid,'900000',{'messages':[{'id':'platform-1','sender_id':uid,'outgoing':True,
            'kind':'text','text':'echo-test','images':[],'timestamp':now+1}], 'has_more':False,'cursor':0})
        rows=[m for m in self.s.store.list(uid,'message') if m.get('text')=='echo-test']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['platform_id'],'platform-1')
    def test_older_history_preserves_latest_preview(self):
        uid=self.s.uid();now=time.time()
        for text,stamp in [('new',now),('old',now-86400)]:
            self.s.save_history(uid,'900000',{'messages':[{'id':text,'sender_id':'800000','outgoing':False,
                'kind':'text','text':text,'images':[],'timestamp':stamp}],'has_more':False,'cursor':0})
        self.assertEqual(self.s.store.get(uid,'conversation','900000')['preview'],'new')
    def test_demo_qr_cannot_call_real_login(self):
        self.assertEqual(self.post('/account/qr').status_code,400)
    def test_shortlink_host_validation(self):
        p=Platform(Path(self.temp.name)/'missing.json')
        for value in ['https://attacker.example/?id=1234567','https://m.tb.cn:80/x','https://user:pass@m.tb.cn/x','http://127.0.0.1/']:
            with self.assertRaises(ValueError):p.resolve_item(value)
        self.assertEqual(p.resolve_item('https://www.goofish.com/item?id=1089891189407'),'1089891189407')
    def test_auth_gate(self):
        path=Path(self.temp.name)/'auth';app=create_app(path,platform=self.platform,auth=True);c=app.test_client()
        try:
            self.assertEqual(c.get('/').status_code,302)
            self.assertEqual(c.get('/api/meta').status_code,401)
            password=(path/'admin-password').read_text().strip()
            r=c.post('/api/login',json={'password':password},headers=self.headers);self.assertEqual(r.status_code,200)
            self.assertEqual(c.get('/api/meta').status_code,200)
            self.assertNotIn('cookies',str(c.get('/api/account').json).lower())
        finally:app.extensions['workspace'].executor.shutdown(wait=True)

if __name__=='__main__':unittest.main()
