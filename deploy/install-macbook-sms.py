"""Install the private SMS worker and a key restricted to that worker."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


def restricted_key(root,public_key,source_ip):
    ipaddress.ip_address(source_ip)
    parts=public_key.split()
    if len(parts)<2 or parts[0]!='ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/=]+',parts[1]):
        raise ValueError('Worker public key is invalid')
    command=shlex.join([str(root/'sms-worker')])
    command=command.replace('\\','\\\\').replace('"','\\"')
    return f'from="{source_ip}",restrict,command="{command}" {parts[0]} {parts[1]} leo-xianyu-sms-worker'


def main():
    if sys.platform!='darwin':raise SystemExit('请在 leo-macbook 的 macOS 终端运行此安装组件。')
    if sys.version_info<(3,10):
        for candidate in ('/opt/homebrew/bin/python3','/usr/local/bin/python3',shutil.which('python3.12'),shutil.which('python3.11')):
            if not candidate or not Path(candidate).is_file() or Path(candidate).resolve()==Path(sys.executable).resolve():continue
            version=subprocess.run([candidate,'-c','import sys; print(int(sys.version_info>=(3,10)))'],capture_output=True,text=True)
            if version.returncode==0 and version.stdout.strip()=='1':os.execv(candidate,[candidate,str(Path(__file__).resolve())])
        raise SystemExit('需要 Python 3.10 或更新版本；请用已安装的 python3.12 运行本安装组件。')
    node=shutil.which('node')
    if not node:raise SystemExit('需要本机已有 Node.js；安装后重新运行即可。')
    source=Path(__file__).resolve().parent.parent
    root=Path.home()/'.local/share/leo-xianyu-sms'
    source_ip=(source/'worker-source-ip.txt').read_text().strip()
    public_key=(source/'worker-authorized-key.pub').read_text().strip()
    line=restricted_key(root,public_key,source_ip)
    root.mkdir(mode=0o700,parents=True,exist_ok=True);root.chmod(0o700)
    if source!=root:shutil.copytree(source,root,dirs_exist_ok=True)
    if not (root/'.venv/bin/python').exists():subprocess.run([sys.executable,'-m','venv',str(root/'.venv')],check=True)
    python=str(root/'.venv/bin/python')
    subprocess.run([python,'-m','pip','install','-r',str(root/'requirements-sms-browser.txt')],check=True)
    executable=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not executable.is_file():raise SystemExit('需要本机 Google Chrome；安装后重新运行即可。')
    config=root/'browser.json'
    fd=os.open(config,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:json.dump({'browser_executable':str(executable),'headless':False,'entry':'home'},f)
    config.chmod(0o600)
    worker=root/'sms-worker'
    command=shlex.join([python,str(root/'web/sms_ssh_worker.py'),
        '--directory',str(root/'state'),'--config',str(config)])
    worker.write_text('#!/bin/sh\nexport PATH='+shlex.quote(str(Path(node).parent))+':"$PATH"\nexec '+command+'\n')
    worker.chmod(0o700)
    ssh=Path.home()/'.ssh';ssh.mkdir(mode=0o700,exist_ok=True)
    authorized=ssh/'authorized_keys'
    existing=authorized.read_text() if authorized.exists() else ''
    if line not in existing.splitlines():
        fd=os.open(authorized,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        with os.fdopen(fd,'a') as f:f.write(('\n' if existing and not existing.endswith('\n') else '')+line+'\n')
    authorized.chmod(0o600)
    print('MacBook 短信执行组件已安装。仅授权来源服务器调用短信组件；无需提供密码或验证码。')


if __name__=='__main__':main()
