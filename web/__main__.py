import argparse
from pathlib import Path
from .app import create_app


def main():
    parser = argparse.ArgumentParser(description='闲鱼工作台')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--no-auth', action='store_true', help='仅用于本机预览')
    parser.add_argument('--public-origin')
    args = parser.parse_args()
    if args.no_auth and (args.host not in ('127.0.0.1', 'localhost') or args.public_origin):
        parser.error('--no-auth 仅允许本机回环地址且不能配置公网来源')
    adapter = None
    if args.demo:
        from .demo import DemoPlatform
        adapter = DemoPlatform()
    directory = args.data_dir or Path(__file__).resolve().parents[1]/'.local'/('web-demo' if args.demo else 'web')
    app = create_app(directory, platform=adapter, auth=not args.no_auth, public_origin=args.public_origin)
    if args.demo:
        from .demo import seed
        seed(app.extensions['workspace'])
        from .service import Service
        accounts = app.extensions['accounts']
        second = Service(directory, DemoPlatform('demo-studio', '慢慢生活 · 家居店'), store=accounts.store)
        accounts.services['demo-studio'] = second
        seed(second)
    from .workspace_identity import install_identity
    def tenant_app(path):
        from .platform import Platform
        child=create_app(path, platform=Platform(path/'bootstrap-session.json'), auth=False, public_origin=args.public_origin)
        child.extensions['monitor'].start()
        child.extensions['business'].start()
        return child
    if not args.demo:
        install_identity(app, directory, 'xianyu', tenant_app, machine_prefixes=('/maintenance/',))
    app.extensions['monitor'].start()
    app.extensions['business'].start()
    from waitress import serve
    print(f'闲鱼工作台 http://{args.host}:{args.port}（{"演示" if args.demo else "真实"}模式）', flush=True)
    if not args.no_auth: print(f'工作台访问密码保存在 {directory}/admin-password', flush=True)
    serve(app, host=args.host, port=args.port, threads=6)


if __name__ == '__main__': main()
