from contextlib import contextmanager
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from PIL import Image, UnidentifiedImageError
from .platform import PlatformError, item_projection


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'workspace.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    uid TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    data TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(uid,kind,id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, uid TEXT NOT NULL, request_id TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, result TEXT,
                    error TEXT, UNIQUE(uid,request_id));
            ''')
            db.execute("UPDATE jobs SET status='unknown',error='服务重启中断，请回查平台结果，不会自动重试' WHERE status IN ('pending','running')")
            for row in db.execute("SELECT uid,id,data FROM documents WHERE kind='draft'").fetchall():
                draft = json.loads(row['data'])
                if draft.get('status') == 'publishing':
                    draft['status'] = 'unknown'
                    db.execute("UPDATE documents SET data=? WHERE uid=? AND kind='draft' AND id=?",
                        (json.dumps(draft, ensure_ascii=False), row['uid'], row['id']))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, uid, kind, value, item_id=None):
        item_id = str(item_id or value.get('id') or uuid.uuid4().hex)
        value = {**value, 'id': item_id}
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
                       (uid, kind, item_id, json.dumps(value, ensure_ascii=False), time.time()))
        return value

    def get(self, uid, kind, item_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM documents WHERE uid=? AND kind=? AND id=?',
                             (uid, kind, str(item_id))).fetchone()
        if not row: raise LookupError()
        return json.loads(row['data'])

    def list(self, uid, kind):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM documents WHERE uid=? AND kind=? ORDER BY updated DESC',
                              (uid, kind)).fetchall()
        return [json.loads(row['data']) for row in rows]

    def delete(self, uid, kind, item_id):
        with self.connect() as db:
            db.execute('DELETE FROM documents WHERE uid=? AND kind=? AND id=?', (uid, kind, item_id))

    def jobs(self, uid, limit=40):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM jobs WHERE uid=? ORDER BY created DESC LIMIT ?', (uid, limit)).fetchall()
        return [self.job_projection(row) for row in rows]

    @staticmethod
    def job_projection(row):
        value = dict(row)
        value.pop('uid', None)
        value.pop('request_id', None)
        value['result'] = json.loads(value['result']) if value.get('result') else None
        return value


class Service:
    def __init__(self, directory, platform, store=None):
        self.store = store or Store(directory)
        self.platform = platform
        self.lock = threading.RLock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='xianyu-web')
        self.login = {'status': 'idle'}
        self.login_lock = threading.Lock()
        self.media_dir = self.store.directory / 'media'
        self.media_dir.mkdir(exist_ok=True, mode=0o700)
        self.bootstrap()

    def uid(self):
        identity = self.platform.identity()
        if not identity: raise PlatformError('请先扫码连接闲鱼账号')
        return identity['id']

    def bootstrap(self):
        identity = self.platform.identity()
        if not identity: return
        uid = identity['id']
        source = self.platform.session_path.parent if hasattr(self.platform, 'session_path') else self.store.directory
        path = source / 'own-item.json'
        if path.exists():
            try:
                item = item_projection(json.loads(path.read_text()))
                if item['seller_id'] == uid:
                    self.store.get(uid, 'product', item['id'])
            except LookupError:
                self.store.put(uid, 'product', item)
                self.record_snapshot(uid, item)
            except Exception: pass
        path = source / 'test-publish-verification.json'
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                self.store.get(uid, 'product', str(raw['item_id']))
            except LookupError:
                self.store.put(uid, 'product', {'id': str(raw['item_id']), 'title': raw['title'],
                    'price': raw['price'], 'status': raw['status'], 'images': [], 'cover': '',
                    'seller_id': uid, 'description': raw['title'], 'views': None, 'wants': None,
                    'collects': None, 'updated_at': None, 'test': True})
            except Exception: pass
        path = source / 'selected-test-send.json'
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                self.store.get(uid, 'conversation', raw['cid'])
            except LookupError:
                self.store.put(uid, 'conversation', {'id': raw['cid'], 'peer_id': raw['toid'],
                    'name': '用户 ' + raw['toid'][-4:], 'preview': '', 'note': '', 'updated_at': None})
            except Exception: pass
        if not self.store.list(uid, 'template'):
            for title, text in [('欢迎咨询', '你好，商品还在，欢迎咨询～'), ('商品详情', '你好，商品详情和实拍图都在页面里，有其他问题可以直接问我。'),
                                ('稍后回复', '你好，消息已收到，稍后给你回复。')]:
                self.store.put(uid, 'template', {'title': title, 'text': text})

    def record_snapshot(self, uid, product):
        self.store.put(uid, 'snapshot', {'product_id': product['id'], 'at': time.time(),
            'views': product.get('views'), 'wants': product.get('wants'), 'collects': product.get('collects')})

    def save_product(self, uid, product):
        try:
            previous = self.store.get(uid, 'product', product['id'])
            for k in ('note', 'tags', 'test'): product[k] = previous.get(k)
        except LookupError: pass
        self.store.put(uid, 'product', product)
        self.record_snapshot(uid, product)
        return product

    def job(self, kind, title, request_id, work, execution_guard=None):
        uid = self.uid()
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('操作标识无效，请刷新页面后再试')
        job_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE uid=? AND request_id=?', (uid, request_id)).fetchone()
            if existing: return self.store.job_projection(existing)
            now = time.time()
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (job_id, uid, request_id, kind, title, 'pending', now, now, None, None))
        def run():
            status, result, error = 'completed', None, None
            with self.lock:
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job_id))
                try:
                    if self.uid() != uid: raise PlatformError('账号已切换，请重新发起操作')
                    if execution_guard: execution_guard()
                    result = work(uid)
                except (PlatformError, ValueError, LookupError) as exc:
                    status = 'failed'
                    error = str(exc) if isinstance(exc, (PlatformError, ValueError)) else '记录不存在'
                    if isinstance(exc,PlatformError) and kind in ('send','sync','history','comment','publish'):
                        state='expired' if 'SESSION_EXPIRED' in str(exc) or 'SESSION_EXOIRED' in str(exc) else 'risk' if 'USER_VALIDATE' in str(exc) or 'RGV587_ERROR' in str(exc) else None
                        if state:
                            from .account_maintenance import Bridge
                            component='listener.comment' if kind=='comment' else 'publisher' if kind=='publish' else 'listener.dm'
                            Bridge(self.store.directory).notify(uid,uid,state,request_id,component)
                except Exception:
                    status = 'unknown' if kind in ('send', 'publish', 'create', 'comment') else 'failed'
                    error = ('请求中断，结果待核查，请同步平台记录；不会自动重试' if status == 'unknown'
                             else '操作未完成，请检查账号或网络后再试')
                with self.store.connect() as db:
                    db.execute('UPDATE jobs SET status=?,updated=?,result=?,error=? WHERE id=?',
                        (status, time.time(), json.dumps(result, ensure_ascii=False) if result is not None else None, error, job_id))
        self.executor.submit(run)
        return {'id': job_id, 'kind': kind, 'title': title, 'status': 'pending'}

    def get_job(self, job_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND uid=?', (job_id, self.uid())).fetchone()
        if not row: raise LookupError()
        return self.store.job_projection(row)

    def get_operation(self, request_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE request_id=? AND uid=?', (request_id, self.uid())).fetchone()
        if not row: raise LookupError()
        return self.store.job_projection(row)

    def import_item(self, value, request_id):
        def work(uid):
            product = self.platform.item(value)
            return self.save_product(uid, product)
        return self.job('import', '导入商品', request_id, work)

    def save_history(self, uid, cid, page):
        try: conv = self.store.get(uid, 'conversation', cid)
        except LookupError: conv = {'id': cid, 'note': ''}
        if page.get('peer_id'): conv['peer_id'] = page['peer_id']
        if page.get('item_id') and not conv.get('item_id'): conv['item_id'] = page['item_id']
        if page.get('name') and (not conv.get('name') or conv['name'].startswith('用户 ')):
            conv['name'] = page['name']
        conv.setdefault('name', '用户 ' + str(conv.get('peer_id', cid))[-4:])
        conv['updated_at'] = time.time()
        existing_messages = [m for m in self.store.list(uid, 'message') if m['cid'] == cid]
        for msg in page['messages']:
            key = msg['id'] or hashlib.sha256(json.dumps([cid, msg], sort_keys=True).encode()).hexdigest()[:28]
            # Replace a locally acknowledged outgoing message with its platform echo.
            if msg.get('outgoing'):
                for local in existing_messages:
                    same = (local.get('local_sent') and local.get('kind') == msg.get('kind') and
                            local.get('text') == msg.get('text') and local.get('images') == msg.get('images') and
                            abs(float(local.get('timestamp') or 0) - float(msg.get('timestamp') or 0)) < 120)
                    if same:
                        key = local['id']
                        msg = {**local, **msg, 'local_sent': True, 'platform_id': msg.get('id')}
                        break
            self.store.put(uid, 'message', {**msg, 'cid': cid}, key)
        all_messages = [m for m in self.store.list(uid, 'message') if m['cid'] == cid]
        if all_messages:
            latest = max(all_messages, key=lambda m: float(m.get('timestamp') or 0))
            conv['preview'] = '[图片]' if latest['kind'] == 'image' else latest['text']
            conv['last_time'] = latest.get('timestamp')
        conv['cursor'] = page.get('cursor')
        conv['has_more'] = page.get('has_more', False)
        self.store.put(uid, 'conversation', conv)
        return {'count': len(page['messages']), 'has_more': page.get('has_more', False), 'cursor': page.get('cursor')}

    def sync(self, request_id, execution_guard=None):
        def work(uid):
            result = self.platform.socket('sync')
            for conv in result['conversations']: self.save_history(uid, conv['id'], conv)
            return {'count': len(result['conversations']), 'scope': result['scope']}
        return self.job('sync', '同步最近会话', request_id, work, execution_guard)

    def history(self, cid, older, request_id, execution_guard=None):
        uid = self.uid()
        conv = self.store.get(uid, 'conversation', cid)
        cursor = conv.get('cursor') if older else None
        if older and (not conv.get('has_more') or cursor is None): raise ValueError('没有更多历史消息')
        def work(uid):
            page = self.platform.socket('history', cid=cid, cursor=cursor)
            if older and page.get('cursor') == cursor and page.get('has_more'):
                raise PlatformError('历史游标没有前进，已停止重复读取')
            return self.save_history(uid, cid, page)
        return self.job('history', '读取聊天历史', request_id, work, execution_guard)

    def send(self, cid, data, execution_guard=None):
        uid = self.uid()
        if data.get('expected_account_id') is not None and str(data['expected_account_id']) != str(uid):
            raise ValueError('执行账号与当前连接账号不一致，尚未发送')
        conv = self.store.get(uid, 'conversation', cid)
        if not conv.get('peer_id'): raise ValueError('请先同步聊天历史，确认会话收件人')
        text = str(data.get('text', '')).strip()
        media_id = data.get('media_id')
        if not text and not media_id: raise ValueError('请输入消息或选择图片')
        if text and media_id: raise ValueError('请分别发送文字和图片')
        if len(text) > 3000: raise ValueError('消息请控制在 3000 字以内')
        media = self.store.get(uid, 'media', media_id) if media_id else None
        def work(uid):
            if data.get('expected_account_id') is not None and str(data['expected_account_id']) != str(uid):
                raise ValueError('执行账号在排队期间发生变化，尚未发送')
            image = None
            if media:
                uploaded = self.platform.upload(str(self.media_dir / media['filename']))
                image = {'url': uploaded['url'], 'width': uploaded['width'], 'height': uploaded['height']}
            if execution_guard: execution_guard()
            result = self.platform.socket('send', cid=cid, peer_id=conv['peer_id'], text=text, image=image)
            self.store.put(uid, 'message', {'cid': cid, 'sender_id': uid, 'outgoing': True,
                'kind': 'image' if media else 'text', 'text': text, 'images': [image['url']] if image else [],
                'timestamp': time.time(), 'local_sent': True, 'platform_id': result.get('message_id')})
            conv['preview'] = text or '[图片]'
            conv['updated_at'] = time.time()
            self.store.put(uid, 'conversation', conv)
            return result
        return self.job('send', '发送图片' if media else '发送文字消息', data.get('request_id'), work, execution_guard)

    def media(self, file):
        uid = self.uid()
        from io import BytesIO
        content = file.read(8 * 1024 * 1024 + 1)
        if not content or len(content) > 8 * 1024 * 1024: raise ValueError('图片大小须小于 8 MB')
        try:
            image = Image.open(BytesIO(content))
            image.verify()
            image = Image.open(BytesIO(content))
            if image.format not in ('JPEG', 'PNG', 'WEBP'): raise ValueError('支持 JPG、PNG、WebP 图片')
            if image.width * image.height > 25_000_000: raise ValueError('图片尺寸过大')
            media_id = uuid.uuid4().hex
            filename = media_id + '.png'
            # Decode and re-encode locally; never retain arbitrary uploaded file bytes.
            image.convert('RGB').save(self.media_dir / filename, format='PNG')
            os.chmod(self.media_dir / filename, 0o600)
        except (UnidentifiedImageError, OSError): raise ValueError('无法读取这张图片')
        return self.store.put(uid, 'media', {'id': media_id, 'filename': filename,
             'width': image.width, 'height': image.height, 'url': '/api/media/' + media_id + '?account_id=' + uid})

    def draft(self, data, draft_id=None):
        uid = self.uid()
        text = str(data.get('description', '')).strip()
        if len(text) > 5000: raise ValueError('商品描述请控制在 5000 字以内')
        delivery = data.get('delivery', '无需邮寄')
        if delivery not in ('包邮', '按距离计费', '一口价', '无需邮寄'): raise ValueError('配送方式无效')
        if draft_id:
            old = self.store.get(uid, 'draft', draft_id)
            if old.get('status') in ('publishing', 'published', 'unknown'): raise ValueError('此草稿已提交或结果待核查，不能重复修改发布')
        ids = data.get('media_ids', [])
        if not isinstance(ids, list) or len(ids) > 9: raise ValueError('最多选择 9 张图片')
        for media_id in ids: self.store.get(uid, 'media', media_id)
        value = {k: data.get(k) for k in ('description', 'price', 'original_price', 'postage', 'self_pickup', 'location_index')}
        value.update(description=text, delivery=delivery, media_ids=ids, status='draft', updated_at=time.time())
        return self.store.put(uid, 'draft', value, draft_id)

    def publish(self, draft_id, request_id, execution_guard=None):
        uid = self.uid()
        draft = self.store.get(uid, 'draft', draft_id)
        if draft['status'] != 'draft': raise ValueError('草稿已提交，不能重复发布')
        if not draft['description'] or not draft['media_ids']: raise ValueError('请填写描述并至少上传一张商品图片')
        try:
            for key in ('price', 'original_price', 'postage'):
                value = Decimal(str(draft.get(key) or 0))
                if not value.is_finite() or value < 0 or value > 9999999 or value.as_tuple().exponent < -2: raise ValueError()
            if Decimal(str(draft['price'])) <= 0: raise ValueError()
        except (InvalidOperation, ValueError): raise ValueError('请填写有效金额，售价须大于 0，最多两位小数')
        try: locations = self.store.get(uid, 'settings', 'locations')['items']
        except LookupError: locations = []
        index = draft.get('location_index')
        if index is None and draft['delivery']=='无需邮寄': location=None
        elif isinstance(index,int) and not isinstance(index,bool) and 0<=index<len(locations): location=locations[index]
        else: raise ValueError('请选择发货地点')
        files = [str(self.media_dir / self.store.get(uid, 'media', x)['filename']) for x in draft['media_ids']]
        def work(uid):
            current = self.store.get(uid, 'draft', draft_id)
            if current['status'] != 'draft': raise PlatformError('草稿已经提交')
            current['status'] = 'publishing'; self.store.put(uid, 'draft', current)
            try:
                if execution_guard: execution_guard()
                def progress(stage,started):
                    current.update(publish_stage=stage,publish_request_started=started)
                    self.store.put(uid,'draft',current)
                kwargs={'progress':progress} if getattr(self.platform,'supports_publish_progress',False) else {}
                result = self.platform.publish(current, files, location,**kwargs)
            except PlatformError:
                current['status'] = 'draft'; self.store.put(uid, 'draft', current); raise
            except Exception:
                current['status'] = 'unknown'; self.store.put(uid, 'draft', current); raise
            current.update(status='published', published_id=result['item_id']); self.store.put(uid, 'draft', current)
            # Publication is complete even if the subsequent read-back fails.
            try: self.save_product(uid, self.platform.item(result['item_id']))
            except Exception: result['readback_pending'] = True
            return result
        return self.job('publish', '发布商品', request_id, work, execution_guard)
