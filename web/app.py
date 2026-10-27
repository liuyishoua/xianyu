import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_file, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from .platform import Platform, PlatformError, save_session, private_json
from .service import Service

ROOT = Path(__file__).resolve().parents[1]


def create_app(directory=None, platform=None, auth=True, public_origin=None, workspace=None, accounts=None, monitor=None):
    directory = Path(directory or ROOT / '.local' / 'web')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = Flask(__name__, static_folder='static')
    from .page_assets import install_pages
    install_pages(app, {'index.html','login.html','automation.html'}, bundles={'app.js':'app.bundle.mjs'})
    key = directory / 'session-key'
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: f.write(secrets.token_hex(32))
    password_file = directory / 'admin-password'
    if auth and not password_file.exists():
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: f.write(secrets.token_urlsafe(15))
    password_hash = generate_password_hash(password_file.read_text().strip()) if auth else None
    app.config.update(SECRET_KEY=key.read_text(), MAX_CONTENT_LENGTH=9 * 1024 * 1024,
        SESSION_COOKIE_NAME='xianyu_console', SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=bool(public_origin and public_origin.startswith('https:')),
        PERMANENT_SESSION_LIFETIME=3600 * 12)
    service = workspace or Service(directory, platform or Platform(ROOT / '.local' / 'session.json'))
    from .accounts import Accounts
    from .monitoring import Monitoring
    accounts = accounts or Accounts(service)
    monitor = monitor or Monitoring(accounts, directory / 'channel.json')
    app.extensions['workspace'] = service
    app.extensions['accounts'] = accounts
    app.extensions['monitor'] = monitor
    children = {}
    from .channel_bridge import register_channel
    register_channel(app, service, directory/'channel.json')
    from .business import register as register_business
    register_business(app, accounts, monitor)
    from .account_maintenance import register as register_maintenance
    register_maintenance(app, accounts, directory, monitor)
    attempts = {}

    @app.before_request
    def guard():
        if request.path.startswith('/maintenance/'): return
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            expected = public_origin or request.host_url.rstrip('/')
            if origin and origin.rstrip('/') != expected:
                return jsonify(error='请求来源无效，请从工作台操作'), 403
            if request.headers.get('X-App-Request') != '1':
                return jsonify(error='请求来源无效'), 403
        if request.path in ('/api/login', '/login') or request.path.startswith('/static/'):
            return
        if auth and not session.get('authenticated'):
            if request.path.startswith('/api/'): return jsonify(error='请先登录工作台'), 401
            return redirect('/login')
        selected = request.headers.get('X-Account-ID') if request.path.startswith('/api/') else None
        if request.path.startswith(('/api/media/', '/api/account/qr/')):
            selected = selected or request.args.get('account_id')
        if not request.path.startswith('/api/business/') and selected and selected != str((service.platform.identity() or {}).get('id', '')):
            try: target = accounts.resolve(selected)
            except LookupError: return jsonify(error='所选账号不存在，请重新选择账号'), 404
            with accounts.lock:
                if selected not in children:
                    children[selected] = create_app(directory, auth=auth, public_origin=public_origin,
                        workspace=target, accounts=accounts, monitor=monitor)
                child = children[selected]
            with child.request_context(request.environ): return child.full_dispatch_request()
        expected_account = request.headers.get('X-Execution-Account')
        if expected_account and request.path.startswith('/api/') and not request.path.startswith('/api/business/'):
            service.lock.acquire()
            g.execution_account_lock = True
            identity = service.platform.identity()
            if not identity or str(identity['id']) != expected_account:
                return jsonify(error='执行账号已变化，已停止该连接器请求'), 409
        if request.path.startswith('/api/') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.is_json:
                data = request.get_json(silent=True)
                if not isinstance(data, dict): return jsonify(error='请求内容必须是对象'), 400
                # Return an existing operation before any operation-specific preflight.
                rid = data.get('request_id')
                identity = service.platform.identity()
                if not request.path.startswith('/api/business/') and data.get('expected_account_id') is not None and (not identity or str(data['expected_account_id']) != str(identity['id'])):
                    return jsonify(error='执行账号与当前连接账号不一致，尚未发送'), 409
                if rid and identity and not request.path.startswith('/api/business/'):
                    with service.store.connect() as db:
                        row = db.execute('SELECT * FROM jobs WHERE uid=? AND request_id=?', (identity['id'], rid)).fetchone()
                    if row: return jsonify(service.store.job_projection(row)), 202
            elif request.path != '/api/media': return jsonify(error='请求格式无效'), 400

    @app.teardown_request
    def release_execution_account_lock(_error):
        if g.pop('execution_account_lock', False):
            service.lock.release()

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/') or request.path == '/login': response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, LookupError): return jsonify(error='记录不存在'), 404
        if isinstance(exc, (ValueError, PlatformError)): return jsonify(error=str(exc)), 400
        if isinstance(exc, HTTPException): return jsonify(error='请求无效或文件过大'), exc.code
        app.logger.error('Workspace failure: %s', type(exc).__name__)
        return jsonify(error='操作未完成，请检查服务状态'), 500

    @app.get('/login')
    def login_page(): return app.send_static_file('login.html')

    @app.post('/api/login')
    def login():
        if not auth: return {'ok': True}
        now = time.time(); ip = request.remote_addr
        attempts[ip] = [x for x in attempts.get(ip, []) if now-x < 60]
        if len(attempts[ip]) >= 8: return jsonify(error='尝试过于频繁，请稍后再试'), 429
        data = request.get_json(silent=True) or {}
        if not check_password_hash(password_hash, str(data.get('password', ''))):
            attempts[ip].append(now)
            return jsonify(error='访问密码不正确'), 401
        session.clear(); session['authenticated'] = True; session.permanent = True
        return {'ok': True}

    @app.post('/api/logout')
    def logout(): session.clear(); return {'ok': True}

    @app.get('/')
    @app.get('/<path:path>')
    def index(path=''):
        if path.startswith('api/') or path.startswith('.'): return jsonify(error='记录不存在'), 404
        return app.send_static_file('index.html')

    @app.get('/api/meta')
    def meta():
        return {'demo': bool(getattr(service.platform, 'demo', False)), 'account': service.platform.identity(),
                'accounts': accounts.list(), 'now': time.time(), 'authenticated': bool(auth), 'version': '2.0'}

    @app.get('/api/accounts')
    def account_matrix():
        from .phone_login_native import active_requests
        sms=active_requests(directory)
        return {'items': [{**row, 'dm': monitor.config(accounts.resolve(row['id']), 'dm'),
            'comment': monitor.config(accounts.resolve(row['id']), 'comment'),
            'sms_request':sms.get(row['id'])} for row in accounts.list()]}

    @app.patch('/api/accounts/<uid>')
    def rename_account(uid):
        target = accounts.resolve(uid)
        name = str(request.json.get('name', '')).strip()
        if not name or len(name) > 80: raise ValueError('账号备注应为 1 到 80 字')
        return service.store.put(accounts.scope, 'account-label', {'name': name}, uid)

    @app.post('/api/accounts/<uid>/browser-session')
    def import_browser_account(uid):
        from .session_import import import_browser_session
        data=request.get_json(silent=True) or {}
        return import_browser_session(accounts,monitor,directory,uid,data.get('cookie'))

    def sms_browser(id):
        from .phone_login_native import NativePhoneLogin
        return NativePhoneLogin(directory)

    @app.get('/api/account/sms/<id>')
    def sms_browser_view(id):
        return sms_browser(id).view(id)

    @app.get('/api/account/sms/<id>/frame')
    def sms_browser_frame(id):
        return send_file(io.BytesIO(sms_browser(id).frame(id)),mimetype='image/jpeg')

    @app.post('/api/account/sms/<id>/control')
    def sms_browser_control(id):
        return sms_browser(id).control(id,request.get_json())

    @app.get('/api/models')
    def models():
        data = monitor.models(service)
        try: bindings = monitor.model_bindings(service, data) if data.get('connected') else service.store.get(service.uid(), 'settings', 'model-bindings')
        except (LookupError, PlatformError): bindings = {}
        from .monitoring import agent_snapshot
        return {**data, 'bindings': bindings, 'agent_bindings': {p: agent_snapshot(data, bindings.get(p, '')).get('id', '') for p in ('dm', 'comment', 'content')}}

    @app.put('/api/models')
    def save_models():
        from .monitoring import PURPOSES, available, resolve_agent, agent_snapshot, bound_route
        catalog = monitor.client(service).catalog()
        bindings = {}
        for purpose in PURPOSES:
            route_id = str(request.json.get(purpose) or '')
            if 'agent_bindings' in request.json:
                try: previous = service.store.get(service.uid(), 'settings', 'model-bindings')
                except LookupError: previous = {}
                agent_data = request.json['agent_bindings']
                if not isinstance(agent_data, dict): raise ValueError('Agent 配置格式无效')
                route_id = resolve_agent(catalog, purpose, service.uid(), str(agent_data.get(purpose) or ''), previous.get(purpose, ''), client=monitor.client(service))
            if route_id and not bound_route(catalog, purpose, service.uid(), route_id):
                raise ValueError('请选择该用途可用的 Agent')
            bindings[purpose] = route_id
        bindings['agents'] = {p: agent_snapshot(catalog, bindings[p]) for p in PURPOSES}
        bindings['explicit_empty'] = [p for p in PURPOSES if not bindings[p]]
        return service.store.put(service.uid(), 'settings', bindings, 'model-bindings')

    def validate_surface(surface):
        if surface not in ('dm', 'comment'): raise LookupError()

    @app.get('/api/monitoring/<surface>')
    def monitor_view(surface):
        validate_surface(surface)
        return monitor.view(service, surface)

    @app.put('/api/monitoring/<surface>')
    def monitor_update(surface):
        validate_surface(surface)
        return monitor.update(service, surface, request.json)

    @app.post('/api/monitoring/<surface>/poll')
    def monitor_poll(surface):
        validate_surface(surface)
        if not monitor.config(service, surface)['enabled']: raise ValueError('请先开启监听')
        if monitor.config(service, surface).get('blocked_auth'):
            raise ValueError('请先完成账号登录或安全验证，再到账号矩阵检查连接')
        monitor.pool.submit(monitor.tick, service, surface)
        return {'ok': True}

    @app.post('/api/monitoring/<surface>/demo-event')
    def demo_monitor_event(surface):
        validate_surface(surface)
        if not getattr(service.platform, 'demo', False): raise ValueError('真实模式不能生成模拟消息')
        if not monitor.config(service, surface)['enabled']: raise ValueError('请先开启监听')
        message = {'id': uuid.uuid4().hex, 'sender_id': '800000', 'outgoing': False,
            'kind': 'text', 'text': '你好，商品还在吗？支持同城自提吗？', 'images': [], 'timestamp': time.time()}
        if surface == 'dm':
            message['cid'] = '900000'
            service.platform.incoming.append(message)
        else:
            message['item_id'] = '1000000001'
            service.platform.comment_rows.append(message)
        monitor.pool.submit(monitor.tick, service, surface)
        return {'ok': True}

    @app.post('/api/monitoring/<surface>/<item_id>/approve')
    def monitor_approve(surface, item_id):
        validate_surface(surface)
        return jsonify(monitor.approve(service, surface, item_id)), 202

    @app.post('/api/monitoring/<surface>/<item_id>/reanalyze')
    def reanalyze_inbox(surface, item_id):
        validate_surface(surface)
        return monitor.reanalyze(service, surface, item_id)

    @app.post('/api/monitoring/<surface>/<item_id>/dismiss')
    def monitor_dismiss(surface, item_id):
        validate_surface(surface)
        return monitor.dismiss(service, surface, item_id)

    @app.get('/api/overview')
    def overview():
        uid = service.uid()
        products = service.store.list(uid, 'product')
        conversations = service.store.list(uid, 'conversation')
        jobs = service.store.jobs(uid, 200)
        snapshots = service.store.list(uid, 'snapshot')
        return {'products': products, 'conversations': conversations[:5], 'jobs': jobs[:8],
                'stats': {'products': len(products), 'online': sum(p.get('status') == '在线' for p in products),
                'views': sum(int(p.get('views') or 0) for p in products),
                'wants': sum(int(p.get('wants') or 0) for p in products),
                'collects': sum(int(p.get('collects') or 0) for p in products),
                'conversations': len(conversations), 'drafts': len(service.store.list(uid, 'draft')),
                'sends': sum(j['kind']=='send' and j['status']=='completed' for j in jobs)},
                'snapshots': sorted(snapshots, key=lambda x: x['at'])[-200:]}

    @app.get('/api/products')
    def products(): return {'items': service.store.list(service.uid(), 'product')}

    @app.post('/api/products/import')
    def import_product():
        d = request.get_json(); return jsonify(service.import_item(d.get('value', ''), d.get('request_id'))), 202

    @app.post('/api/products/<item_id>/refresh')
    def refresh_product(item_id):
        service.store.get(service.uid(), 'product', item_id)
        return jsonify(service.import_item(item_id, request.json.get('request_id'))), 202

    @app.patch('/api/products/<item_id>')
    def annotate_product(item_id):
        uid = service.uid(); item = service.store.get(uid, 'product', item_id)
        d = request.get_json()
        item['note'] = str(d.get('note', ''))[:1000]
        item['tags'] = str(d.get('tags', ''))[:150]
        return service.store.put(uid, 'product', item)

    @app.get('/api/conversations')
    def conversations(): return {'items': service.store.list(service.uid(), 'conversation')}

    @app.post('/api/conversations/sync')
    def sync(): return jsonify(service.sync(request.json.get('request_id'))), 202

    @app.post('/api/conversations/create')
    def create_conversation():
        import re
        d = request.get_json(); peer = str(d.get('peer_id', '')); item = str(d.get('item_id', ''))
        if not re.fullmatch(r'\d{5,24}', peer) or not re.fullmatch(r'\d{6,24}', item):
            raise ValueError('请输入对方数字用户 ID 和对方商品 ID')
        def work(uid):
            result = service.platform.socket('create', peer_id=peer, item_id=item)
            return service.store.put(uid, 'conversation', {**result, 'name': '用户 '+peer[-4:], 'preview': '', 'updated_at': time.time()})
        return jsonify(service.job('create', '主动创建会话', d.get('request_id'), work)), 202

    @app.patch('/api/conversations/<cid>')
    def annotate_conversation(cid):
        uid = service.uid(); conv = service.store.get(uid, 'conversation', cid)
        for key in ('name', 'note', 'item_id'):
            if key in request.json: conv[key] = str(request.json[key])[:500]
        return service.store.put(uid, 'conversation', conv)

    @app.get('/api/conversations/<cid>/messages')
    def messages(cid):
        uid = service.uid(); conv = service.store.get(uid, 'conversation', cid)
        items = [m for m in service.store.list(uid, 'message') if m['cid'] == cid]
        items.sort(key=lambda m: float(m.get('timestamp') or 0))
        return {'conversation': conv, 'items': items[-500:], 'limited': len(items)>500}

    @app.post('/api/conversations/<cid>/history')
    def history(cid):
        d = request.get_json()
        return jsonify(service.history(cid, bool(d.get('older')), d.get('request_id'))), 202

    @app.post('/api/conversations/<cid>/send')
    def send(cid):
        with service.lock:
            return jsonify(service.send(cid, request.get_json())), 202

    @app.get('/api/drafts')
    def drafts(): return {'items': service.store.list(service.uid(), 'draft')}

    @app.post('/api/drafts')
    def draft(): return service.draft(request.get_json())

    @app.put('/api/drafts/<draft_id>')
    def edit_draft(draft_id): return service.draft(request.get_json(), draft_id)

    @app.delete('/api/drafts/<draft_id>')
    def delete_draft(draft_id):
        uid = service.uid(); draft = service.store.get(uid, 'draft', draft_id)
        if draft['status'] == 'publishing': raise ValueError('发布过程中不能删除草稿')
        service.store.delete(uid, 'draft', draft_id); return {'ok': True}

    @app.post('/api/drafts/<draft_id>/publish')
    def publish(draft_id): return jsonify(service.publish(draft_id, request.json.get('request_id'))), 202

    @app.post('/api/media')
    def media():
        if 'file' not in request.files: raise ValueError('请选择图片')
        return service.media(request.files['file'])

    @app.get('/api/media/<media_id>')
    def media_file(media_id):
        value = service.store.get(service.uid(), 'media', media_id)
        return send_file(service.media_dir / value['filename'], mimetype='image/png')

    @app.get('/api/templates')
    def templates(): return {'items': service.store.list(service.uid(), 'template')}

    def save_template(template_id=None):
        d = request.get_json(); title = str(d.get('title', '')).strip(); text = str(d.get('text', '')).strip()
        if not title or not text or len(text)>3000 or len(title)>80: raise ValueError('填写标题和正文，正文最多 3000 字')
        if template_id: service.store.get(service.uid(), 'template', template_id)
        return service.store.put(service.uid(), 'template', {'title': title, 'text': text}, template_id)
    app.add_url_rule('/api/templates', 'new_template', save_template, methods=['POST'])
    app.add_url_rule('/api/templates/<template_id>', 'edit_template', save_template, methods=['PUT'])

    @app.delete('/api/templates/<template_id>')
    def delete_template(template_id):
        service.store.delete(service.uid(), 'template', template_id); return {'ok': True}

    @app.get('/api/jobs')
    def jobs(): return {'items': service.store.jobs(service.uid())}

    @app.get('/api/jobs/<job_id>')
    def job(job_id): return service.get_job(job_id)

    @app.get('/api/operations/<request_id>')
    def operation(request_id): return service.get_operation(request_id)

    @app.get('/api/account')
    def account():
        identity = service.platform.identity()
        checked = None
        if identity:
            try: checked = service.store.get(identity['id'], 'settings', 'account-check')
            except LookupError: pass
        return {'account': identity, 'check': checked, 'login': {k:v for k,v in service.login.items() if k!='image'}}

    @app.get('/api/account/sms-executor-package')
    def sms_executor_package():
        package=directory/'sms-worker-package.tar.gz'
        if not package.is_file():raise LookupError()
        return send_file(package,mimetype='application/gzip',as_attachment=True,
            download_name='leo-xianyu-sms.tar.gz')

    @app.post('/api/account/check')
    def check_account():
        def work(uid):
            try: result = service.platform.check()
            except PlatformError as exc:
                service.store.put(uid, 'settings', {'checked_at': time.time(), 'can_message': False,
                    'error': str(exc)}, 'account-check')
                raise
            monitor.connection_restored(service)
            return service.store.put(uid, 'settings', result, 'account-check')
        return jsonify(service.job('check', '检查账号连接', request.json.get('request_id'), work)), 202

    @app.get('/api/locations')
    def locations():
        try: items = service.store.get(service.uid(), 'settings', 'locations')['items']
        except LookupError: items = []
        return {'items': [{'index': i, 'label': ' · '.join(str(x.get(k) or '') for k in ('prov', 'city', 'area', 'poi'))} for i,x in enumerate(items)]}

    @app.post('/api/locations/refresh')
    def refresh_locations():
        def work(uid):
            items = service.platform.locations()
            service.store.put(uid, 'settings', {'items': items}, 'locations')
            return {'count': len(items)}
        return jsonify(service.job('locations', '读取发货地点', request.json.get('request_id'), work)), 202

    @app.post('/api/category/recommend')
    def recommend():
        description = str(request.json.get('description', '')).strip()
        if not description: raise ValueError('请先填写商品描述')
        return jsonify(service.job('recommend', '推荐商品类目', request.json.get('request_id'),
                       lambda uid: service.platform.recommend(description, []))), 202

    @app.post('/api/account/qr')
    def qr():
        if getattr(service.platform, 'demo', False): raise ValueError('演示空间不会发起真实扫码')
        from .platform import qrcode_login
        import qrcode
        expected = request.json.get('reconnect_account_id')
        if expected: accounts.resolve(str(expected))
        with service.login_lock:
            if service.login['status'] in ('starting', 'waiting', 'scanned', 'finishing'): return {k:v for k,v in service.login.items() if k!='image'}
            qr_id = uuid.uuid4().hex
            service.login = {'id': qr_id, 'status': 'starting'}
        def run():
            def on_qr(url):
                buffer = io.BytesIO(); qrcode.make(url).save(buffer, format='PNG')
                with service.login_lock:
                    service.login.update(status='waiting', image=buffer.getvalue(), expires_at=time.time()+120)
            def on_status(status):
                with service.login_lock:
                    service.login['status'] = {'NEW':'waiting', 'SCANNED':'scanned', 'SCANED':'scanned',
                                               'CONFIRMED':'finishing', 'EXPIRED':'expired'}.get(status, 'waiting')
            try:
                api = qrcode_login(timeout=120, show_qrcode=False, on_qrcode=on_qr, on_status=on_status)
                uid = accounts.register(api, str(expected) if expected else None)
                target = accounts.resolve(uid)
                if target.store.get(uid, 'settings', 'account-check').get('can_message'):
                    monitor.connection_restored(target)
                with service.login_lock: service.login.update(status='confirmed', account_id=uid)
            except TimeoutError:
                with service.login_lock: service.login['status'] = 'expired'
            except Exception:
                with service.login_lock: service.login.update(status='failed', error='扫码未完成，请重新获取二维码')
            finally:
                with service.login_lock: service.login.pop('image', None)
        threading.Thread(target=run, daemon=True).start()
        return {'id': qr_id, 'status': 'starting'}

    @app.get('/api/account/qr/<qr_id>.png')
    def qr_image(qr_id):
        if service.login.get('id') != qr_id or not service.login.get('image'): raise LookupError()
        return send_file(io.BytesIO(service.login['image']), mimetype='image/png')

    return app
