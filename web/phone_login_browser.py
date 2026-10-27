"""Use the official SMS page and its JavaScript, without solving challenges."""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from requests.cookies import create_cookie

from goofish_apis import XianyuApis
from utils.goofish_utils import generate_device_id
from .platform import private_json
from .phone_login import (PhoneLogin, PhoneLoginError, LOGIN, PASSPORT, sms_failure_reason,
    require_sms_success, sms_response_data, sms_response_diagnostic)

LOGIN_URL=LOGIN+'?'+urlencode({'appName':'xianyu','appEntrance':'web','lang':'zh_cn',
    'styleType':'vertical','fromSite':'77','qrCodeFirst':'false'})
HANDOFF_HOSTS={'passport.goofish.com','www.goofish.com','login.taobao.com','passport.taobao.com'}


class LoginFrame:
    """Keep network observation on the page and form interaction in its iframe."""
    def __init__(self,page,frame):self.page=page;self.frame=frame
    def locator(self,*args,**kwargs):return self.frame.locator(*args,**kwargs)
    def evaluate(self,*args,**kwargs):return self.frame.evaluate(*args,**kwargs)
    def expect_response(self,*args,**kwargs):return self.page.expect_response(*args,**kwargs)
    def on(self,*args,**kwargs):return self.page.on(*args,**kwargs)
    def remove_listener(self,*args,**kwargs):return self.page.remove_listener(*args,**kwargs)
    def wait_for_timeout(self,*args,**kwargs):return self.page.wait_for_timeout(*args,**kwargs)
    def wait_for_function(self,*args,**kwargs):return self.frame.wait_for_function(*args,**kwargs)


def official_url(value,path=None):
    url=urljoin(PASSPORT,value);u=urlsplit(url)
    if u.scheme!='https' or u.username or u.password or u.port not in (None,443):
        raise PhoneLoginError('短信登录返回了需人工核对的跳转')
    if path is not None:
        if u.hostname!='passport.goofish.com' or u.path!=path:
            raise PhoneLoginError('官方短信登录接口已变化')
    elif u.hostname not in HANDOFF_HOSTS:
        raise PhoneLoginError('短信登录返回了需人工核对的跳转')
    return url


def browser_proxy(config):
    value=config.get('browser_proxy')
    if not value:return None
    url=urlsplit(value)
    if url.scheme not in ('socks5','http') or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password or url.path or url.query or url.fragment:
        raise PhoneLoginError('短信浏览器代理必须使用本机已配置的网络入口')
    try:port=url.port
    except ValueError:port=None
    if not port or not 1<=port<=65535:raise PhoneLoginError('短信浏览器代理端口无效')
    return value


