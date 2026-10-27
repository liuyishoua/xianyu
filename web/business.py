"""Durable workbench business tasks. No agent or platform authority lives in a prompt."""
import hashlib
import json
import math
import threading
import time
import uuid
from flask import request, jsonify

TERMINAL = {'succeeded', 'failed', 'uncertain', 'cancelled'}
OPS = {'product.read':'读取商品缓存', 'conversation.read':'读取会话缓存', 'message.read':'读取消息缓存',
       'conversation.sync':'同步最近会话', 'message.history':'同步会话历史', 'draft.create':'保存商品草稿',
       'product.publish':'发布已有商品草稿', 'dm.reply':'发送会话文字回复',
       'dm.initiate':'主动创建商品咨询会话', 'comment.reply':'回复商品评论',
       'reply.draft':'起草会话回复（不发送）', 'content.analyze':'分析商品内容（不发布）', 'public.search':'公开搜索', 'product.modify':'真实修改商品'}


class Business:
    def __init__(self, accounts, monitor, clock=time.time):
        self.accounts, self.monitor, self.store, self.clock = accounts, monitor, accounts.store, clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        with self.store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS business_authority(goal TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS business_tasks(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, fingerprint TEXT NOT NULL,status TEXT NOT NULL,job_id TEXT,result TEXT,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS business_created(goal TEXT NOT NULL,account TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(goal,account,kind,id));
                CREATE TABLE IF NOT EXISTS business_events(cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT UNIQUE NOT NULL,data TEXT NOT NULL);''')

    def capabilities(self):
        resources=[]
        for a in self.accounts.list():
            s=self.accounts.resolve(str(a['id'])); ops=[]
            try: channel_source=getattr(self.monitor.client(s),'source','demo-xianyu' if getattr(s.platform,'demo',False) else '')
            except ValueError: channel_source=''
            for op,label in OPS.items():
                available=op not in ('public.search','product.modify')
                if op=='comment.reply': available=self.monitor.can_reply(s,'comment')
                if op in ('reply.draft','content.analyze'):
                    try: bindings=s.store.get(s.uid(),'settings','model-bindings')
                    except LookupError: bindings={}
                    available=bool(channel_source and (getattr(s.platform,'demo',False) or bindings.get('dm' if op=='reply.draft' else 'content')))
                reason='' if available else ('当前账号未绑定可用模型 Agent' if op in ('reply.draft','content.analyze') else '真实平台接口尚未实现')
                if a.get('status')=='error' and op in ('conversation.sync','message.history','product.publish','dm.reply','dm.initiate','comment.reply'):
                    available=False;reason='账号连接需要恢复或安全验证'
                params={'product.read':['item_id?'],'message.read':['conversation_id'],'message.history':['conversation_id','older?'],'reply.draft':['conversation_id','text?'],'content.analyze':['item_id?','draft_description?','text?'],'dm.reply':['conversation_id','text'],'product.publish':['draft_id'],'dm.initiate':['peer_id','item_id'],'comment.reply':['item_id','comment_id','text'],'draft.create':['description','price? (required before publish)','media_ids? (required before publish)','location_index? (required before publish)','delivery?']}.get(op,[])
                ops.append({'id':op,'label':label,'available':available,'reason':reason,'parameters':params})
            resources.append({'channel_source_id':channel_source,'channel_account_id':str(a['id']),'account_id':str(a['id']),'name':a.get('label') or a.get('name'), 'operations':ops,
                'scope_options':{'resource_ids':[{'id':x['id'],'label':x.get('title',x['id'])} for x in s.store.list(s.uid(),'product')]+[{'id':x['id'],'label':'草稿 · '+str(x.get('description',''))[:60]} for x in s.store.list(s.uid(),'draft')],
                  'conversation_ids':[{'id':x['id'],'label':x.get('name',x['id'])} for x in s.store.list(s.uid(),'conversation')],
                  'contact_ids':[{'id':str(x['peer_id']),'label':x.get('name',str(x['peer_id']))} for x in s.store.list(s.uid(),'conversation') if x.get('peer_id')]}})
        return {'source':'xianyu_workbench','resources':resources,'lease_seconds':30}

    def authority(self, goal, data):
        if not isinstance(data.get('revision'),int) or isinstance(data['revision'],bool) or data['revision']<0: raise ValueError('授权版本无效')
        if not isinstance(data.get('project_id'),str) or not data['project_id']: raise ValueError('授权项目无效')
        if not isinstance(data.get('enabled'),bool): raise ValueError('授权开关无效')
        expiry=data.get('expires_at',0)
        if not isinstance(expiry,(int,float)) or isinstance(expiry,bool) or not math.isfinite(expiry) or expiry<0 or expiry>self.clock()+31 or (data['enabled'] and expiry<self.clock()-1): raise ValueError('授权租约最多 30 秒')
        if not isinstance(data.get('grants'),list): raise ValueError('授权账号列表无效')
        for grant in data['grants']:
            self.accounts.resolve(str(grant.get('account_id','')))
            if 'expires_at' in grant and (not isinstance(grant['expires_at'],(int,float)) or isinstance(grant['expires_at'],bool) or not math.isfinite(grant['expires_at']) or grant['expires_at']<0): raise ValueError('账号授权到期时间无效')
            if not isinstance(grant.get('operations'),list) or any(x not in OPS for x in grant['operations']): raise ValueError('授权操作无效')
            scope=grant.get('scope',{})
            if not isinstance(scope,dict) or not isinstance(scope.get('include_goal_created',False),bool): raise ValueError('目标新建资源授权无效')
            if not isinstance(scope,dict) or any(not isinstance(scope.get(k,[]),list) or any(not isinstance(x,str) for x in scope.get(k,[])) for k in ('resource_ids','conversation_ids','contact_ids')): raise ValueError('授权范围无效')
        with self.lock,self.store.connect() as db:
            row=db.execute('SELECT data FROM business_authority WHERE goal=?',(goal,)).fetchone()
            if row:
                old=json.loads(row['data'])
                if data['revision']<old['revision']: raise ValueError('授权版本已过期')
                if data['revision']==old['revision'] and {k:v for k,v in data.items() if k!='expires_at'}!={k:v for k,v in old.items() if k!='expires_at'}: raise ValueError('相同授权版本不得更改权限')
            db.execute('INSERT OR REPLACE INTO business_authority VALUES(?,?)',(goal,json.dumps(data)))
        return {'goal_id':goal,**data}

    def check(self,p):
        with self.store.connect() as db: row=db.execute('SELECT data FROM business_authority WHERE goal=?',(p['goal_id'],)).fetchone()
        if not row: raise ValueError('目标未授权')
        a=json.loads(row['data'])
        if a.get('project_id')!=p['project_id']: raise ValueError('目标授权项目不匹配')
        if not a['enabled'] or a['expires_at']<=self.clock() or a['revision']!=p['authorization_revision']: raise ValueError('目标授权已撤回、过期或变化')
        grants=[g for g in a['grants'] if str(g['account_id'])==p['account_id'] and p['operation'] in g['operations'] and g.get('expires_at',a['expires_at'])>self.clock()]
        if not grants: raise ValueError('账号或操作未获授权')
        inp=p['input']; op=p['operation']
        for g in grants:
            scope=g.get('scope',{})
            def created(kind,ident):
                if not scope.get('include_goal_created'): return False
                with self.store.connect() as db:
                    record=db.execute('SELECT t.payload FROM business_created c JOIN business_tasks t ON t.id=c.task_id WHERE c.goal=? AND c.account=? AND c.kind=? AND c.id=?',(p['goal_id'],p['account_id'],kind,ident)).fetchone()
                    if not record: return False
                    if kind=='conversation' and str(json.loads(record['payload'])['input'].get('peer_id','')) not in scope.get('contact_ids',[]): return False
                    return True
            cid=str(inp.get('conversation_id','')); target=str(inp.get('item_id') or inp.get('draft_id') or '')
            if op in ('message.read','message.history','dm.reply','reply.draft') and (not cid or (cid not in scope.get('conversation_ids',[]) and not created('conversation',cid))): continue
            if op in ('product.publish','comment.reply','dm.initiate') and (not target or (target not in scope.get('resource_ids',[]) and not created('draft' if op=='product.publish' else 'product',target))): continue
            if op=='dm.initiate' and str(inp.get('peer_id','')) not in scope.get('contact_ids',[]): continue
            if op in ('product.read','content.analyze') and target and scope.get('resource_ids') and target not in scope['resource_ids'] and not created('product',target): continue
            if str(self.accounts.resolve(p['account_id']).uid())!=p['account_id']: raise ValueError('执行账号已变化')
            return g
        raise ValueError('目标资源或会话未获授权')

    def submit(self,p):
        required=('request_id','goal_id','project_id','action_id','account_id','operation','input','authorization_revision')
        if not isinstance(p.get('authorization_revision'),int) or isinstance(p.get('authorization_revision'),bool): raise ValueError('授权版本无效')
        if p.get('origin')!='headquarters': raise ValueError('业务任务来源无效')
        if any(k not in p for k in required) or not isinstance(p.get('input'),dict): raise ValueError('业务任务字段不完整')
        if any(not isinstance(p[k],str) or not 1<=len(p[k])<=160 for k in required[:6]): raise ValueError('业务任务标识无效')
        if len(p['request_id'])>100: raise ValueError('请求标识过长')
        if p['operation'] not in OPS: raise ValueError('操作不存在')
        digest=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.lock,self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_tasks WHERE request_id=?',(p['request_id'],)).fetchone()
            if row:
                if row['fingerprint']!=digest: raise ValueError('请求标识对应的参数已变化')
                return self.project(row)
            self.check(p)
            cap=next(x for x in self.capabilities()['resources'] if x['account_id']==p['account_id'])
            if not next(x for x in cap['operations'] if x['id']==p['operation'])['available']: raise ValueError('真实操作尚不可用')
            ident=uuid.uuid4().hex
            db.execute('INSERT INTO business_tasks VALUES(?,?,?,?,?,?,?,?)',(ident,p['request_id'],json.dumps(p),digest,'queued',None,None,self.clock()))
            self.emit_status(db,ident)
        return self.get(ident)

    def project(self,row):
        p=json.loads(row['payload'])
        result=json.loads(row['result']) if row['result'] else None
        # An old receipt's boolean false was never proof that an unknown write did not occur.
        if row['status']=='uncertain' and isinstance(result,dict): result={**result,'platform_action_performed':None}
        return {'id':row['id'],**p,'status':row['status'],'state':row['status'],'source_task_id':row['job_id'],'result':result}
    def get(self,ident):
        with self.store.connect() as db: row=db.execute('SELECT * FROM business_tasks WHERE id=?',(ident,)).fetchone()
        if not row: raise LookupError()
        return self.project(row)
    def emit(self,key,event):
        with self.store.connect() as db: db.execute('INSERT OR IGNORE INTO business_events(event_key,data) VALUES(?,?)',(key,json.dumps(event,ensure_ascii=False)))
    def emit_status(self,db,ident):
        row=db.execute('SELECT * FROM business_tasks WHERE id=?',(ident,)).fetchone()
        t=self.project(row)
        event={'kind':'task.status','type':'task.status','task_id':ident,'goal_id':t['goal_id'],'project_id':t['project_id'],'action_id':t['action_id'],'account_id':t['account_id'],'payload':t,**t}
        db.execute('INSERT OR IGNORE INTO business_events(event_key,data) VALUES(?,?)',('status:'+ident+':'+t['status'],json.dumps(event,ensure_ascii=False)))
    def update(self,ident,status,result=None,job_id=None):
        with self.store.connect() as db:
            db.execute('UPDATE business_tasks SET status=?,result=?,job_id=COALESCE(?,job_id) WHERE id=?',(status,json.dumps(result) if result is not None else None,job_id,ident))
            self.emit_status(db,ident)
    def events(self,after=0):
        self.collect_messages()
        with self.store.connect() as db: rows=db.execute('SELECT cursor,data FROM business_events WHERE cursor>? ORDER BY cursor LIMIT 200',(after,)).fetchall()
        return {'items':[{'cursor':r['cursor'],**json.loads(r['data'])} for r in rows], 'cursor':rows[-1]['cursor'] if rows else after}
    def collect_messages(self):
        with self.store.connect() as db: rows=db.execute('SELECT * FROM business_tasks').fetchall()
        for row in rows:
            p=json.loads(row['payload']); cid=p['input'].get('conversation_id')
            if not cid:
                with self.store.connect() as db:
                    created=db.execute("SELECT id FROM business_created WHERE goal=? AND account=? AND kind='conversation' AND task_id=?",(p['goal_id'],p['account_id'],row['id'])).fetchone()
                cid=created['id'] if created else None
            if not cid: continue
            try: self.check(p)
            except (ValueError,LookupError): continue
            s=self.accounts.resolve(p['account_id'])
            for m in s.store.list(s.uid(),'message'):
                if m.get('cid')==cid and not m.get('outgoing') and float(m.get('timestamp') or 0)>=row['created']:
                    self.emit('message:'+p['goal_id']+':'+p['account_id']+':'+str(m['id']), {'kind':'message.received','type':'message.received','project_id':p['project_id'],'payload':{'message':m},'goal_id':p['goal_id'],'action_id':p['action_id'],'account_id':p['account_id'],'source_task_id':row['job_id'],'task_id':row['id'],'source':'workbench_platform_cache','message':m})

    def record_created(self,p,kind,ident,task_id):
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO business_created VALUES(?,?,?,?,?)',(p['goal_id'],p['account_id'],kind,str(ident),task_id))

    def tick(self):
        with self.lock:
            with self.store.connect() as db: rows=db.execute("SELECT * FROM business_tasks WHERE status NOT IN ('succeeded','failed','uncertain','cancelled')").fetchall()
            for row in rows:
                t=self.project(row); p=json.loads(row['payload']); s=self.accounts.resolve(p['account_id']); rid='business-'+row['id']
                try:
                    if t['source_task_id'] and t['source_task_id'].startswith('channel:'):
                        self.check(p)
                        from .channel_client import ChannelError
                        try: event=self.monitor.client(s).status(t['source_task_id'][8:],p['account_id'])
                        except ChannelError: continue
                        if event.get('state') in ('completed','succeeded'):
                            output=(event.get('result') or {}).get('output') or {}
                            structured=output.get('structured') if isinstance(output,dict) else None
                            invalid=isinstance(output,dict) and output.get('status') in ('needs_context','blocked','error')
                            if isinstance(structured,dict):
                                invalid=invalid or structured.get('status') in ('needs_context','blocked','error') or structured.get('decision') in ('no_reply','handoff','hold','stop')
                                keys=('reply_text','reply','text') if p['operation']=='reply.draft' else ('reply','reply_text','body','content','text','description')
                                usable=any(isinstance(structured.get(k),str) and structured[k].strip() for k in keys)
                            else:
                                usable=isinstance(output,dict) and isinstance(output.get('text'),str) and bool(output['text'].strip())
                            if invalid or not usable:
                                self.update(t['id'],'failed',{'source':'channel_model_draft','reason':'模型完成但缺少可用产物','model_status':(structured if isinstance(structured,dict) else output).get('status') if isinstance(output,dict) else None,'channel_event_id':event['id'],'evidence':event.get('result'),'platform_action_performed':False})
                                continue
                            self.update(t['id'],'succeeded',{'source':'channel_model_draft','channel_event_id':event['id'],'channel_issue':event.get('issue_id') or event.get('result',{}).get('issue_id'),'evidence':event.get('result'),'platform_action_performed':False})
                        elif event.get('state') in ('failed','blocked_no_route','blocked_reply','failed_preflight','uncertain','attention','cancelled','error'):
                            self.update(t['id'],'failed',{'source':'channel_model_draft','reason':'模型未返回可用草稿','platform_action_performed':False})
                        continue
                    # Reconcile persisted original job before deciding whether new work is safe.
                    try: job=s.get_operation(rid)
                    except LookupError: job=None
                    if job:
                        status={'completed':'succeeded','failed':'failed','unknown':'uncertain','pending':'waiting_platform','running':'running'}[job['status']]
                        if status=='succeeded' and p['operation']=='product.publish' and (job.get('result') or {}).get('item_id'):
                            self.record_created(p,'product',job['result']['item_id'],t['id'])
                        if status!=t['status']:
                            source={'product.read':'authenticated_workbench_cache','conversation.read':'authenticated_workbench_cache','message.read':'authenticated_workbench_cache','draft.create':'local_draft','conversation.sync':'workbench_platform_read','message.history':'workbench_platform_read'}.get(p['operation'],'workbench_platform_job')
                            if getattr(s.platform,'demo',False): source='demo_platform_job'
                            self.update(t['id'],status,{'source':source,'job_id':job['id'],'evidence':job['result'],'reason':job['error'],'platform_action_performed':None if status=='uncertain' else status=='succeeded' and p['operation'] in ('dm.reply','dm.initiate','comment.reply','product.publish')},job['id'])
                        continue
                    self.check(p)
                    if t['status']!='queued':
                        self.update(t['id'],'uncertain',{'source':'workbench','reason':'执行已开始但原任务记录不可确认，不会重发'})
                        continue
                    self.update(t['id'],'running')
                    guard=lambda p=p: self.check(p)
                    op=p['operation']; inp=p['input']; uid=s.uid(); cid=str(inp.get('conversation_id',''))
                    if op in ('reply.draft','content.analyze'):
                        from .channel_bridge import submit_analysis
                        model_input={k:inp[k] for k in (('conversation_id','text') if op=='reply.draft' else ('text','item_id','draft_description','price')) if k in inp}
                        guard(); event,_=submit_analysis(s,self.monitor,{**model_input,'message_id':'business-'+p['request_id']})
                        self.update(t['id'],'waiting_model',job_id='channel:'+event['id'])
                        continue
                    if op=='conversation.sync': job=s.sync(rid,guard)
                    elif op=='message.history': job=s.history(cid,bool(inp.get('older')),rid,guard)
                    elif op=='dm.reply':
                        if not isinstance(inp.get('text'),str) or not inp['text'].strip(): raise ValueError('需要已核实的回复正文；模型草稿走现有工作台审批流程')
                        job=s.send(cid,{'text':inp['text'],'request_id':rid,'expected_account_id':p['account_id']},guard)
                    elif op=='product.publish': job=s.publish(str(inp['draft_id']),rid,guard)
                    else:
                        def work(uid,p=p,s=s,task_id=t['id']):
                            self.check(p); op=p['operation']; i=p['input']; scope=self.check(p).get('scope',{})
                            if op in ('product.read','conversation.read','message.read'):
                                kind={'product.read':'product','conversation.read':'conversation','message.read':'message'}[op]
                                items=s.store.list(uid,kind)
                                if op=='message.read': items=[x for x in items if x.get('cid')==i['conversation_id']]
                                elif op=='product.read':
                                    ids=scope.get('resource_ids',[])
                                    if ids and scope.get('include_goal_created'):
                                        with self.store.connect() as db:
                                            ids=ids+[x['id'] for x in db.execute("SELECT id FROM business_created WHERE goal=? AND account=? AND kind='product'",(p['goal_id'],p['account_id'])).fetchall()]
                                    items=[x for x in items if (not ids or x['id'] in ids) and (not i.get('item_id') or x['id']==i['item_id'])]
                                elif scope.get('conversation_ids'): items=[x for x in items if x['id'] in scope['conversation_ids']]
                                result={'source':'authenticated_workbench_cache','items':items}
                                if op=='product.read':
                                    drafts=s.store.list(uid,'draft')
                                    if scope.get('resource_ids'):
                                        allowed=set(scope['resource_ids'])
                                        if scope.get('include_goal_created'):
                                            with self.store.connect() as db:
                                                allowed.update(x['id'] for x in db.execute("SELECT id FROM business_created WHERE goal=? AND account=? AND kind='draft'",(p['goal_id'],p['account_id'])))
                                        drafts=[d for d in drafts if d['id'] in allowed]
                                    attached={mid for d in drafts for mid in d.get('media_ids',[])}
                                    media=[{k:m[k] for k in ('id','width','height','url') if k in m} for m in s.store.list(uid,'media') if not scope.get('resource_ids') or m['id'] in attached]
                                    try: locations=s.store.get(uid,'settings','locations')['items']
                                    except LookupError: locations=[]
                                    result['publication_context']={'account_id':uid,'drafts':drafts,'media':media,
                                        'locations':[{'index':n,'label':' · '.join(str(x.get(k) or '') for k in ('prov','city','area','poi'))} for n,x in enumerate(locations)],
                                        'draft_can_be_saved_incomplete':True,
                                        'required_before_publish':['description','positive_price','owned_media_ids','owned_location_index (only physical shipping)'],
                                        'missing_preparation':(['media'] if not media else [])+(['location'] if not locations else [])}
                                return result
                            if op=='draft.create':
                                draft=s.draft(i)
                                self.record_created(p,'draft',draft['id'],task_id)
                                return {'source':'local_draft','draft':draft}
                            if op=='dm.initiate':
                                import re
                                if not re.fullmatch(r'\d{5,24}',str(i.get('peer_id',''))) or not re.fullmatch(r'\d{6,24}',str(i.get('item_id',''))): raise ValueError('对方用户和商品标识无效')
                                item=s.platform.item(i['item_id'])
                                if str(item.get('seller_id',''))!=str(i['peer_id']): raise ValueError('商品实际卖家与收件人不一致')
                                self.check(p); result=s.platform.socket('create',peer_id=i['peer_id'],item_id=i['item_id'])
                                conv=s.store.put(uid,'conversation',{**result,'name':'用户 '+str(i['peer_id'])[-4:],'preview':'','updated_at':self.clock()})
                                self.record_created(p,'conversation',conv['id'],task_id)
                                return conv
                            if op=='comment.reply':
                                rows=s.store.list(uid,'inbox-comment'); msg=next((x for x in rows if x.get('message_id')==i.get('comment_id') and x.get('target')==i.get('item_id')),None)
                                if not msg: raise ValueError('已验证评论不存在')
                                if not isinstance(i.get('text'),str) or not 1<=len(i['text'].strip())<=3000: raise ValueError('评论正文无效')
                                self.check(p); return s.platform.reply_comment(msg['message'],i['text'])
                            raise ValueError('真实操作尚不可用')
                        kind={'dm.initiate':'create','comment.reply':'comment'}.get(op,'business-read')
                        job=s.job(kind,'指挥中心业务任务',rid,work,guard)
                    self.update(t['id'],'waiting_platform',job_id=job['id'])
                except Exception as exc:
                    from .channel_client import ChannelError
                    if isinstance(exc,ChannelError):
                        self.update(t['id'],'uncertain',{'source':'channel_model_draft','reason':'模型提交结果待核对，保留原消息标识，不会自动重发'})
                    elif isinstance(exc,(ValueError,LookupError)):
                        self.update(t['id'],'cancelled' if '授权' in str(exc) else 'failed',{'source':'workbench','reason':str(exc) or '记录不存在'})
                    else:
                        self.update(t['id'],'uncertain',{'source':'workbench','reason':'业务调度中断，请核对原任务，未自动重发'})
            self.collect_messages()
    def cancel(self,ident):
        with self.lock:
            t=self.get(ident)
            if t['status']=='queued': self.update(ident,'cancelled',{'source':'workbench','reason':'总部取消尚未执行的业务任务'})
            return self.get(ident)

    def start(self):
        def run():
            while not self.stop.wait(.5):
                try: self.tick()
                except Exception: pass
        threading.Thread(target=run,name='xianyu-business',daemon=True).start()


def register(app,accounts,monitor):
    business=Business(accounts,monitor); app.extensions['business']=business
    app.add_url_rule('/api/business/capabilities','business_capabilities',business.capabilities)
    app.add_url_rule('/api/business/tasks','business_submit',lambda:(jsonify(business.submit(request.json)),202),methods=['POST'])
    app.add_url_rule('/api/business/tasks/<ident>','business_get',business.get)
    app.add_url_rule('/api/business/tasks/<ident>/cancel','business_cancel',business.cancel,methods=['POST'])
    app.add_url_rule('/api/business/events','business_events',lambda:business.events(int(request.args.get('after',0))))
    app.add_url_rule('/api/business/goals/<goal>/authority','business_authority',lambda goal:business.authority(goal,request.json),methods=['PUT'])
    return business
