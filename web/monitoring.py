"""Durable polling, proposals and receipts. A platform write always uses Service.job."""
import copy
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from .channel_client import ChannelClient, ChannelError
from .platform import PlatformError

TERMINAL = {'sent', 'failed', 'unknown', 'ignored', 'handoff', 'no_reply', 'stale'}
CONTROL_INSTRUCTIONS = {
    'request_approval': '请求批准：生成最终回复或决定不回复，每条正文由账号主人批准后发送。普通聊天不必要求人工接管。',
    'auto_approve': '帮我批准：自主处理普通聊天并给出可直接发送的最终正文；特殊事项返回 handoff，requires_human=true。',
    'full_access': '完全放开：自主决定 reply 或 no_reply，不返回 handoff，不请求人工批准。缺少事实时自然询问或不回复，不编造；requires_human=false。',
}

def policy(config):
    return {'route_id': config['route_id'], 'control_mode': config['control_mode']}


PURPOSES = {'dm': ('reply_draft', 'dm', 'dm.received'),
            'comment': ('reply_draft', 'comment', 'comment.received'),
            'content': ('content_analysis', 'content', 'content.analysis_requested')}


def available(route, purpose, account_id=None):
    category, surface, event = PURPOSES[purpose]
    accounts = route.get('account_ids', ['*'])
    if account_id is not None and '*' not in accounts and str(account_id) not in accounts:
        return False
    return bool(route.get('agent')) and all('*' in route.get(key, ['*']) or value in route.get(key, [])
        for key, value in [('categories', category), ('surfaces', surface), ('event_types', event)]) and 'text' in route.get('kinds', [])


def agent_choices(catalog, purpose, account_id):
    """The live authorized Multica directory is selectable for every purpose."""
    agents = catalog.get('agents')
    if agents is None:
        if not catalog.get('demo'): raise ChannelError('Agent 实时目录不可用，请刷新后重试')
        agents = [r.get('agent') for r in catalog.get('routes', []) if r.get('agent')]
    choices = {}
    for agent in agents:
        if not agent.get('id'): continue
        key = agent['id']
        choices[key] = {'id': key, 'name': agent.get('name') or key,
            'route_ids': [r['id'] for r in catalog.get('routes', []) if
                (r.get('agent') or {}).get('id') == key and available(r, purpose, account_id)
                and (catalog.get('demo') or str(account_id) in r.get('account_ids', []))]}
    return list(choices.values())


def bound_route(catalog, purpose, account_id, route_id):
    return next((r for r in catalog.get('routes', []) if r['id'] == route_id
        and available(r, purpose, account_id)
        and (catalog.get('demo') or str(account_id) in r.get('account_ids', []))), None)


def resolve_agent(catalog, purpose, account_id, agent_id, preferred='', client=None):
    if not agent_id: return ''
    choice = next((a for a in agent_choices(catalog, purpose, account_id) if a['id'] == agent_id), None)
    if not choice: raise ValueError('请选择实时目录中可用的 Agent')
    if choice['route_ids']:
        return preferred if preferred in choice['route_ids'] else choice['route_ids'][0]
    if client is None: raise ValueError('Agent 尚未绑定当前账号，请刷新后重试')
    result = client.bind(agent_id, account_id, purpose)
    route = {**result['route'], 'agent': result['agent']}
    if not bound_route({'routes': [route], 'demo': catalog.get('demo', False)}, purpose, account_id, route['id']):
        raise ValueError('Agent 绑定未通过当前账号和用途校验')
    catalog.setdefault('routes', []).append(route)
    return route['id']


def agent_snapshot(catalog, route_id):
    route = next((r for r in catalog.get('routes', []) if r['id'] == route_id), None)
    return copy.deepcopy(route.get('agent') or {}) if route else {}


def record_agent(row, config):
    snapshot = copy.deepcopy(config.get('agent') or {})
    row.setdefault('agent', snapshot)  # Original reception identity never changes.
    row.setdefault('agent_history', []).append({'agent': snapshot, 'route_id': config['route_id'],
        'analysis_version': row.get('analysis_version', 0)})
    row['execution_agent'] = snapshot


