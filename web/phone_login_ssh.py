"""Run official SMS browser actions on an explicitly configured owned computer."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

import requests
from requests.cookies import create_cookie

from goofish_apis import XianyuApis
from utils.goofish_utils import generate_device_id
from .phone_login import PhoneLogin, PhoneLoginError
from .platform import private_json


class SshPhoneLogin(PhoneLogin):
    def __init__(self,directory,config):
        super().__init__(directory)
        self.config=config
        self.ssh=config['ssh']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',self.ssh['host']) or not re.fullmatch(r'[A-Za-z0-9_.-]+',self.ssh['user']):
            raise ValueError('短信执行主机配置无效')
        if not self.ssh.get('known_hosts') or not Path(self.ssh['known_hosts']).is_file():
            raise ValueError('请先核验短信执行主机身份')

    def invoke(self,payload):
        root=self.ssh.get('remote_root','.local/share/leo-xianyu-sms')
        if root.startswith('-') or not root or '\x00' in root:raise ValueError('短信执行目录无效')
        remote=shlex.join([root+'/sms-worker'])
        command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=4',
            '-o','UserKnownHostsFile='+self.ssh['known_hosts']]
        identity=self.ssh.get('identity_file')
        if identity:command+=['-i',identity,'-o','IdentitiesOnly=yes']
        command += [self.ssh['user']+'@'+self.ssh['host'],remote]
        try:
            reply=subprocess.run(command,input=json.dumps(payload),capture_output=True,text=True,timeout=28)
            if reply.returncode:raise RuntimeError('Remote SMS action unavailable')
            result=json.loads(reply.stdout)
            if not isinstance(result,dict):raise ValueError('Invalid SMS reply')
            return result
        except Exception:
            # Do not expose stderr, command output, SMS codes or session cookies.
            return {'state':'uncertain','reason':'本机短信执行响应未确认，未重复发送或提交验证码'}

    def start(self,id,phone,expected=None):
        if not re.fullmatch(r'1\d{10}',phone) or not expected:raise ValueError('短信账号绑定信息不完整')
        result=self.invoke({'action':'start','id':id,'phone':phone,'expected':expected})
        if result.get('state')=='confirmed':
            return {'state':'needs_user','reason':'本机已完成过此请求，请核对工作台登录态'}
        if result.get('state')=='waiting':
            private_json(self.path(id),{'engine':'ssh','ssh_config':self.config,
                'phone_digest':hashlib.sha256(phone.encode()).hexdigest(),'expected':expected,'expires':time.time()+180})
        return {k:result[k] for k in ('state','reason','remote_id') if k in result}

    def confirm(self,id,phone,code,expected):
        saved=json.loads(self.path(id).read_text())
        if saved['expires']<time.time() or saved['phone_digest']!=hashlib.sha256(phone.encode()).hexdigest() or saved['expected']!=expected:
            raise ValueError('短信执行会话已过期或绑定变化')
        if not re.fullmatch(r'\d{6}',code):raise ValueError('验证码应为6位')
        try:
            result=self.invoke({'action':'confirm','id':id,'phone':phone,'code':code,'expected':expected})
            if result.get('state')=='uncertain':raise RuntimeError('Remote confirmation uncertain')
            if result.get('state')!='confirmed':raise PhoneLoginError(str(result.get('reason') or '本机短信登录未确认'))
            receipt=result['session'];cookies=receipt['cookies']
            owned={c['value'] for c in cookies if c['name']=='unb' and c['domain'].lstrip('.') in ('goofish.com','www.goofish.com')}
            if owned!={expected}:raise PhoneLoginError('本机短信登录身份与原账号不一致，未保存登录态')
            session=requests.Session()
            if receipt.get('user_agent'):session.headers['User-Agent']=receipt['user_agent']
            for cookie in cookies:
                if cookie['domain'].lstrip('.') not in ('goofish.com','www.goofish.com','passport.goofish.com'):continue
                session.cookies.set_cookie(create_cookie(**cookie))
            api=XianyuApis(session.cookies.get_dict(),generate_device_id(expected));api.session=session
            return api
        finally:self.path(id).unlink(missing_ok=True)
