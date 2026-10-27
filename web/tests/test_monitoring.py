import io
import json
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from web.app import create_app
from web.demo import DemoPlatform, seed
from web.service import Service
from web.platform import Platform, PlatformError, private_json
from web.accounts import Accounts
from web.monitoring import Monitoring


class RecordingPlatform(DemoPlatform):
    def __init__(self, uid='seller-a'):
        super().__init__(uid, uid)
        self.sends = []
        self.comment_sends = []
        self.timeout = False
    def socket(self, operation, **kwargs):
        if operation == 'send':
            self.sends.append(kwargs)
            if self.timeout: raise TimeoutError()
        return super().socket(operation, **kwargs)
    def reply_comment(self, comment, text):
        self.comment_sends.append((comment, text))
        return super().reply_comment(comment, text)


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = RecordingPlatform()
        self.app = create_app(self.tmp.name, platform=self.platform, auth=False)
        self.s = self.app.extensions['workspace']
        self.accounts = self.app.extensions['accounts']
        self.monitor = self.app.extensions['monitor']
        seed(self.s)
        self.b = Service(self.tmp.name, RecordingPlatform('seller-b'), store=self.s.store)
        seed(self.b)
        self.accounts.services['seller-b'] = self.b
        self.c = self.app.test_client()
        self.headers = {'X-App-Request': '1', 'Origin': 'http://localhost'}
    def tearDown(self):
        self.monitor.close()
        self.accounts.close()
        self.tmp.cleanup()
    def enable(self, surface='dm', automatic=False, route='demo-assistant'):
        return self.monitor.update(self.s, surface, {'enabled': True, 'automatic': automatic, 'route_id': route})
    def incoming(self, mid='new-1', surface='dm', age=0):
        row = {'id': mid, 'kind': 'text', 'sender_id': '800000', 'text': '商品还在吗？',
               'outgoing': False, 'images': [], 'timestamp': time.time() + age}
        if surface == 'dm':
            row['cid'] = '900000'; self.platform.incoming.append(row)
        else:
            row['item_id'] = '1000000001'; self.platform.comment_rows.append(row)
        return row
    def rows(self, surface='dm'):
        return self.monitor.view(self.s, surface)['items']
    def finish(self):
        self.s.executor.shutdown(wait=True)
        return self.rows()[0]
    def account_routes(self):
        base = self.monitor.client(self.s).catalog()['routes'][0]
        return {'connected': True, 'agents': [base['agent']], 'routes': [
            {**base, 'id': 'account-a', 'account_ids': ['seller-a']},
            {**base, 'id': 'account-b', 'account_ids': ['seller-b']},
            {**base, 'id': 'shared', 'account_ids': ['*']},
            {**base, 'id': 'legacy'},
        ]}

    def test_route_choices_are_scoped_to_execution_account(self):
        catalog = self.account_routes()
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            for account, expected in [('seller-a', 'account-a'), ('seller-b', 'account-b')]:
                data = self.c.get('/api/models', headers={'X-Account-ID': account}).json
                for choices in data['choices'].values():
                    self.assertEqual({r['id'] for r in choices}, {expected, 'shared', 'legacy'})

    def test_other_account_route_cannot_be_bound_or_selected(self):
        catalog = self.account_routes()
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            for account, forbidden in [('seller-a', 'account-b'), ('seller-b', 'account-a')]:
                headers = {**self.headers, 'X-Account-ID': account}
                for purpose in ('dm', 'comment', 'content'):
                    response = self.c.put('/api/models', json={purpose: forbidden}, headers=headers)
                    self.assertEqual(response.status_code, 400)
                for surface in ('dm', 'comment'):
                    # Even a stopped monitor must not retain another account's route.
                    response = self.c.put('/api/monitoring/' + surface,
                        json={'route_id': forbidden}, headers=headers)
                    self.assertEqual(response.status_code, 400)
            response = self.c.put('/api/models', json={'dm': 'account-a'}, headers=self.headers)
            self.assertEqual(response.status_code, 200)

    def test_route_only_migration_preserves_monitor_auth_block_and_switches(self):
        catalog = self.account_routes()
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            for surface, enabled in [('dm', True), ('comment', False)]:
                old = self.monitor.config(self.s, surface)
                old.update(enabled=enabled, automatic=True, auto_reply=True,
                    control_mode='full_access', route_id='legacy', blocked_auth=True,
                    state='blocked_auth', error='需要安全验证')
                self.s.store.put(self.s.uid(), 'monitor', old, surface)
                response = self.c.put('/api/monitoring/' + surface,
                    json={'route_id': 'account-a'}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                new = response.json
                self.assertEqual(new['route_id'], 'account-a')
                self.assertEqual(new['revision'], old['revision'] + 1)
                for key in ('enabled', 'automatic', 'auto_reply', 'control_mode',
                            'blocked_auth', 'state', 'error'):
                    self.assertEqual(new[key], old[key])
                self.assertEqual(self.c.post('/api/monitoring/' + surface + '/poll',
                    json={}, headers=self.headers).status_code, 400)

    def test_complete_live_directory_agent_can_bind_without_prebuilt_route(self):
        catalog = self.account_routes()
        agent = {'id': 'new-live-agent', 'name': '新建通用 Agent'}
        catalog['agents'].append(agent)
        route = {**catalog['routes'][0], 'id': 'new-agent-current-account', 'agent': agent}
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog), patch(
                'web.monitoring.DemoChannel.bind', return_value={'route': route, 'agent': agent}, create=True) as bind:
            data = self.c.get('/api/models').json
            self.assertIn(agent['id'], {a['id'] for a in data['agent_choices']['dm']})
            result = self.c.put('/api/monitoring/dm', json={'agent_id': agent['id']}, headers=self.headers)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json['agent_id'], agent['id'])
            self.assertEqual(result.json['route_id'], route['id'])
            bind.assert_called_once_with(agent['id'], 'seller-a', 'dm')

    def test_live_directory_rename_and_failure_are_reflected(self):
        catalog = self.account_routes()
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            self.assertEqual(self.c.get('/api/models').json['agent_choices']['dm'][0]['name'], catalog['agents'][0]['name'])
            catalog['agents'][0] = {**catalog['agents'][0], 'name': '实时改名 Agent'}
            self.assertEqual(self.c.get('/api/models').json['agent_choices']['dm'][0]['name'], '实时改名 Agent')
        from web.channel_client import ChannelError
        with patch('web.monitoring.DemoChannel.catalog', side_effect=ChannelError('实时目录离线')):
            data = self.c.get('/api/models').json
            self.assertFalse(data['connected'])
            self.assertEqual(data['agent_choices']['dm'], [])
            self.assertIn('实时目录离线', data['error'])

    def test_exact_account_name_defaults_without_overriding_saved_choice(self):
        catalog = self.account_routes()
        agent = {'id': 'demo-agent', 'name': '闲鱼-seller-a'}
        catalog['agents'] = [agent]
        for route in catalog['routes']: route['agent'] = agent
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            data = self.c.get('/api/models').json
            self.assertEqual({data['bindings'][p] for p in ('dm','comment','content')}, {'account-a'})
            config = self.monitor.config(self.s, 'dm')
            self.assertEqual(config['agent_id'], 'demo-agent')
            self.assertFalse(config['enabled'])
            self.monitor.update(self.s, 'dm', {'agent_id': ''})
            self.assertEqual(self.monitor.config(self.s, 'dm')['route_id'], '')
            self.c.put('/api/models', json={'agent_bindings': {}}, headers=self.headers)
            self.assertEqual(self.c.get('/api/models').json['bindings']['dm'], '')

    def test_inbox_reads_and_stop_do_not_depend_on_agent_directory(self):
        with patch('web.monitoring.DemoChannel.catalog', side_effect=AssertionError('Inbox must remain local')) as catalog:
            for surface in ('dm', 'comment'):
                response = self.c.get('/api/monitoring/' + surface)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['items'], [])
                config = response.json['config']
                config.update(enabled=True, route_id='previously-validated-route')
                self.s.store.put(self.s.uid(), 'monitor', config, surface)
                stopped = self.c.put('/api/monitoring/' + surface, json={'enabled': False}, headers=self.headers)
                self.assertEqual(stopped.status_code, 200)
                self.assertFalse(stopped.json['enabled'])
                self.assertEqual(stopped.json['route_id'], 'previously-validated-route')
            catalog.assert_not_called()

    def test_account_default_preserves_another_explicit_authorized_agent(self):
        catalog = self.account_routes()
        default = {'id': 'demo-agent', 'name': '闲鱼-seller-a'}
        selected = {'id': 'selected-agent', 'name': '独立已选 Agent'}
        catalog['agents'] = [default, selected]
        for route in catalog['routes']: route['agent'] = default
        catalog['routes'].append({**catalog['routes'][0], 'id': 'selected-route', 'agent': selected})
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            response = self.c.put('/api/models', json={'agent_bindings': {'dm': selected['id'],
                'comment': default['id'], 'content': default['id']}}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(self.c.get('/api/models').json['agent_bindings']['dm'], selected['id'])
            self.monitor.update(self.s, 'dm', {'agent_id': selected['id']})
            self.assertEqual(self.monitor.config(self.s, 'dm')['agent_id'], selected['id'])

    def test_ambiguous_account_name_never_chooses_first_agent(self):
        catalog = self.account_routes()
        catalog['agents'] = [{'id': 'one', 'name': '闲鱼-seller-a'}, {'id': 'two', 'name': '闲鱼-seller-a'}]
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            self.assertFalse(self.c.get('/api/models').json['bindings'].get('dm'))
            self.assertEqual(self.monitor.config(self.s, 'dm')['route_id'], '')

    def test_agent_choices_deduplicate_and_exclude_unbound_real_routes(self):
        catalog = self.account_routes()
        base = catalog['routes'][0]
        catalog['routes'].append({**base, 'id': 'account-a-comment'})
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            data = self.c.get('/api/models').json
            self.assertEqual(len(data['agent_choices']['dm']), 1)
            self.assertEqual(data['agent_choices']['dm'][0]['route_ids'], ['account-a', 'account-a-comment'])

    def test_agent_binding_rejects_other_account_and_is_single_choice(self):
        catalog = self.account_routes()
        catalog['routes'][1]['agent'] = {'id': 'other-agent', 'name': 'Other account'}
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            for endpoint, body in [('/api/models', {'agent_bindings': {'dm': 'other-agent'}}),
                                  ('/api/monitoring/dm', {'agent_id': 'other-agent'}),
                                  ('/api/monitoring/dm', {'agent_id': ['demo-agent']})]:
                self.assertEqual(self.c.put(endpoint, json=body, headers=self.headers).status_code, 400)
            config = self.c.put('/api/monitoring/dm', json={'agent_id': 'demo-agent'}, headers=self.headers).json
            self.assertEqual(config['route_id'], 'account-a')
            self.assertEqual(config['agent_id'], 'demo-agent')
            data = self.c.put('/api/models', json={'agent_bindings': {'dm': 'demo-agent'}}, headers=self.headers).json
            self.assertEqual(data['dm'], 'account-a')
            self.assertEqual(data['agents']['dm']['id'], 'demo-agent')

    def test_agent_history_keeps_original_snapshot_after_reanalysis(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        original = self.rows()[0]
        catalog = self.monitor.client(self.s).catalog()
        catalog['routes'][0]['agent'] = {'id': 'new-agent', 'name': 'New Agent'}
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            self.monitor.update(self.s, 'dm', {'agent_id': 'new-agent'})
            self.monitor.reanalyze(self.s, 'dm', original['id'])
            row = self.rows()[0]
            self.assertEqual(row['agent'], original['agent'])
            self.assertEqual(row['execution_agent']['id'], 'new-agent')
            self.assertEqual([a['agent']['id'] for a in row['agent_history']], ['demo-agent', 'new-agent'])

    def test_approval_blocks_changed_route_agent(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        row = self.rows()[0]
        catalog = self.monitor.client(self.s).catalog()
        catalog['routes'][0]['agent'] = {'id': 'unexpected', 'name': 'Changed'}
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            with self.assertRaises(ValueError):
                self.monitor.approve(self.s, 'dm', row['id'])
        self.assertEqual(self.platform.sends, [])
        self.assertEqual(self.rows()[0]['status'], 'review')

    def test_generation_blocks_changed_route_agent(self):
        config = self.enable(); msg = self.incoming()
        self.monitor.ingest(self.s, 'dm', msg, '900000', config)
        catalog = self.monitor.client(self.s).catalog()
        catalog['routes'][0]['agent'] = {'id': 'unexpected', 'name': 'Changed'}
        with patch('web.monitoring.DemoChannel.catalog', return_value=catalog):
            self.monitor.process(self.s, 'dm', self.rows()[0], config)
        self.assertEqual(self.rows()[0]['status'], 'failed')
        self.assertEqual(self.platform.sends, [])

    def test_network_timeout_does_not_submit_account_recovery_work_order(self):
        self.enable()
        with patch('web.account_maintenance.Bridge') as bridge, patch.object(self.platform,'socket',side_effect=TimeoutError()):
            self.monitor.tick(self.s,'dm');self.monitor.tick(self.s,'dm')
            bridge.return_value.notify.assert_not_called()
        with patch('web.account_maintenance.Bridge') as bridge:
            self.monitor.tick(self.s,'dm')
            args=bridge.return_value.notify.call_args.args
            self.assertEqual(args[:3],('seller-a','seller-a','connected'))
            self.assertEqual(args[-1],'listener.dm')
        self.assertEqual(self.platform.sends,[])

    def test_actual_receive_auth_failure_submits_one_recovery_order(self):
        self.enable()
        with patch('web.account_maintenance.Bridge') as bridge, patch.object(self.platform,'socket',side_effect=PlatformError('FAIL_SYS_SESSION_EXPIRED')):
            self.monitor.tick(self.s,'dm');self.monitor.tick(self.s,'dm')
            self.assertEqual(bridge.return_value.notify.call_count,1)
            args=bridge.return_value.notify.call_args.args
            self.assertEqual(args[:3],('seller-a','seller-a','expired'))
            self.assertEqual(args[-1],'listener.dm')

    def test_model_failure_is_not_reported_as_account_disconnect(self):
        self.enable()
        with patch('web.account_maintenance.Bridge') as bridge, patch.object(self.platform,'socket',side_effect=ValueError('模型配置不完整')):
            self.monitor.tick(self.s,'dm');bridge.return_value.notify.assert_not_called()

    def test_poll_baseline_does_not_reply_to_old_history(self):
        self.enable(automatic=True)
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.platform.sends, [])
    def test_repeated_poll_creates_one_proposal_no_manual_send(self):
        self.enable(); self.incoming()
        self.monitor.tick(self.s, 'dm'); self.monitor.tick(self.s, 'dm')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['status'], 'review')
        self.assertEqual(self.platform.sends, [])
    def test_approve_deduplicates_platform_write_and_records_receipt(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        row = self.rows()[0]
        first = self.monitor.approve(self.s, 'dm', row['id'])
        second = self.monitor.approve(self.s, 'dm', row['id'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.finish()['status'], 'sent')
        self.assertEqual(len(self.platform.sends), 1)
    def test_refresh_waits_for_send_intent_job_and_preserves_receipt(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        row = self.rows()[0]; intent = threading.Event(); release = threading.Event()
        original = self.s.send
        def delayed(*args, **kwargs):
            intent.set()
            if not release.wait(3): raise AssertionError('send was not released')
            return original(*args, **kwargs)
        results = []; errors = []
        def approve():
            try: self.monitor.approve(self.s, 'dm', row['id'])
            except Exception as exc: errors.append(exc)
        with patch.object(self.s, 'send', side_effect=delayed):
            sender = threading.Thread(target=approve); sender.start()
            self.assertTrue(intent.wait(3))
            stale = self.s.store.get(self.s.uid(), 'inbox-dm', row['id'])
            reader = threading.Thread(target=lambda: results.append(self.rows()))
            reader.start(); time.sleep(.05)
            self.assertTrue(reader.is_alive())
            release.set(); sender.join(3); reader.join(3)
        self.assertEqual(errors, [])
        self.assertIn(results[0][0]['status'], ('sending', 'sent'))
        self.assertEqual(self.finish()['status'], 'sent')
        self.monitor.process(self.s, 'dm', stale, self.monitor.config(self.s, 'dm'))
        self.assertEqual(self.rows()[0]['status'], 'sent')
        self.assertEqual(len(self.platform.sends), 1)

    def test_automatic_reply_is_bound_to_the_account(self):
        self.enable(automatic=True); self.incoming(); self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.finish()['status'], 'sent')
        self.assertEqual(len(self.platform.sends), 1)
        self.assertEqual(self.b.platform.sends, [])
    def test_stopped_listener_never_generates_or_sends(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        row = self.rows()[0]
        self.monitor.update(self.s, 'dm', {'enabled': False})
        with self.assertRaises(ValueError): self.monitor.approve(self.s, 'dm', row['id'])
        self.incoming('new-2'); self.monitor.tick(self.s, 'dm')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.platform.sends, [])
    def test_stop_during_platform_read_returns_without_waiting_or_sending(self):
        self.enable(automatic=True); self.incoming()
        entered, release = threading.Event(), threading.Event()
        original = self.platform.socket
        def delayed(operation, **kwargs):
            entered.set(); release.wait(5)
            return original(operation, **kwargs)
        with patch.object(self.platform, 'socket', side_effect=delayed):
            worker = threading.Thread(target=self.monitor.tick, args=(self.s, 'dm'))
            worker.start(); self.assertTrue(entered.wait(2))
            try:
                start = time.monotonic()
                self.monitor.update(self.s, 'dm', {'enabled': False})
                self.assertLess(time.monotonic()-start, 1)
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.platform.sends, []); self.assertEqual(self.rows(), [])
    def test_receipts_are_reconciled_while_listener_stopped(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        self.monitor.approve(self.s, 'dm', self.rows()[0]['id'])
        self.monitor.update(self.s, 'dm', {'enabled': False})
        self.assertEqual(self.finish()['status'], 'sent')
    def test_resume_keeps_watermark_and_collects_gap(self):
        first = self.enable()
        self.monitor.update(self.s, 'dm', {'enabled': False})
        self.incoming(); self.enable()
        self.assertEqual(self.monitor.config(self.s, 'dm')['since'], first['since'])
        self.monitor.tick(self.s, 'dm'); self.assertEqual(len(self.rows()), 1)
    def test_new_customer_message_makes_old_proposal_stale(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        old = self.rows()[0]
        self.incoming('new-2', age=1); self.monitor.tick(self.s, 'dm')
        with self.assertRaises(ValueError): self.monitor.approve(self.s, 'dm', old['id'])
        self.assertEqual(self.platform.sends, [])
        self.assertEqual(self.s.store.get(self.s.uid(), 'inbox-dm', old['id'])['status'], 'stale')
    def test_uncertain_send_never_retries(self):
        self.platform.timeout = True
        self.enable(automatic=True); self.incoming(); self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.finish()['status'], 'unknown')
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(len(self.platform.sends), 1)
    def test_dismissed_message_is_not_regenerated(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        self.monitor.dismiss(self.s, 'dm', self.rows()[0]['id'])
        self.monitor.tick(self.s, 'dm'); self.assertEqual(self.rows()[0]['status'], 'ignored')
    def test_plain_unstructured_model_text_cannot_auto_send(self):
        self.enable(automatic=True); self.incoming()
        client = self.monitor.client(self.s)
        client.send = lambda *a, **kw: {'id': 'fake', 'result': {'output': {'text': '我猜可以包邮'}}}
        with patch.object(self.monitor, 'client', return_value=client): self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows()[0]['status'], 'handoff')
        self.assertEqual(self.platform.sends, [])
    def test_model_declines_reply(self):
        self.enable(automatic=True); self.incoming()
        client = self.monitor.client(self.s)
        client.send = lambda *a, **kw: {'id': 'fake', 'result': {'output': {'structured': {'decision': 'no_reply'}}}}
        with patch.object(self.monitor, 'client', return_value=client): self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows()[0]['status'], 'no_reply'); self.assertEqual(self.platform.sends, [])
    def test_model_human_flag_is_preserved_in_automatic_mode(self):
        self.enable(automatic=True); self.incoming()
        client = self.monitor.client(self.s)
        client.send = lambda *a, **kw: {'id': 'fake', 'result': {'output': {'structured': {
            'reply': '请人工核对价格', 'requires_human': True}}}}
        with patch.object(self.monitor, 'client', return_value=client): self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows()[0]['status'], 'review')
        self.assertTrue(self.rows()[0]['requires_human']); self.assertEqual(self.platform.sends, [])
    def test_three_modes_drive_real_decision_and_approval_semantics(self):
        for surface in ('dm', 'comment'):
            for mode in ('request_approval', 'auto_approve', 'full_access'):
                with self.subTest(surface=surface, mode=mode):
                    self.monitor.update(self.s, surface, {'enabled': True, 'control_mode': mode,
                        'auto_reply': True, 'route_id': 'demo-assistant'})
                    self.incoming('mode-' + surface + mode, surface=surface, age=1)
                    self.monitor.tick(self.s, surface)
                    row = next(r for r in self.rows(surface) if r['message_id'] == 'mode-' + surface + mode)
                    request = row['request']
                    self.assertEqual(request['context']['control_mode'], mode)
                    if mode == 'request_approval':
                        self.assertEqual(row['status'], 'review')
                        self.monitor.approve(self.s, surface, row['id'])
                    for _ in range(100):
                        row = next(r for r in self.rows(surface) if r['id'] == row['id'])
                        if row['status'] == 'sent': break
                        time.sleep(.01)
                    self.assertEqual(row['status'], 'sent')
        self.assertEqual(len(self.platform.sends), 3)
        self.assertEqual(len(self.platform.comment_sends), 3)

    def test_mode_change_regenerates_pending_draft_and_then_automatically_sends_once(self):
        self.enable(); self.incoming(); self.monitor.tick(self.s, 'dm')
        first = self.rows()[0]
        self.monitor.update(self.s, 'dm', {'control_mode': 'full_access', 'auto_reply': True})
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.finish()['status'], 'sent')
        self.assertNotEqual(first['channel_id'], self.rows()[0]['channel_id'])
        self.assertEqual(self.rows()[0]['request']['context']['control_mode'], 'full_access')
        self.assertEqual(len(self.platform.sends), 1)

    def test_full_access_rejects_human_handoff_instead_of_sending_or_waiting_forever(self):
        self.monitor.update(self.s, 'dm', {'enabled': True, 'control_mode': 'full_access',
            'auto_reply': True, 'route_id': 'demo-assistant'})
        self.incoming(); client = self.monitor.client(self.s)
        client.send = lambda *a, **kw: {'id': 'fake', 'result': {'output': {'structured': {
            'decision': 'handoff', 'reply_text': '请主人批准', 'requires_human': True}}}}
        with patch.object(self.monitor, 'client', return_value=client): self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows()[0]['status'], 'failed')
        self.assertIn('完全放开', self.rows()[0]['error'])
        self.assertEqual(self.platform.sends, [])
        self.monitor.reanalyze(self.s, 'dm', self.rows()[0]['id'])
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.finish()['status'], 'sent')

    def test_send_switch_can_pause_full_access_without_changing_mode(self):
        self.monitor.update(self.s, 'dm', {'enabled': True, 'control_mode': 'full_access',
            'auto_reply': False, 'route_id': 'demo-assistant'})
        self.incoming(); self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows()[0]['status'], 'review'); self.assertEqual(self.platform.sends, [])
        self.monitor.update(self.s, 'dm', {'auto_reply': True})
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.finish()['status'], 'sent')

    def test_unknown_send_cannot_be_reanalyzed_and_repeated(self):
        self.platform.timeout = True; self.enable(automatic=True); self.incoming()
        self.monitor.tick(self.s, 'dm'); row = self.finish()
        self.assertEqual(row['status'], 'unknown')
        with self.assertRaises(ValueError): self.monitor.reanalyze(self.s, 'dm', row['id'])
        self.assertEqual(len(self.platform.sends), 1)

    def test_legacy_config_migrates_without_enabling_automatic_sends(self):
        for automatic, expected in [(False, 'request_approval'), (True, 'auto_approve')]:
            self.s.store.put(self.s.uid(), 'monitor', {'id': 'dm', 'enabled': True, 'automatic': automatic,
                'interval': 30, 'route_id': '', 'revision': 1, 'state': 'listening'}, 'dm')
            result = self.monitor.config(self.s, 'dm')
            self.assertEqual(result['control_mode'], expected)
            self.assertEqual(result['automatic'], automatic)
        for value in ['automatic', '', None, True, []]:
            with self.assertRaises(ValueError): self.monitor.update(self.s, 'dm', {'control_mode': value})

    def test_expired_account_overwrites_previous_health_result(self):
        with patch.object(self.platform, 'check', side_effect=PlatformError('FAIL_SYS_SESSION_EXPIRED')):
            response = self.c.post('/api/account/check', json={'request_id': 'expiry-check'}, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            self.s.executor.shutdown(wait=True)
        account = next(a for a in self.c.get('/api/accounts').json['items'] if a['id'] == self.s.uid())
        self.assertEqual(account['status'], 'error'); self.assertFalse(account['check']['can_message'])
    def test_expired_listener_explains_relogin_and_can_resume(self):
        self.enable()
        with patch.object(self.platform, 'socket', side_effect=PlatformError('FAIL_SYS_SESSION_EXPIRED')):
            self.monitor.tick(self.s, 'dm')
        config = self.monitor.config(self.s, 'dm')
        self.assertEqual(config['state'], 'error'); self.assertIn('重新扫码', config['error'])
        self.assertTrue(config['enabled'])
        self.assertTrue(config['blocked_auth'])
        self.monitor.connection_restored(self.s)
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.monitor.config(self.s, 'dm')['state'], 'listening')
    def test_security_challenge_blocks_repeated_reads_until_successful_account_check(self):
        self.enable()
        with patch.object(self.platform, 'socket', side_effect=PlatformError('FAIL_SYS_USER_VALIDATE, RGV587_ERROR')) as socket:
            self.monitor.tick(self.s, 'dm')
            self.monitor.tick(self.s, 'dm')
            self.assertEqual(socket.call_count, 1)
        config = self.monitor.config(self.s, 'dm')
        self.assertTrue(config['blocked_auth'])
        self.assertIn('安全验证', config['error'])
        health = self.s.store.get(self.s.uid(), 'settings', 'account-check')
        self.assertTrue(health['needs_verification']); self.assertFalse(health['can_message'])
        self.assertEqual(self.c.post('/api/monitoring/dm/poll', json={}, headers=self.headers).status_code, 400)
        response = self.c.post('/api/account/check', json={'request_id': 'after-verification'}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        for _ in range(100):
            job = self.s.get_job(response.json['id'])
            if job['status'] == 'completed': break
            time.sleep(.01)
        self.assertEqual(job['status'], 'completed')
        self.assertFalse(self.monitor.config(self.s, 'dm')['blocked_auth'])
        self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.monitor.config(self.s, 'dm')['state'], 'listening')
    def test_lost_channel_response_reuses_immutable_request(self):
        self.enable(); self.incoming(); calls = []
        client = self.monitor.client(self.s)
        original = client.send
        def send(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1: raise ValueError('response lost')
            return original(*args, **kwargs)
        client.send = send
        with patch.object(self.monitor, 'client', return_value=client):
            self.monitor.tick(self.s, 'dm'); self.monitor.tick(self.s, 'dm')
        self.assertEqual(calls[0], calls[1]); self.assertEqual(self.rows()[0]['status'], 'review')
    def test_comment_flow_has_separate_records_and_receipt(self):
        self.enable('comment', automatic=True); self.incoming(surface='comment')
        self.monitor.tick(self.s, 'comment'); self.s.executor.shutdown(wait=True)
        self.assertEqual(self.rows('comment')[0]['status'], 'sent')
        self.assertEqual(len(self.platform.comment_sends), 1)
        self.assertEqual(self.platform.sends, [])
    def test_existing_own_comment_reply_prevents_duplicate_response(self):
        self.enable('comment'); incoming = self.incoming(surface='comment')
        self.monitor.tick(self.s, 'comment'); row = self.rows('comment')[0]
        self.platform.comment_rows.append({**incoming, 'id': 'own-reply', 'reply_to_id': incoming['id'],
            'outgoing': True, 'sender_id': self.s.uid(), 'timestamp': time.time()+1})
        self.monitor.update(self.s, 'comment', {'control_mode': 'full_access'})
        self.monitor.tick(self.s, 'comment')
        self.assertEqual(self.rows('comment')[0]['status'], 'stale')
        self.assertEqual(self.platform.comment_sends, [])
        with self.assertRaises(ValueError): self.monitor.approve(self.s, 'comment', row['id'])

    def test_unsupported_comments_cannot_be_enabled(self):
        self.platform.supports_comments = False
        self.assertFalse(self.monitor.view(self.s, 'comment')['supported'])
        with self.assertRaises(ValueError): self.enable('comment')

    def test_read_only_comments_backfill_but_cannot_send(self):
        self.platform.supports_comment_replies = False
        self.enable('comment')
        self.incoming(surface='comment', age=-600)
        self.monitor.tick(self.s, 'comment')
        view = self.monitor.view(self.s, 'comment')
        self.assertTrue(view['supported'])
        self.assertFalse(view['can_reply'])
        self.assertEqual(len(view['items']), 1)
        self.assertEqual(view['items'][0]['status'], 'review')
        with self.assertRaisesRegex(ValueError, '闲鱼 App'):
            self.monitor.approve(self.s, 'comment', view['items'][0]['id'])
        self.assertEqual(self.rows('comment')[0]['status'], 'review')
        self.assertEqual(self.platform.comment_sends, [])
        with self.assertRaises(ValueError): self.enable('comment', automatic=True)
    def test_no_route_receive_only(self):
        self.enable(route=''); self.incoming(); self.monitor.tick(self.s, 'dm')
        self.assertEqual(self.rows()[0]['status'], 'received')
    def test_auto_mode_requires_a_valid_route_and_strict_settings(self):
        for data in [{'enabled': True, 'automatic': True, 'route_id': ''},
                     {'enabled': True, 'route_id': 'unknown'}, {'enabled': 'false'}, {'interval': 0}, {'interval': True}]:
            with self.assertRaises(ValueError): self.monitor.update(self.s, 'dm', data)
    def test_selected_account_requests_do_not_change_other_accounts(self):
        header = {**self.headers, 'X-Account-ID': 'seller-b'}
        r = self.c.post('/api/templates', json={'title': 'B only', 'text': 'isolated'}, headers=header)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(len([t for t in self.b.store.list('seller-b', 'template') if t['title'] == 'B only']), 1)
        self.assertFalse(any(t['title'] == 'B only' for t in self.s.store.list('seller-a', 'template')))
        self.assertEqual(self.s.uid(), 'seller-a')
        self.assertEqual(self.c.get('/api/meta', headers=header).json['account']['id'], 'seller-b')
    def test_unknown_account_and_cross_account_job_are_rejected(self):
        self.assertEqual(self.c.get('/api/products', headers={'X-Account-ID': 'missing'}).status_code, 404)
        j = self.s.sync('sync-account-a')
        self.assertEqual(self.c.get('/api/jobs/' + j['id'], headers={'X-Account-ID': 'seller-b'}).status_code, 404)
    def test_account_media_query_and_auth_isolation(self):
        file = io.BytesIO(); Image.new('RGB', (20, 20)).save(file, 'PNG'); file.seek(0)
        r = self.c.post('/api/media', data={'file': (file, 'image.png')}, headers={**self.headers, 'X-Account-ID': 'seller-b'})
        self.assertEqual(r.status_code, 200, r.json)
        response = self.c.get(r.json['url']); self.assertEqual(response.status_code, 200); response.close()
        self.assertEqual(self.c.get('/api/media/' + r.json['id']).status_code, 404)
    def test_demo_channel_cannot_use_real_config(self):
        with patch('web.monitoring.ChannelClient', side_effect=AssertionError('real channel forbidden')):
            self.assertTrue(self.c.get('/api/models').json['demo'])
            r = self.c.post('/api/channel/messages', json={'message_id': 'synthetic-draft', 'draft_description': '个人闲置台灯', 'text': '优化描述'}, headers=self.headers)
            self.assertEqual(r.status_code, 202, r.json)
    def test_account_auth_cannot_be_bypassed_with_header(self):
        app = create_app(Path(self.tmp.name) / 'protected', platform=RecordingPlatform('protected'), auth=True)
        try:
            self.assertEqual(app.test_client().get('/api/meta', headers={'X-Account-ID': 'protected'}).status_code, 401)
            self.assertEqual(app.test_client().get('/api/media/missing?account_id=protected').status_code, 401)
        finally:
            app.extensions['monitor'].close(); app.extensions['accounts'].close()
    def test_session_registry_survives_restart_without_exposing_credentials(self):
        root = Path(self.tmp.name) / 'registry'
        session = root / 'legacy.json'
        private_json(session, {'device_id': 'device', 'cookies': [{'name': 'unb', 'value': '123456'},
            {'name': 'tracknick', 'value': 'Test'}, {'name': 'secret', 'value': 'PRIVATE'}]})
        service = Service(root, Platform(session)); registry = Accounts(service)
        try:
            self.assertTrue(registry.path('123456').is_file())
            self.assertNotIn('PRIVATE', str(registry.list()))
            refreshed = json.loads(registry.path('123456').read_text())
            refreshed['device_id'] = 'refreshed-device'
            private_json(registry.path('123456'), refreshed)
            legacy_service = Service(root, Platform(session))
            fresh_registry = Accounts(legacy_service)
            try:
                self.assertEqual(json.loads(fresh_registry.path('123456').read_text())['device_id'], 'refreshed-device')
            finally: fresh_registry.close()
            second = Service(root, Platform(root / 'missing.json'))
            restored = Accounts(second)
            try: self.assertEqual(restored.resolve('123456').uid(), '123456')
            finally: restored.close(); second.executor.shutdown(wait=True)
        finally: registry.close()


if __name__ == '__main__': unittest.main()