class DemoChannel:
    def catalog(self):
        return {'connected': True, 'demo': True, 'multica_url': 'https://d.58nft.net:9447',
            'routes': [{'id': 'demo-assistant', 'name': '演示经营助手', 'categories': ['*'],
                'surfaces': ['*'], 'event_types': ['*'], 'kinds': ['text'],
                'agent': {'id': 'demo-agent', 'name': '演示助手（离线）'}}]}

    def send(self, account_id, message_id, text='', **kwargs):
        reply = '你好，商品还在。你想了解商品状态还是配送方式？'
        if kwargs.get('surface') == 'content': reply = '个人闲置，使用爱惜，实物状态良好。欢迎咨询，支持同城自提。'
        return {'id': message_id, 'state': 'completed', 'result': {'output': {
            'text': reply, 'structured': {'decision': 'reply', 'reply_text': reply, 'requires_human': False}}}}

    def status(self, event_id, account_id):
        return self.send(account_id, event_id)


class Monitoring:
    def __init__(self, accounts, config_path):
        self.accounts = accounts
        self.store = accounts.store
        self.config_path = config_path
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='xianyu-monitor')
        self.inflight = {}
        self.thread = None
        self.guard = threading.RLock()

    def client(self, service):
        return DemoChannel() if getattr(service.platform, 'demo', False) else ChannelClient(self.config_path)

    def models(self, service):
        try:
            catalog = self.client(service).catalog()
            return {**catalog, 'agent_choices': {p: agent_choices(catalog, p, service.uid()) for p in PURPOSES}, 'choices': {p: [r for r in catalog['routes'] if available(r, p, service.uid())] for p in PURPOSES}}
        except ChannelError as exc:
            return {'connected': False, 'error': str(exc), 'routes': [], 'choices': {p: [] for p in PURPOSES},
                    'agent_choices': {p: [] for p in PURPOSES}, 'multica_url': 'https://d.58nft.net:9447'}

    def default_agent(self, service, catalog):
        identity = service.platform.identity() or {}
        name = str(identity.get('name') or '').strip()
        if not name: return ''
        expected = '闲鱼-' + name
        matches = [a for a in agent_choices(catalog, 'dm', service.uid()) if a['name'] == expected]
        return matches[0]['id'] if len(matches) == 1 else ''

    def model_bindings(self, service, catalog=None):
        try: bindings = self.store.get(service.uid(), 'settings', 'model-bindings')
        except LookupError: bindings = {}
        catalog = catalog if catalog is not None else self.client(service).catalog()
        default = self.default_agent(service, catalog)
        if not default: return bindings
        changed = False
        for purpose in PURPOSES:
            if bindings.get(purpose) or purpose in bindings.get('explicit_empty', []): continue
            route_id = resolve_agent(catalog, purpose, service.uid(), default, client=self.client(service))
            bindings[purpose] = route_id
            bindings.setdefault('agents', {})[purpose] = agent_snapshot(catalog, route_id)
            changed = True
        return self.store.put(service.uid(), 'settings', bindings, 'model-bindings') if changed else bindings

    def config(self, service, surface, *, resolve_default=True):
        try: config = self.store.get(service.uid(), 'monitor', surface)
        except LookupError:
            config = {'id': surface, 'enabled': False, 'automatic': False, 'interval': 30,
                      'route_id': '', 'revision': 0, 'state': 'stopped', 'last_poll': None, 'error': ''}
        # Preserve existing manual/automatic behavior when migrating older accounts.
        config.setdefault('control_mode', 'auto_approve' if config['automatic'] else 'request_approval')
        config.setdefault('auto_reply', True)
        if resolve_default and not config['route_id'] and not config.get('agent_explicit_empty'):
            try:
                catalog = self.client(service).catalog()
                default = self.default_agent(service, catalog)
                if default:
                    config['route_id'] = resolve_agent(catalog, surface, service.uid(), default, client=self.client(service))
                    config['agent'] = agent_snapshot(catalog, config['route_id'])
                    config['agent_id'] = config['agent'].get('id', '')
                    config['revision'] += 1
                    self.store.put(service.uid(), 'monitor', config, surface)
            except ChannelError:
                pass  # Directory errors are surfaced by models; reception stays stoppable.
        return config

    def supported(self, service, surface):
        return surface == 'dm' or bool(getattr(service.platform, 'supports_comments', False))

    def can_reply(self, service, surface):
        return surface == 'dm' or bool(getattr(service.platform, 'supports_comment_replies',
            getattr(service.platform, 'supports_comments', False)))

    def update(self, service, surface, data):
        # Settings must remain stoppable while a platform read is in flight.
        with self.guard:
            old = self.config(service, surface, resolve_default=False)
            for key in ('enabled', 'automatic', 'auto_reply'):
                if key in data and not isinstance(data[key], bool): raise ValueError('监听开关格式无效')
            interval = data.get('interval', old['interval'])
            if type(interval) is not int or not 15 <= interval <= 300: raise ValueError('监听间隔应为 15 到 300 秒')
            enabled = data.get('enabled', old['enabled'])
            mode = data.get('control_mode', old['control_mode'])
            if not isinstance(mode, str) or mode not in CONTROL_INSTRUCTIONS:
                raise ValueError('控制方式应为请求批准、帮我批准或完全放开')
            send_enabled = data.get('auto_reply', old['auto_reply'])
            if 'automatic' in data and 'control_mode' not in data and 'auto_reply' not in data:
                mode = 'auto_approve' if data['automatic'] else 'request_approval'
                send_enabled = True
            automatic = send_enabled and mode != 'request_approval'
            route_id = str(data.get('route_id', old['route_id']) or '')
            catalog = None
            if 'agent_id' in data:
                catalog = self.client(service).catalog()
                route_id = resolve_agent(catalog, surface, service.uid(), str(data['agent_id'] or ''), route_id, client=self.client(service))
            if enabled and not self.supported(service, surface):
                raise ValueError('当前闲鱼适配器尚未提供商品留言读取和回复接口，不能开启留言监听')
            if route_id and (enabled or 'route_id' in data):
                catalog = catalog or self.client(service).catalog()
                if not bound_route(catalog, surface, service.uid(), route_id):
                    raise ValueError('请选择当前账号可用的 Agent')
            if enabled and automatic and not route_id: raise ValueError('开启自动回复前请选择 Agent')
            if automatic and not self.can_reply(service, surface):
                raise ValueError('当前可读取商品留言和生成草稿，回复请在闲鱼 App 发送')
            value = {**old, 'enabled': enabled, 'automatic': automatic, 'route_id': route_id,
                'interval': interval, 'control_mode': mode, 'auto_reply': send_enabled, 'revision': old['revision'] + 1,
                'state': 'waiting' if enabled else 'stopped', 'error': '', 'blocked_auth': False}
            value['agent'] = agent_snapshot(catalog, route_id) if catalog is not None else (old.get('agent') or {})
            value['agent_id'] = value['agent'].get('id', '')
            if {'agent_id', 'route_id'} & set(data): value['agent_explicit_empty'] = not bool(route_id)
            route_only = bool({'route_id', 'agent_id'} & set(data)) and set(data) <= {'route_id', 'agent_id', 'request_id', 'expected_account_id'}
            if route_only:
                for key in ('blocked_auth', 'state', 'error', 'enabled', 'automatic', 'auto_reply', 'control_mode'):
                    if key in old: value[key] = old[key]
            if enabled and not old['enabled']:
                # First activation establishes a baseline. Resume keeps the original watermark.
                value.setdefault('since', time.time() - (86400 if surface == 'comment' else 0))
            self.store.put(service.uid(), 'monitor', value, surface)
            return value

    def view(self, service, surface):
        # Reading local inbox state must not wait for the remote Agent catalog.
        config = self.config(service, surface, resolve_default=False)
        rows = self.store.list(service.uid(), 'inbox-' + surface)
        # Local receipt reconciliation continues even while reception is stopped.
        for row in rows:
            if row['status'] == 'sending': self.process(service, surface, row, config)
        rows.sort(key=lambda r: r.get('created', 0), reverse=True)
        return {'config': config, 'supported': self.supported(service, surface),
                'can_reply': self.can_reply(service, surface), 'items': rows[:200],
                'total': len(rows), 'stats': {key: sum(r['status'] in statuses for r in rows) for key, statuses in {
                    'received': {r['status'] for r in rows}, 'review': {'review'}, 'sent': {'sent'},
                    'attention': {'failed', 'unknown', 'handoff'}}.items()}}

    def start(self):
        if self.thread: return
        self.thread = threading.Thread(target=self.run, daemon=True, name='xianyu-listeners')
        self.thread.start()

    def connection_restored(self, service):
        """Only an explicit successful login/check releases an authentication block."""
        with self.guard:
            for surface in ('dm', 'comment'):
                config = self.config(service, surface)
                if config.get('blocked_auth'):
                    config.update(blocked_auth=False, error='',
                        state='waiting' if config['enabled'] else 'stopped', last_poll=None)
                    self.store.put(service.uid(), 'monitor', config, surface)

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=3)
        self.pool.shutdown(wait=True)

    def run(self):
        while not self.stop_event.is_set():
            with self.accounts.lock: services = list(self.accounts.services.values())
            for service in services:
                for surface in ('dm', 'comment'):
                    config = self.config(service, surface)
                    key = (service.uid(), surface)
                    if not config['enabled'] or config.get('blocked_auth'): continue
                    with self.guard:
                        previous = self.inflight.get(key)
                        if previous and not previous.done(): continue
                        if time.time() - (config.get('last_poll') or 0) < config['interval']: continue
                        self.inflight[key] = self.pool.submit(self.tick, service, surface)
            self.stop_event.wait(1)

    def tick(self, service, surface):
        with service.lock:
            config = self.config(service, surface)
            if not config['enabled'] or config.get('blocked_auth'): return
            revision, uid = config['revision'], service.uid()
            try:
                if not self.supported(service, surface): raise ValueError('当前平台不支持商品留言监听')
                if surface == 'dm':
                    result = service.platform.socket('sync')
                    current = self.config(service, surface)
                    if not current['enabled'] or current['revision'] != revision: return
                    conversations = result['conversations']
                    found = {c['id'] for c in conversations}
                    # Include cached sessions: sync push alone may omit quiet conversations.
                    known = self.store.list(uid, 'conversation')
                    offset = config.get('scan_offset', 0)
                    rotated = known[offset:] + known[:offset]
                    for conv in rotated[:20]:
                        if conv['id'] not in found:
                            conversations.append({'id': conv['id'], **service.platform.socket('history', cid=conv['id'])})
                            current = self.config(service, surface)
                            if not current['enabled'] or current['revision'] != revision: return
                    for conv in conversations:
                        service.save_history(uid, conv['id'], conv)
                        for message in conv['messages']:
                            self.ingest(service, surface, message, conv['id'], config)
                    config['scan_offset'] = (offset + 20) % max(len(known), 1)
                else:
                    products = self.store.list(uid, 'product')
                    item_ids = [p['id'] for p in products if p.get('seller_id') == uid]
                    offset = config.get('scan_offset', 0)
                    item_ids = item_ids[offset:] + item_ids[:offset]
                    for message in service.platform.comments(item_ids[:10]):
                        self.ingest(service, surface, message, message['item_id'], config)
                    config['scan_offset'] = (offset + 10) % max(len(item_ids), 1)
                for row in self.store.list(uid, 'inbox-' + surface):
                    self.process(service, surface, row, config)
                fresh = self.config(service, surface)
                if fresh['revision'] == revision:
                    fresh.update(state='listening', last_poll=time.time(), error='')
                    fresh['scan_offset'] = config['scan_offset']
                    previously=config.get('state')
                    self.store.put(uid, 'monitor', fresh, surface)
                    if previously!='listening':
                        from .account_maintenance import Bridge
                        Bridge(self.store.directory).notify(uid,uid,'connected',str(time.time_ns()),'listener.'+surface)
                    if surface == 'dm':
                        self.store.put(uid, 'settings', {'checked_at': time.time(), 'can_message': True}, 'account-check')
            except Exception as exc:
                fresh = self.config(service, surface)
                if fresh['revision'] == revision:
                    detail = str(exc) if isinstance(exc, (ValueError, PlatformError)) else '读取未完成，请检查账号连接或网络后重试'
                    if 'SESSION_EXPIRED' in detail:
                        detail = '闲鱼登录已失效，请到账号矩阵重新扫码登录；恢复后监听会自动连接'
                        fresh['blocked_auth'] = True
                        self.store.put(uid, 'settings', {'checked_at': time.time(), 'can_message': False, 'error': detail}, 'account-check')
                    elif 'USER_VALIDATE' in detail or 'RGV587_ERROR' in detail:
                        detail = '闲鱼要求账号安全验证，请在闲鱼 App 完成验证后，到账号矩阵检查连接；验证前已暂停轮询'
                        fresh['blocked_auth'] = True
                        self.store.put(uid, 'settings', {'checked_at': time.time(), 'can_message': False,
                            'needs_verification': True, 'error': detail}, 'account-check')
                    from .account_maintenance import Bridge
                    import requests
                    from websockets.exceptions import ConnectionClosed
                    order_state='expired' if 'SESSION_EXPIRED' in str(exc) else 'risk' if 'USER_VALIDATE' in str(exc) or 'RGV587_ERROR' in str(exc) else None
                    if order_state and config.get('state')!='error':
                        Bridge(self.store.directory).notify(uid,uid,order_state,str(config.get('last_poll') or config['revision']),'listener.'+surface)
                    fresh.update(state='error', last_poll=time.time(), error=detail)
                    self.store.put(uid, 'monitor', fresh, surface)

    def ingest(self, service, surface, message, target, config):
        if surface == 'comment' and message.get('outgoing') and message.get('reply_to_id'):
            self.store.put(service.uid(), 'comment-reply', message)
        if message.get('outgoing') or message.get('kind') == 'system': return
        stamp = float(message.get('timestamp') or 0)
        if stamp < config.get('since', time.time()): return
        # Stable fallback excludes volatile fetch timestamps only when a platform ID exists.
        mid = message.get('id') or hashlib.sha256(json.dumps([target, message], sort_keys=True).encode()).hexdigest()
        key = hashlib.sha256((str(target) + ':' + str(mid)).encode()).hexdigest()[:32]
        try:
            existing = self.store.get(service.uid(), 'inbox-' + surface, key)
            if surface == 'comment' and existing['message'] != message:
                existing['message'] = message
                self.store.put(service.uid(), 'inbox-' + surface, existing)
            return
        except LookupError: pass
        row = {'id': key, 'message_id': mid,
            'target': target, 'message': message, 'text': message.get('text', ''),
            'created': stamp, 'status': 'queued' if config['route_id'] else 'received',
            'route_id': config['route_id'], 'policy': policy(config), 'revision': config['revision'], 'reply': '', 'error': ''}
        record_agent(row, config)
        self.store.put(service.uid(), 'inbox-' + surface, row)

    def is_current(self, service, surface, row):
        if surface == 'comment':
            return not any(m.get('item_id') == row['target'] and m.get('reply_to_id') == row['message_id']
                and float(m.get('timestamp') or 0) >= row['created']
                for m in self.store.list(service.uid(), 'comment-reply'))
        messages = [m for m in self.store.list(service.uid(), 'message') if m.get('cid') == row['target']]
        return not any(float(m.get('timestamp') or 0) > row['created'] and
            (m.get('outgoing') or m.get('id') != row['message_id']) for m in messages)

    def process(self, service, surface, row, config):
        uid, kind = service.uid(), 'inbox-' + surface
        def save(): self.store.put(uid, kind, row)
        if row['status'] == 'sending':
            # Approval saves the intent before inserting its job under this same guard.
            # A concurrent refresh must neither see that gap nor overwrite a newer receipt.
            with self.guard:
                current = self.store.get(uid, kind, row['id'])
                row.clear(); row.update(current)
                if row['status'] != 'sending': return
                try: job = service.get_operation('inbox-' + row['id'])
                except LookupError: row.update(status='unknown', error='发送意图已保存但任务未确认，请核对平台'); save(); return
                if job['status'] in ('pending', 'running'): return
                row.update(status={'completed': 'sent', 'unknown': 'unknown', 'failed': 'failed'}[job['status']],
                    error=job.get('error') or '', receipt=job.get('result')); save(); return
        if row['status'] not in ('queued', 'generating', 'review'): return
        fresh = self.config(service, surface)
        if not fresh['enabled'] or fresh['revision'] != config['revision']: return
        if not self.is_current(service, surface, row): row.update(status='stale', error='已有更新的消息或回复'); save(); return
        previous_policy = row.get('policy', {'route_id': row['route_id'], 'control_mode': 'request_approval'})
        if previous_policy != policy(config):
            # A changed policy needs a fresh immutable model request, including existing drafts.
            row.update(status='queued' if config['route_id'] else 'received', reply='', error='',
                       route_id=config['route_id'], policy=policy(config), revision=config['revision'],
                       analysis_version=row.get('analysis_version', 0) + 1)
            record_agent(row, config)
            for key in ('request', 'channel_id', 'requires_human'): row.pop(key, None)
            save()
            if row['status'] == 'received': return
        elif row['revision'] != config['revision']:
            row['revision'] = config['revision']; save()
        if row['status'] == 'review':
            if config['automatic'] and not row.get('requires_human', True) and row.get('revision') == config['revision']: self.approve(service, surface, row['id'], automatic=True)
            return
        client = self.client(service)
        if row['status'] == 'queued':
            context = {'messages': [], 'control_mode': config['control_mode'], 'instruction': CONTROL_INSTRUCTIONS[config['control_mode']] + ' 返回 JSON：decision 为 reply、handoff 或 no_reply，reply_text 为最终回复正文，requires_human 为布尔值；只有 handoff 才为 true。不要虚构价格、库存或承诺。'}
            if surface == 'dm':
                conv = self.store.get(uid, 'conversation', row['target'])
                context['conversation'] = {k: conv.get(k) for k in ('id', 'name', 'note', 'item_id')}
                messages = [m for m in self.store.list(uid, 'message') if m.get('cid') == row['target']]
                messages.sort(key=lambda m: float(m.get('timestamp') or 0))
                context['messages'] = [{k: m.get(k) for k in ('id', 'text', 'kind', 'outgoing', 'timestamp')} for m in messages[-20:]]
                if conv.get('item_id'):
                    try: context['item'] = self.store.get(uid, 'product', conv['item_id'])
                    except LookupError: pass
            else:
                try: context['item'] = self.store.get(uid, 'product', row['target'])
                except LookupError: pass
            # Save before submission; retry with the same immutable key after a lost response.
            if 'request' not in row:
                catalog = client.catalog()
                if not bound_route(catalog, surface, uid, row['route_id']):
                    row.update(status='failed', error='绑定 Agent 已不可用，请重新选择并分析'); save(); return
                snapshot = agent_snapshot(catalog, row['route_id'])
                expected = (row.get('execution_agent') or row.get('agent') or {}).get('id')
                if expected and snapshot.get('id') != expected:
                    row.update(status='failed', error='路由绑定的 Agent 已变化，请重新分析'); save(); return
                row.setdefault('agent', snapshot)
                row['execution_agent'] = snapshot
                row['request'] = {'text': row['text'] or '[图片消息，需要人工查看]', 'category': 'reply_draft',
                    'conversation_id': row['target'], 'surface': surface, 'event_type': surface + '.received',
                    'context': context, 'route_id': row['route_id']}
                save()
            event = client.send(uid, 'xianyu-inbox-' + row['id'] + ('-v' + str(row['analysis_version']) if row.get('analysis_version') else ''), **row['request'])
            row.update(status='generating', channel_id=event['id']); save()
        else: event = client.status(row['channel_id'], uid)
        if event.get('result'):
            output = event['result'].get('output') or {}
            structured = output.get('structured') or {}
            if not structured:
                try: structured = json.loads(output.get('text', ''))
                except (TypeError, ValueError): structured = {}
            if not isinstance(structured, dict): structured = {}
            decision = structured.get('decision')
            reply = structured.get('reply_text') or structured.get('reply') or ''
            human = decision == 'handoff' or structured.get('requires_human') is True
            if config['control_mode'] == 'full_access' and (human or decision not in ('reply', 'no_reply') or structured.get('requires_human') is not False):
                row.update(status='failed', error='模型未遵守完全放开模式，返回人工确认或缺少明确决策；未发送，请重新分析'); save(); return
            if decision == 'no_reply':
                row.update(status='no_reply', error=str(structured.get('reason') or '模型判断无需回复')); save(); return
            if decision == 'handoff' and (not isinstance(reply, str) or not reply.strip()):
                row.update(status='handoff', error=str(structured.get('reason') or '需要账号主人处理')); save(); return
            if not isinstance(reply, str) or not reply.strip() or len(reply) > 3000:
                row.update(status='handoff', error='助手未返回可验证的文字回复，请人工处理'); save(); return
            row.update(status='review', reply=reply.strip(), requires_human=human or structured.get('requires_human') is not False); save()
            if config['automatic'] and not row['requires_human'] and row.get('revision') == config['revision']: self.approve(service, surface, row['id'], automatic=True)
        elif event.get('state') in ('failed', 'blocked_no_route', 'blocked_reply', 'failed_preflight', 'uncertain', 'attention'):
            row.update(status='failed', error=event.get('error') or 'Multica 任务未完成'); save()

    def approve(self, service, surface, item_id, automatic=False):
        with service.lock, self.guard:
            row = self.store.get(service.uid(), 'inbox-' + surface, item_id)
            if row['status'] == 'sending':
                return service.get_operation('inbox-' + row['id'])
            if row['status'] != 'review': raise ValueError('这条消息没有待发送的回复')
            if not self.can_reply(service, surface):
                raise ValueError('留言回复接口尚未接通，请复制草稿到闲鱼 App 回复')
            config = self.config(service, surface)
            if automatic and (not config['enabled'] or not config['automatic'] or config['control_mode'] == 'request_approval' or
                    row.get('revision') != config['revision'] or row.get('requires_human', True)): return None
            if not config['enabled']: raise ValueError('监听已停止，请先恢复监听再发送')
            if row.get('policy', {'route_id': row['route_id'], 'control_mode': 'request_approval'}) != policy(config):
                raise ValueError('回复配置已变化，请重新分析后批准当前正文')
            if config.get('blocked_auth'): raise ValueError('账号需要登录或安全验证，请检查连接后再发送')
            catalog = self.client(service).catalog()
            route = bound_route(catalog, surface, service.uid(), row['route_id'])
            expected = (row.get('execution_agent') or row.get('agent') or {}).get('id')
            if not route or (expected and route.get('agent', {}).get('id') != expected):
                raise ValueError('绑定 Agent 已变化或不可用，请重新选择并分析')
            if not self.is_current(service, surface, row): raise ValueError('会话已有更新，请重新查看后回复')
            row['status'] = 'sending'
            self.store.put(service.uid(), 'inbox-' + surface, row)
            try:
                if surface == 'dm':
                    job = service.send(row['target'], {'text': row['reply'], 'request_id': 'inbox-' + row['id'],
                        'expected_account_id': service.uid()})
                else:
                    job = service.job('comment', '回复商品留言', 'inbox-' + row['id'],
                        lambda uid: service.platform.reply_comment(row['message'], row['reply']))
                return job
            except (ValueError, LookupError):
                row['status'] = 'review'; self.store.put(service.uid(), 'inbox-' + surface, row); raise

    def reanalyze(self, service, surface, item_id):
        with service.lock, self.guard:
            row = self.store.get(service.uid(), 'inbox-' + surface, item_id)
            if row['status'] not in ('received', 'review', 'handoff', 'failed'):
                raise ValueError('此记录不能重新分析；已发送或结果待核查的回复不会重发')
            try:
                service.get_operation('inbox-' + row['id'])
            except LookupError: pass
            else: raise ValueError('此记录已有发送任务，请核对结果，不能重新分析发送')
            config = self.config(service, surface)
            if not config['enabled'] or not config['route_id']: raise ValueError('请先选择回复路由并开始监听')
            if not self.is_current(service, surface, row): raise ValueError('会话已有更新消息，请处理最新记录')
            row.update(status='queued', reply='', error='', route_id=config['route_id'], policy=policy(config),
                       revision=config['revision'], analysis_version=row.get('analysis_version', 0) + 1)
            record_agent(row, config)
            for key in ('request', 'channel_id', 'requires_human'): row.pop(key, None)
            return self.store.put(service.uid(), 'inbox-' + surface, row)

    def dismiss(self, service, surface, item_id):
        with service.lock:
            row = self.store.get(service.uid(), 'inbox-' + surface, item_id)
            if row['status'] in TERMINAL or row['status'] == 'sending': raise ValueError('此记录已经处理或正在发送')
            row['status'] = 'ignored'
            return self.store.put(service.uid(), 'inbox-' + surface, row)
