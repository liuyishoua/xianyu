"""Keep the official SMS browser alive for owner-operated verification."""
import hashlib
import json
import queue
import re
import threading
import time

import requests
from goofish_apis import XianyuApis
from utils.goofish_utils import generate_device_id
from .phone_login import PhoneLogin,PhoneLoginError,sms_response_data,require_sms_success
from .phone_login_browser import BrowserPhoneLogin,browser_proxy,HANDOFF_HOSTS
from .platform import private_json
from urllib.parse import urlsplit

POOL={}
POOL_LOCK=threading.Lock()


def active_requests(directory):
    prefix=str(PhoneLogin(directory).directory)
    with POOL_LOCK:items=[(id,s) for (path,id),s in POOL.items() if path==prefix]
    return {s.expected:{'id':id,**s.status()} for id,s in items if s.state in ('opening','verification','waiting','processing')}


class NativeSession:
    def __init__(self,adapter,id,phone,expected):
        self.adapter=adapter;self.id=id;self.phone=phone;self.expected=expected
        self.phone_digest=hashlib.sha256(phone.encode()).hexdigest()
        self.state='opening';self.reason='正在打开官方短信登录页'
        self.ready=threading.Event();self.commands=queue.Queue();self.expires=time.time()+1800
        self.sent_at=0;self.confirmed_once=False;self.responses=[];self.login_responses=[]
        self.lock=threading.RLock()

    def status(self):
        with self.lock:return {'phase':self.state,'reason':self.reason,'expires':self.expires,'account_id':self.expected}

    def machine(self):
        status=self.status();state={'waiting':'waiting','verification':'uncertain','opening':'uncertain'}.get(status['phase'],'needs_user')
        return {'state':state,'remote_id':self.id,'reason':status['reason']}

    def change(self,state,reason):
        with self.lock:self.state=state;self.reason=reason

    def call(self,kind,**data):
        if not self.thread.is_alive():raise PhoneLoginError('官方短信会话已结束，未再次发码')
        reply=queue.Queue(maxsize=1);self.commands.put((kind,data,reply))
        try:result=reply.get(timeout=30)
        except queue.Empty:raise PhoneLoginError('官方页面操作尚未确认，未重复提交') from None
        if isinstance(result,Exception):raise result
        return result

    def sync_waiting(self):
        # Both services share this local durable SDK; never announce waiting without smsToken.
        from .account_maintenance import Bridge
        from maintenance.store import Store
        directory=self.adapter.directory.parent
        bridge=Bridge(directory)
        if not bridge.config or bridge.config.get('source')!='xianyu':return
        attempt=bridge.attempt(self.id)
        if not attempt or attempt['account_id']!=self.expected:return
        center=Store(directory.parents[2]/'account-maintenance'/'.local')
        job=center.job(self.id)
        if not job or job['source']!='xianyu' or job['external_id']!=self.expected:return
        if job['state'] not in ('preparing','needs_user','uncertain','waiting'):return
        bridge.update(self.id,'waiting',self.id,{'reason':'等待手机自动转发验证码'})
        if job['state']!='waiting':center.update(self.id,expected_state=job['state'],state='waiting',
            remote_id=self.id,expires=self.sent_at+180,reason='等待手机自动转发验证码')

    def close_pending(self):
        from .account_maintenance import Bridge
        from maintenance.store import Store
        directory=self.adapter.directory.parent;bridge=Bridge(directory)
        if not bridge.config or bridge.config.get('source')!='xianyu':return
        center=Store(directory.parents[2]/'account-maintenance'/'.local');job=center.job(self.id)
        if job and job['source']=='xianyu' and job['external_id']==self.expected and job['state']=='uncertain':
            bridge.update(self.id,'needs_user',self.id,{'reason':self.reason})
            center.update(self.id,expected_state='uncertain',state='needs_user',reason=self.reason)

    def authorized_attempt(self):
        from .account_maintenance import Bridge
        from maintenance.store import Store
        directory=self.adapter.directory.parent;bridge=Bridge(directory)
        if not bridge.config:return True
        center=Store(directory.parents[2]/'account-maintenance'/'.local');job=center.job(self.id)
        return bool(job and job['source']=='xianyu' and job['external_id']==self.expected
            and job['state'] not in ('cancelled','confirmed','needs_attention') and center.binding_valid(job))

    def observe(self,response):
        u=urlsplit(response.url)
        if u.hostname!='passport.goofish.com':return
        if u.path=='/newlogin/sms/send.do':self.responses.append(response)
        if u.path=='/newlogin/sms/login.do':self.login_responses.append(response)

    def inspect_send(self):
        if self.state not in ('opening','verification'):return
        for response in self.responses:
            try:data=sms_response_data(response.json())
            except Exception:continue
            if data.get('smsToken') and not data.get('isCheckCodeShowed'):
                self.sent_at=time.time();self.expires=self.sent_at+180
                self.change('waiting','验证码已发送，等待手机自动转发')
                self.sync_waiting();return

    def receipt(self):
        successful=False
        for response in self.login_responses:
            try:require_sms_success(sms_response_data(response.json()));successful=True
            except Exception:continue
        if not successful:raise PhoneLoginError('官方页面尚未确认短信登录成功，未重复提交验证码')
        cookies=self.context.cookies()
        uid={c['value'] for c in cookies if c['name']=='unb' and c['domain'].lstrip('.') in ('goofish.com','www.goofish.com')}
        if uid!={self.expected}:raise PhoneLoginError('官方网页登录身份与原账号不一致，未保存登录态')
        api=XianyuApis({},generate_device_id(self.expected))
        api.session=self.adapter.session()
        for c in cookies:
            if c['domain'].lstrip('.') not in ('goofish.com','www.goofish.com','passport.goofish.com'):continue
            api.session.cookies.set_cookie(requests.cookies.create_cookie(name=c['name'],value=c['value'],
                domain=c['domain'],path=c['path'],secure=c['secure'],expires=int(c['expires']) if c['expires']>0 else None))
        self.change('login_done','短信登录已提交，等待工作台核验消息连接')
        return api

    def execute(self,kind,data):
        page=self.page.page if hasattr(self.page,'page') else self.page
        if kind=='frame':return page.screenshot(type='jpeg',quality=70)
        if kind=='drag':
            points=data.get('points')
            if not isinstance(points,list) or not 1<=len(points)<=100:raise ValueError('页面操作无效')
            for point in points:
                if not isinstance(point,list) or len(point)!=2 or any(type(v) not in (int,float) for v in point) or not 0<=point[0]<=1000 or not 0<=point[1]<=850:raise ValueError('页面坐标无效')
            page.mouse.move(*points[0]);page.mouse.down()
            try:
                for point in points[1:]:page.mouse.move(*point);page.wait_for_timeout(16)
            finally:page.mouse.up()
            return {'ok':True}
        if kind=='text':
            text=data.get('text')
            if not isinstance(text,str) or not re.fullmatch(r'[A-Za-z0-9]{1,16}',text):raise ValueError('验证文字无效')
            page.keyboard.insert_text(text);return {'ok':True}
        if kind=='confirm':
            if self.state!='waiting' or self.expires<time.time() or self.confirmed_once:raise PhoneLoginError('短信会话已结束或已提交，未重复确认')
            code=data.get('code','')
            if not re.fullmatch(r'\d{6}',code):raise ValueError('验证码应为6位')
            self.confirmed_once=True
            self.page.locator('input[placeholder*="验证码"]').fill(code)
            self.change('processing','正在通过官方页面确认短信登录')
            with self.page.expect_response(lambda r:urlsplit(r.url).path=='/newlogin/sms/login.do',timeout=15000):
                self.page.locator('button.sms-login').click()
            for _ in range(80):
                try:return self.receipt()
                except PhoneLoginError:page.wait_for_timeout(100)
            raise PhoneLoginError('官方短信登录仍需页面确认，未重复提交验证码')
        raise ValueError('页面操作不受支持')

    def run(self):
        try:
            with self.adapter.browser_adapter.browser() as (self.context,self.page):
                self.page.on('response',self.observe)
                self.adapter.browser_adapter.wait_ready(self.page)
                self.page.locator('#fm-sms-login-id').fill(self.phone)
                agreement=self.page.locator('#fm-agreement-checkbox')
                if agreement.is_visible():agreement.check()
                with self.page.expect_response(lambda r:urlsplit(r.url).path=='/newlogin/sms/send.do',timeout=10000):
                    self.page.locator('.send-btn-link').click()
                self.inspect_send()
                if self.state=='opening':self.change('verification','请在工作台的官方页面完成安全验证，程序不会再次点击发码')
                self.ready.set()
                checked_at=0
                while time.time()<self.expires and self.state not in ('login_done','closed'):
                    if time.time()-checked_at>2:
                        checked_at=time.time()
                        if not self.authorized_attempt():break
                    self.page.wait_for_timeout(100);self.inspect_send()
                    try:kind,data,reply=self.commands.get_nowait()
                    except queue.Empty:continue
                    try:reply.put(self.execute(kind,data))
                    except Exception as exc:reply.put(PhoneLoginError(str(exc)) if isinstance(exc,(PhoneLoginError,ValueError)) else PhoneLoginError('官方页面操作未完成，未重复提交'))
                if self.state!='login_done':self.change('closed','官方短信会话已结束，未再次发码')
        except Exception as error:
            private_json(self.adapter.path(self.id).with_suffix('.diagnostic.json'),{
                'failed_phase':self.state,'error_type':type(error).__name__,
                'sms_success_observed':bool(self.sent_at)})
            self.change('closed','官方短信页面未就绪或连接中断，未再次发码')
        finally:
            self.ready.set();self.phone=''
            if self.state=='closed':
                try:self.close_pending()
                except Exception:pass
            self.responses.clear();self.login_responses.clear()
            self.page=None;self.context=None
            while True:
                try:_,_,reply=self.commands.get_nowait()
                except queue.Empty:break
                reply.put(PhoneLoginError('官方短信会话已结束，未重复提交'))


