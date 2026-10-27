"""Private stdin/stdout protocol; credentials never belong in terminal output."""
import argparse
from contextlib import contextmanager,redirect_stderr,redirect_stdout
import fcntl
import hashlib
import io
import json
from pathlib import Path
import re
import sys

if __package__ in (None,''):
    script_directory=str(Path(__file__).resolve().parent)
    sys.path[:]=[str(Path(__file__).resolve().parents[1])]+[p for p in sys.path if p!=script_directory]

from web.phone_login_browser import BrowserPhoneLogin
from web.phone_login import PhoneLoginError
from web.platform import private_json


class SmsWorker:
    def __init__(self,directory,config=None,adapter=None):
        self.directory=Path(directory)
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.adapter=adapter or BrowserPhoneLogin(self.directory,config or {})

    def path(self,id):return self.directory/'attempts'/(hashlib.sha256(id.encode()).hexdigest()+'.json')

    @contextmanager
    def lock(self,path):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with path.with_suffix('.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

    def dispatch(self,data):
        if isinstance(data,dict) and data.get('action')=='health':
            executable=self.adapter.config.get('browser_executable')
            return {'state':'ready','platform':sys.platform,'browser_installed':bool(executable and Path(executable).is_file())}
        if not isinstance(data,dict) or data.get('action') not in ('start','confirm','status'):
            raise ValueError('短信执行动作无效')
        id=data.get('id','');phone=data.get('phone','');expected=data.get('expected','')
        if not isinstance(id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',id):raise ValueError('请求ID无效')
        if not re.fullmatch(r'1\d{10}',phone) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',expected):raise ValueError('短信绑定信息无效')
        digest=hashlib.sha256((phone+'\n'+expected).encode()).hexdigest();path=self.path(id)
        with self.lock(path):
            saved=json.loads(path.read_text()) if path.exists() else None
            if saved and saved['digest']!=digest:raise ValueError('请求ID绑定信息变化')
            action=data['action']
            if action in ('start','status') and saved:return saved['reply']
            if action=='status':return {'state':'absent'}
            if action=='start':
                record={'digest':digest,'reply':{'state':'uncertain','reason':'发码意图已保存，响应未确认'}}
                private_json(path,record)
                record['reply']=self.adapter.start(id,phone,expected=expected)
                private_json(path,record)
                return record['reply']
            if not saved:raise ValueError('短信登录请求不存在')
            if saved['reply']['state']!='waiting':return saved['reply']
            if not re.fullmatch(r'\d{6}',data.get('code','')):raise ValueError('验证码应为6位')
            saved['reply']={'state':'uncertain','reason':'验证码提交意图已保存，响应未确认'}
            private_json(path,saved)
            try:
                api=self.adapter.confirm(id,phone,data['code'],expected)
                cookies=[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,
                    'secure':c.secure,'expires':c.expires} for c in api.session.cookies]
                saved['reply']={'state':'confirmed','session':{'cookies':cookies,
                    'user_agent':api.session.headers.get('User-Agent','')}}
            except PhoneLoginError as exc:saved['reply']={'state':'needs_user','reason':str(exc)}
            except ValueError:saved['reply']={'state':'needs_user','reason':'本机短信会话已失效，请重新发起'}
            except Exception:pass
            private_json(path,saved)
            return saved['reply']


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True);parser.add_argument('--config',required=True)
    args=parser.parse_args()
    output=sys.stdout
    # Library diagnostics must never mix with the encrypted transport payload.
    with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
        try:
            data=json.loads(sys.stdin.read(16384));config=json.loads(Path(args.config).read_text())
            result=SmsWorker(args.directory,config).dispatch(data)
        except Exception:result={'state':'uncertain','reason':'本机短信执行未确认，请核对执行组件'}
    json.dump(result,output,ensure_ascii=False)


if __name__=='__main__':main()
