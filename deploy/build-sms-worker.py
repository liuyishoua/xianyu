"""Build the private native-worker download from a fixed public-source manifest."""
import argparse
import io
import ipaddress
from pathlib import Path
import tarfile

FILES=('requirements.txt','requirements-web.txt','requirements-sms-browser.txt','LICENSE',
    'THIRD_PARTY_NOTICES.md','goofish_apis.py','goofish_live.py','web/__init__.py',
    'web/platform.py','web/phone_login.py','web/phone_login_browser.py','web/phone_login_ssh.py',
    'web/sms_ssh_worker.py','deploy/install-macbook-sms.py','message/__init__.py',
    'message/types.py','utils/__init__.py','utils/goofish_utils.py','utils/build_cookies.py',
    'static/goofish_js_version_2.js')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--public-key',type=Path,required=True)
    parser.add_argument('--source-ip',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();ipaddress.ip_address(args.source_ip)
    root=Path(__file__).resolve().parents[1]
    public=args.public_key.read_bytes()
    if not public.startswith(b'ssh-ed25519 '):raise ValueError('Expected an SSH public key')
    args.output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tarfile.open(args.output,'w:gz') as archive:
        for name in FILES:
            path=root/name
            if not path.is_file() or path.is_symlink():raise ValueError('Worker source manifest is incomplete')
            archive.add(path,arcname='leo-xianyu-sms/'+name,recursive=False)
        for name,data in [('worker-authorized-key.pub',public),('worker-source-ip.txt',(args.source_ip+'\n').encode())]:
            info=tarfile.TarInfo('leo-xianyu-sms/'+name);info.size=len(data);info.mode=0o600
            archive.addfile(info,io.BytesIO(data))
    args.output.chmod(0o600)


if __name__=='__main__':main()