class BrowserPhoneLogin(PhoneLogin):
    def __init__(self,directory,config=None):
        super().__init__(directory)
        self.config=config or {}

    @staticmethod
    def wait_ready(page):
        page.wait_for_function('''() => !!(window.sufei&&typeof window.sufei.check==='function'
            &&window.UA_Opt&&window[window.UA_Opt.LogVal])''',timeout=10000,polling=100)

    def observe_official_check(self,page):
        if not self.config.get('diagnostic_mode'):return
        # Observe the native SDK; preserve its receiver, arguments and return value.
        page.evaluate('''() => {
            const state=window.__leoSmsObservation={calls:0,callbacks:0,callback_pass:false,
                callback_token:false,threw:false,outer_ret:false,wrapped_ret:false};
            const original=window.sufei.check;
            window.sufei.check=function(response,callback,...rest) {
                const url=response&&(response.config&&response.config.url||response.url||
                    response.request&&response.request.responseURL)||'';
                if(!String(url).includes('/newlogin/sms/send.do'))
                    return original.call(this,response,callback,...rest);
                state.calls++;
                state.outer_ret=!!(response&&response.ret);
                state.wrapped_ret=!!(response&&response.data&&response.data.ret);
                try { return original.call(this,response,function(...args) {
                    state.callbacks++;state.callback_pass=!!args[0];
                    state.callback_token=!!(args[1]&&args[1]._sufeiToken);
                    return callback.apply(this,args);
                },...rest); } catch(error) {state.threw=true;throw error;}
            };
        }''')

    def official_check_diagnostic(self,page):
        if not self.config.get('diagnostic_mode'):return {}
        value=page.evaluate('''() => ({...window.__leoSmsObservation,
            webdriver:navigator.webdriver===true,
            ua_ready:!!(window.UA_Opt&&window[window.UA_Opt.LogVal]),
            umid_ready:!!window.umidToken,
            server_umid_ready:!!((window.viewData||{}).umidToken),
            challenge_frame_present:!!document.querySelector('iframe[src*="punish"],iframe[src*="captcha"]')})''')
        if not isinstance(value,dict):return {}
        return {'official_check':{k:value[k] for k in ('calls','callbacks') if type(value.get(k)) is int},
            'browser_observation':{k:value[k] for k in ('callback_pass','callback_token','threw','outer_ret','wrapped_ret','webdriver',
                'ua_ready','umid_ready','server_umid_ready','challenge_frame_present') if type(value.get(k)) is bool}}

    @contextmanager
    def display(self,config=None):
        config=self.config if config is None else config
        if config.get('headless',True) or sys.platform=='darwin' or os.environ.get('DISPLAY'):
            yield None;return
        process=subprocess.Popen(['Xvfb','-displayfd','1','-screen','0','1280x900x24','-nolisten','tcp'],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            if not select.select([process.stdout],[],[],3)[0]:raise RuntimeError('Display initialization timed out')
            number=process.stdout.readline().strip()
            if not number.isdigit():raise RuntimeError('Display initialization failed')
            yield {**os.environ,'DISPLAY':':'+number}
        finally:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            process.stdout.close()

    @contextmanager
    def browser(self,state=None,config=None):
        from playwright.sync_api import sync_playwright
        config=self.config if config is None else config
        proxy=browser_proxy(config)
        with self.display(config) as env,sync_playwright() as p:
            options={'headless':bool(config.get('headless',True)),'args':['--no-sandbox'],'timeout':5000}
            if env:options['env']=env
            if proxy:options['proxy']={'server':proxy}
            executable=config.get('browser_executable')
            if executable:options['executable_path']=executable
            browser=p.chromium.launch(**options)
            try:
                options={'locale':'zh-CN','viewport':{'width':1000,'height':850}}
                if state:options['storage_state']=state
                context=browser.new_context(**options)
                page=context.new_page();page.set_default_timeout(5000)
                if config.get('entry')=='home':
                    page.goto('https://www.goofish.com/',wait_until='domcontentloaded',timeout=8000)
                    iframe=page.locator('iframe[src*="passport.goofish.com"]')
                    iframe.wait_for(state='attached',timeout=8000)
                    frame=iframe.element_handle().content_frame()
                    frame.locator('#fm-sms-login-id').wait_for(timeout=5000)
                    login=LoginFrame(page,frame)
                else:
                    page.goto(LOGIN_URL,wait_until='domcontentloaded',timeout=8000)
                    page.locator('#fm-sms-login-id').wait_for()
                    login=page
                yield context,login
            finally:browser.close()

    def start(self,id,phone,expected=None):
        if not re.fullmatch(r'1\d{10}',phone):raise ValueError('手机号须为11位')
        sent=False;phase='page_open'
        try:
            with self.browser() as (context,page):
                phase='sdk_ready'
                self.wait_ready(page)
                phase='observation'
                self.observe_official_check(page)
                phase='form_ready'
                page.locator('#fm-sms-login-id').fill(phone)
                agreement=page.locator('#fm-agreement-checkbox')
                if agreement.is_visible():agreement.check()
                responses=[]
                page_errors=[]
                def collect_error(error):
                    message=str(getattr(error,'message',''))
                    page_errors.append('data' in message and ('undefined' in message or 'null' in message))
                def collect(response):
                    url=urlsplit(response.url)
                    if url.hostname=='passport.goofish.com' and url.path=='/newlogin/sms/send.do':responses.append(response)
                page.on('pageerror',collect_error)
                page.on('response',collect)
                try:
                    phase='send_response'
                    with page.expect_response(lambda r:urlsplit(r.url).hostname=='passport.goofish.com' and urlsplit(r.url).path=='/newlogin/sms/send.do',timeout=10000) as pending:
                        sent=True
                        page.locator('.send-btn-link').click()
                    first=pending.value
                    phase='official_callback'
                    if first not in responses:responses.append(first)
                    deadline=time.monotonic()+min(8,max(0,float(self.config.get('official_response_wait_seconds',8))))
                    processed=0;data={};payload={};response=first
                    while True:
                        while processed<len(responses):
                            response=responses[processed];processed+=1
                            try:payload=response.json()
                            except Exception:payload={}
                            data=sms_response_data(payload)
                            if data.get('smsToken') and not data.get('isCheckCodeShowed'):break
                        if data.get('smsToken') and not data.get('isCheckCodeShowed') or time.monotonic()>=deadline:break
                        # The official page may finish its own verification callback.
                        # Do not click again or manufacture a verification response.
                        page.wait_for_timeout(100)
                    diagnostic=sms_response_diagnostic(payload,response.status)
                    diagnostic.update(response_count=processed,official_followup_observed=processed>1,
                        sms_credential_received=bool(data.get('smsToken')),
                        page_error_count=len(page_errors),response_shape_error=any(page_errors))
                    try:diagnostic.update(self.official_check_diagnostic(page))
                    except Exception:diagnostic['observation_unavailable']=True
                    form=parse_qs(response.request.post_data or '',keep_blank_values=True)
                    diagnostic['request_fields']=sorted(k for k in form if re.fullmatch(r'[A-Za-z0-9_-]{1,64}',k))[:64]
                    diagnostic['request_context']={k:bool(form.get(k,[''])[0]) for k in ('ua','umidToken','bx-ua','bx-umidtoken','deviceId','pageTraceId')}
                    try:visible=page.locator('body').inner_text(timeout=1000)
                    except Exception:visible=''
                    if isinstance(visible,str):
                        diagnostic.update(page_reports_sms_sent='短信验证码已发送' in visible,
                            page_reports_verification=any(s in visible for s in ('按住滑块','拖动滑块','安全验证','访问验证')))
                    private_json(self.path(id).with_suffix('.diagnostic.json'),diagnostic)
                finally:
                    page.remove_listener('response',collect)
                    page.remove_listener('pageerror',collect_error)
                if data.get('isCheckCodeShowed') or not data.get('smsToken'):
                    return {'state':'needs_user','reason':sms_failure_reason(data,'发码')}
                form={k:v[0] for k,v in parse_qs(response.request.post_data or '',keep_blank_values=True).items()}
                private_json(self.path(id),{'engine':'browser','form':form,
                    'browser_config':{k:self.config[k] for k in ('browser_executable','headless','entry','browser_proxy') if k in self.config},
                    'phone_digest':hashlib.sha256(phone.encode()).hexdigest(),'sms_token':data['smsToken'],
                    'expires':time.time()+180,'browser_state':context.storage_state()})
                return {'state':'waiting','remote_id':id}
        except Exception as error:
            private_json(self.path(id).with_suffix('.diagnostic.json'),{
                'failed_phase':phase,'send_intent':sent,
                'timeout':'Timeout' in type(error).__name__,
                'browser_closed':'closed' in str(error).lower()})
            return {'state':'uncertain' if sent else 'needs_user',
                'reason':'官方短信发码响应不确定，未再次发码' if sent else '官方短信登录页未就绪，请检查浏览器和网络'}

    def confirm(self,id,phone,code,expected):
        saved=json.loads(self.path(id).read_text())
        if saved['expires']<time.time() or saved['phone_digest']!=hashlib.sha256(phone.encode()).hexdigest():
            raise ValueError('短信登录会话已过期或手机号变化')
        if not re.fullmatch(r'\d{6}',code):raise ValueError('验证码应为6位')
        # A request created before enabling this engine still uses its original session.
        if saved.get('engine')!='browser':return super().confirm(id,phone,code,expected)
        config=saved.get('browser_config',self.config)
        try:
            with self.browser(saved['browser_state'],config=config) as (context,page):
                self.wait_ready(page)
                api=official_url(page.evaluate('window.viewConfig.api.smsLoginApi'),'/newlogin/sms/login.do')
                form={k:v for k,v in saved['form'].items() if k not in ('bx-ua','bx-umidtoken','bx_et','x-pipu2')}
                runtime=page.evaluate('''() => {
                    const server=(window.viewData||{}).umidToken||'';
                    return {...((window.viewData||{}).loginFormData||{}),ua:window.UA_Opt&&window[window.UA_Opt.LogVal]||'',
                        umidToken:window.umidToken||server,umidTag:window.umidToken?'WEB':server?'SERVER':'NOT_INIT',
                        umidGetStatusVal:window.um&&window.um.getStatus(true)||'',
                        screenPixel:screen.width+'x'+screen.height,navlanguage:navigator.language,
                        navUserAgent:navigator.userAgent,navPlatform:navigator.platform,
                        isIframe:String(self!==top),documentReferer:document.referrer};
                }''')
                form.update(runtime);form.update(smsCode=code,smsToken=saved['sms_token'],keepLogin='false')
                # Native XHR runs through the official page's normal request hooks.
                response=page.evaluate('''({url,form}) => new Promise((resolve,reject) => {
                    const xhr=new XMLHttpRequest();xhr.open('POST',url);xhr.withCredentials=true;
                    xhr.timeout=15000;xhr.setRequestHeader('Content-Type','application/x-www-form-urlencoded');
                    xhr.onload=()=>{try{resolve(JSON.parse(xhr.responseText))}catch(e){reject(new Error('Invalid login response'))}};
                    xhr.onerror=()=>reject(new Error('Login transport failure'));
                    xhr.ontimeout=()=>reject(new Error('Login response timeout'));
                    xhr.send(new URLSearchParams(form).toString());
                })''',{'url':api,'form':form})
                data=sms_response_data(response)
                if data.get('titleMsg') or data.get('isCheckCodeShowed'):
                    raise PhoneLoginError(sms_failure_reason(data,'短信登录'))
                if data.get('smsRegToken') or data.get('iframeRedirect'):
                    raise PhoneLoginError('平台要求额外确认，请在官方短信登录页完成验证')
                require_sms_success(data)
                urls=list(data.get('asyncUrls') or [])
                if data.get('redirect') and data.get('redirectUrl'):urls.append(data['redirectUrl'])
                if data.get('parentRedirect') and data.get('parentRedirectUrl'):urls.append(data['parentRedirectUrl'])
                for url in urls[:8]:
                    url=official_url(url)
                    for _ in range(5):
                        reply=context.request.get(url,timeout=10000,max_redirects=0)
                        location=reply.headers.get('location')
                        if reply.status not in (301,302,303,307,308) or not location:break
                        url=official_url(urljoin(url,location))
                    else:raise PhoneLoginError('短信登录会话跳转次数过多')
                cookies=context.cookies()
                uid={c['value'] for c in cookies if c['name']=='unb' and c['domain'].lstrip('.') in ('goofish.com','www.goofish.com')}
                if uid!={expected}:raise PhoneLoginError('短信登录身份与要恢复的闲鱼账号不一致，未保存登录态')
                session=requests.Session();session.headers['User-Agent']=runtime['navUserAgent']
                proxy=browser_proxy(config)
                if proxy:
                    session.trust_env=False
                    session.proxies={'http':proxy.replace('socks5://','socks5h://',1),
                        'https':proxy.replace('socks5://','socks5h://',1)}
                for cookie in cookies:
                    if cookie['domain'].lstrip('.') not in ('goofish.com','www.goofish.com','passport.goofish.com'):continue
                    session.cookies.set_cookie(create_cookie(name=cookie['name'],value=cookie['value'],
                        domain=cookie['domain'],path=cookie['path'],secure=cookie['secure'],
                        expires=int(cookie['expires']) if cookie['expires']>0 else None))
                account=XianyuApis(session.cookies.get_dict(),generate_device_id(expected));account.session=session
                return account
        finally:self.path(id).unlink(missing_ok=True)
