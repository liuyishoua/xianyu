"""Independent account sessions and executors; browser selection never moves a worker."""
import hashlib
import threading
import time
from .platform import Platform, PlatformError, save_session, private_json
from .service import Service


class Accounts:
    scope = '__workspace__'

    def __init__(self, primary):
        self.primary = primary
        self.store = primary.store
        self.lock = threading.RLock()
        self.services = {}
        identity = primary.platform.identity()
        if identity:
            self.services[str(identity['id'])] = primary
            if not getattr(primary.platform, 'demo', False):
                self.remember(primary.platform)
        if not getattr(primary.platform, 'demo', False):
            for record in self.store.list(self.scope, 'account'):
                uid = record['id']
                if uid in self.services: continue
                platform = Platform(self.path(uid))
                if platform.identity():
                    self.services[uid] = Service(self.store.directory, platform, store=self.store)

    def path(self, uid):
        key = hashlib.sha256(str(uid).encode()).hexdigest()[:32]
        return self.store.directory / 'accounts' / key / 'session.json'

    def remember(self, platform):
        identity = platform.identity()
        uid = str(identity['id'])
        target = self.path(uid)
        if platform.session_path != target:
            import json
            # A migrated session may have fresher cookies than the legacy bootstrap file.
            if not target.exists(): private_json(target, json.loads(platform.session_path.read_text()))
            platform.session_path = target
        self.store.put(self.scope, 'account', identity, uid)

    def register(self, api, expected=None):
        uid = str(api.session.cookies.get_dict().get('unb') or '')
        if not uid: raise PlatformError('登录未返回账号身份')
        if expected and uid != expected:
            raise ValueError('扫码账号与要恢复的账号不一致，请使用原账号重新扫码')
        with self.lock:
            target = self.services.get(uid)
            if target:
                with target.lock: save_session(self.path(uid), api)
                target.platform.session_path = self.path(uid)
            else:
                save_session(self.path(uid), api)
                target = Service(self.store.directory, Platform(self.path(uid)), store=self.store)
                self.services[uid] = target
            self.remember(target.platform)
            target.bootstrap()
            with target.lock:
                try: health = target.platform.check()
                except Exception:
                    health = {'checked_at': time.time(), 'can_message': False,
                              'error': '登录已保存，但消息连接未确认，请检查连接'}
                self.store.put(uid, 'settings', health, 'account-check')
        return uid

    def resolve(self, uid=None):
        if not uid: return self.primary
        with self.lock:
            if uid not in self.services: raise LookupError()
            return self.services[uid]

    def list(self):
        with self.lock:
            rows = []
            for uid, service in self.services.items():
                identity = service.platform.identity()
                if not identity: continue
                try: checked = self.store.get(uid, 'settings', 'account-check')
                except LookupError: checked = None
                try: label = self.store.get(self.scope, 'account-label', uid)['name']
                except LookupError: label = ''
                rows.append({**identity, 'status': 'error' if checked and not checked.get('can_message') else identity['status'],
                    'label': label, 'check': checked,
                    'products': len(self.store.list(uid, 'product')),
                    'conversations': len(self.store.list(uid, 'conversation'))})
            return rows

    def close(self):
        for service in self.services.values():
            service.executor.shutdown(wait=True)
