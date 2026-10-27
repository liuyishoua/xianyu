"""Official Goofish SMS form adapter. Challenges remain owner actions."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

import requests
from requests.cookies import create_cookie
from goofish_apis import XianyuApis
from utils.goofish_utils import generate_device_id
from utils.build_cookies import UA
from .platform import private_json, PlatformError

PASSPORT='https://passport.goofish.com'
LOGIN=PASSPORT+'/mini_login.htm'


class PhoneLoginError(PlatformError):
    """A sanitized official SMS error that can be shown in the workbench."""


def sms_response_data(payload):
    if not isinstance(payload,dict):return {}
    content=payload.get('content')
    data=content.get('data') if isinstance(content,dict) else None
    if isinstance(data,dict):return data
    # An access-verification response may replace the normal login envelope.
    outer=payload.get('data');outer=outer if isinstance(outer,dict) else {}
    url=str(payload.get('url') or outer.get('url') or '')
    ret=str(payload.get('ret') or '')
    if payload.get('rgv587_flag') or re.search(r'/(?:punish|captcha|verify)(?:[/?]|$)',url) or any(code in ret for code in ('USER_VALIDATE','RGV587')):
        return {'isCheckCodeShowed':True}
    return {k:v for k,v in payload.items() if k in ('resultCode','titleMsg','message')}


def sms_response_diagnostic(payload,status):
    def keys(value):return sorted(k for k in value if re.fullmatch(r'[A-Za-z0-9_]{1,64}',k))[:64] if isinstance(value,dict) else []
    content=payload.get('content') if isinstance(payload,dict) else None
    data=content.get('data') if isinstance(content,dict) else None
    return {'http_status':status,'root_fields':keys(payload),'content_fields':keys(content),
        'data_fields':keys(data),'requires_verification':bool(sms_response_data(payload).get('isCheckCodeShowed'))}


def sms_failure_reason(data,phase):
    if data.get('isCheckCodeShowed'):
        return f'闲鱼要求{phase}安全验证，请在官方短信登录页完成验证'
    message=str(data.get('titleMsg') or data.get('message') or '')
    message=re.sub(r'https?://\S+','[链接已隐藏]',message)
    message=re.sub(r'\d{4,}|[A-Za-z0-9_=-]{20,}','[已隐藏]',message)[:120]
    code=str(data.get('resultCode') or '')
    suffix=f'（错误码 {code}）' if re.fullmatch(r'-?\d{1,8}',code) else ''
    return f'闲鱼{phase}未成功{suffix}'+('：'+message if message else '，官方没有返回成功凭证')


def require_sms_success(data):
    code=data.get('resultCode')
    if code is not None and str(code)!='100':
        raise PhoneLoginError(sms_failure_reason(data,'短信登录'))
    if not (str(code)=='100' or data.get('loginSuccess') is True or data.get('st')
            or data.get('redirect') or data.get('parentRedirect') or data.get('asyncUrls')):
        raise PhoneLoginError('闲鱼未确认短信登录成功，未保存登录态')


def create_phone_login(directory):
    path=Path(directory)/'phone-login-config.json'
    config=json.loads(path.read_text()) if path.exists() else {}
    engine=config.get('engine','requests')
    if engine=='browser':
        if config.get('interactive'):
            from .phone_login_native import NativePhoneLogin
            return NativePhoneLogin(directory,config)
        from .phone_login_browser import BrowserPhoneLogin
        return BrowserPhoneLogin(directory,config)
    if engine=='ssh':
        from .phone_login_ssh import SshPhoneLogin
        return SshPhoneLogin(directory,config)
    if engine!='requests':raise ValueError('短信登录引擎配置无效')
    return PhoneLogin(directory,config=config)


def window_json(html,name):
    match=re.search(r'window\.'+re.escape(name)+r'\s*=\s*',html)
    if not match:raise PlatformError('官方登录页面结构已变化，请使用扫码登录')
    return json.JSONDecoder().raw_decode(html[match.end():])[0]


def rsa_mobile(phone,modulus,exponent):
    # The official havana-nlogin RSAKey.encrypt uses PKCS#1 v1.5 and hex output.
    if not re.fullmatch(r'[0-9a-fA-F]{128,1024}',modulus) or not re.fullmatch(r'[0-9a-fA-F]{1,8}',exponent):raise PlatformError('官方短信登录公钥格式变化')
    n=int(modulus,16);e=int(exponent,16);width=(n.bit_length()+7)//8;message=phone.encode()
    if e<3 or not e%2 or len(message)>width-11:raise PlatformError('官方短信登录公钥不可用')
    padding=bytes(secrets.randbelow(255)+1 for _ in range(width-len(message)-3))
    value=pow(int.from_bytes(b'\x00\x02'+padding+b'\x00'+message,'big'),e,n)
    encoded=format(value,'x')
    return encoded if len(encoded)%2==0 else '0'+encoded


class PhoneLogin:
    def __init__(self,directory,session_factory=requests.Session,config=None):
        self.directory=Path(directory)/'phone-login';self.session_factory=session_factory
        self.config=config or {}

    def path(self,id):return self.directory/(hashlib.sha256(id.encode()).hexdigest()+'.json')

    def session(self,config=None):
        s=self.session_factory();s.headers.update({'User-Agent':UA,'Referer':LOGIN,'Origin':PASSPORT})
        from .phone_login_browser import browser_proxy
        proxy=browser_proxy(self.config if config is None else config)
        if proxy:
            proxy=proxy.replace('socks5://','socks5h://',1)
            s.proxies.update({'http':proxy,'https':proxy});s.trust_env=False
        return s

    def start(self,id,phone,expected=None):
        if not re.fullmatch(r'1\d{10}',phone):raise ValueError('手机号须为11位')
        s=self.session()
        page=s.get(LOGIN,params={'appName':'xianyu','appEntrance':'web','lang':'zh_cn','styleType':'vertical','fromSite':'77','qrCodeFirst':'false'},timeout=15)
        page.raise_for_status();view=window_json(page.text,'viewData');config=window_json(page.text,'viewConfig')
        encrypted=rsa_mobile(phone,config['rsaModulusSec'],config['rsaExponentSec']) if config.get('rsaModulusSec') and config.get('rsaExponentSec') else None
        form={**view['loginFormData'],'loginId':phone if not encrypted else '','phoneCode':'86','countryCode':'CN','codeLength':6,
              'defaultView':'sms','isIframe':'true','navlanguage':'zh-CN','navPlatform':'Win32','navUserAgent':UA,
              'documentReferer':'https://www.goofish.com/','umidTag':'SERVER','jsVersion':'0.10.37'}
        if encrypted:form['loginId2']=encrypted
        form={k:('true' if v else 'false') if isinstance(v,bool) else v for k,v in form.items()}
        response=s.post(PASSPORT+'/newlogin/sms/send.do',params={'appName':'xianyu','fromSite':'77'},data=form,timeout=15)
        raw=response.json()
        data=sms_response_data(raw)
        if self.config.get('diagnostic_mode'):
            status=response.status_code if type(response.status_code) is int else None
            private_json(self.path(id).with_suffix('.diagnostic.json'),{
                **sms_response_diagnostic(raw,status),
                'engine':'requests','configured_proxy':bool(self.config.get('browser_proxy')),
                'sms_success_observed':bool(data.get('smsToken') and not data.get('isCheckCodeShowed'))})
        if data.get('isCheckCodeShowed') or not data.get('smsToken'):
            return {'state':'needs_user','reason':sms_failure_reason(data,'发码')}
        # Keep passport cookies/tokens only inside this workbench, never in the central service.
        private_json(self.path(id),{'form':form,'phone_digest':hashlib.sha256(phone.encode()).hexdigest(),'sms_token':data['smsToken'],'expires':time.time()+180,
            'network_config':{k:self.config[k] for k in ('browser_proxy',) if k in self.config},
            'cookies':[dict(name=c.name,value=c.value,domain=c.domain,path=c.path,secure=c.secure,expires=c.expires) for c in s.cookies]})
        return {'state':'waiting','remote_id':id}

    def confirm(self,id,phone,code,expected):
        saved=json.loads(self.path(id).read_text())
        if saved.get('engine')=='native_browser':
            from .phone_login_native import NativePhoneLogin
            return NativePhoneLogin(self.directory.parent,saved['browser_config']).confirm(id,phone,code,expected)
        if saved.get('engine')=='browser':
            from .phone_login_browser import BrowserPhoneLogin
            return BrowserPhoneLogin(self.directory.parent,saved.get('browser_config')).confirm(id,phone,code,expected)
        if saved.get('engine')=='ssh':
            from .phone_login_ssh import SshPhoneLogin
            return SshPhoneLogin(self.directory.parent,saved['ssh_config']).confirm(id,phone,code,expected)
        if saved['expires']<time.time() or saved['phone_digest']!=hashlib.sha256(phone.encode()).hexdigest():raise ValueError('短信登录会话已过期或手机号变化')
        if not re.fullmatch(r'\d{6}',code):raise ValueError('验证码应为6位')
        s=self.session(saved.get('network_config',self.config))
        for c in saved['cookies']:s.cookies.set_cookie(create_cookie(**c))
        form={**saved['form'],'smsCode':code,'smsToken':saved['sms_token'],'keepLogin':'false'}
        try:
            raw=s.post(PASSPORT+'/newlogin/sms/login.do',params={'appName':'xianyu','fromSite':'77'},data=form,timeout=15).json()
            data=sms_response_data(raw)
            if data.get('titleMsg') or data.get('isCheckCodeShowed'):
                raise PhoneLoginError(sms_failure_reason(data,'短信登录'))
            if data.get('smsRegToken') or data.get('isCheckCodeShowed') or data.get('iframeRedirect'):
                raise PlatformError('平台要求额外确认，请在闲鱼官网完成验证')
            require_sms_success(data)
            # Honor only first-party session handoff URLs supplied by the official response.
            urls=list(data.get('asyncUrls') or [])
            if data.get('redirect') and data.get('redirectUrl'):urls.append(data['redirectUrl'])
            for url in urls[:8]:
                u=urlsplit(url)
                if u.scheme!='https' or u.username or u.password or u.port not in (None,443) or u.hostname not in ('passport.goofish.com','www.goofish.com','login.taobao.com','passport.taobao.com'):
                    raise PlatformError('官方登录返回了需人工核对的跳转')
                s.get(url,timeout=10,allow_redirects=False)
            uid=s.cookies.get('unb',domain='.goofish.com') or s.cookies.get('unb',domain='www.goofish.com') or ''
            if not uid or uid!=expected:raise PlatformError('短信登录身份与要恢复的闲鱼账号不一致，未保存登录态')
            api=XianyuApis({c.name:c.value for c in s.cookies if c.domain.endswith('goofish.com')},generate_device_id(uid));api.session=s
            return api
        finally:self.path(id).unlink(missing_ok=True)