class NativePhoneLogin(PhoneLogin):
    def __init__(self,directory,config=None):
        super().__init__(directory);self.config=config or {};self.browser_adapter=BrowserPhoneLogin(directory,self.config)

    def selected(self,id,expected=None):
        with POOL_LOCK:session=POOL.get((str(self.directory),id))
        if not session or expected and session.expected!=expected:raise PhoneLoginError('官方短信会话不存在或账号不匹配，未再次发码')
        return session

    def start(self,id,phone,expected=None):
        if not re.fullmatch(r'1\d{10}',phone) or not expected:raise ValueError('短信绑定无效')
        key=(str(self.directory),id)
        with POOL_LOCK:
            session=POOL.get(key)
            if session:
                if session.expected!=expected or session.phone_digest!=hashlib.sha256(phone.encode()).hexdigest():raise ValueError('账号身份不匹配')
                return session.machine()
            session=NativeSession(self,id,phone,expected);POOL[key]=session
            private_json(self.path(id),{'engine':'native_browser','expected':expected,
                'phone_digest':hashlib.sha256(phone.encode()).hexdigest(),'expires':session.expires,'browser_config':self.config})
            session.thread=threading.Thread(target=session.run,daemon=True,name='xianyu-official-sms')
        session.thread.start()
        session.ready.wait(30)
        return session.machine()

    def confirm(self,id,phone,code,expected):
        saved=json.loads(self.path(id).read_text())
        if saved.get('engine')!='native_browser':return super().confirm(id,phone,code,expected)
        if saved['phone_digest']!=hashlib.sha256(phone.encode()).hexdigest():raise ValueError('手机号绑定已变化')
        return self.selected(id,expected).call('confirm',code=code)

    def view(self,id):return self.selected(id).status()
    def frame(self,id):return self.selected(id).call('frame')
    def control(self,id,data):
        if not isinstance(data,dict) or data.get('kind') not in ('drag','text'):raise ValueError('页面操作不受支持')
        session=self.selected(id)
        if session.state!='verification':raise ValueError('当前不是官方安全验证阶段')
        if not session.authorized_attempt():raise ValueError('原登录请求已停止或绑定已变化')
        return session.call(data['kind'],**{k:v for k,v in data.items() if k!='kind'})
