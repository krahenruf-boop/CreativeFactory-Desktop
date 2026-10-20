"""Real CF/SQLite approval integration; fake external delivery, no paid APIs."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_batch_workspace as fixtures
from autocut.feishu_tasks import cards
from autocut.feishu_tasks import recovery
from autocut.feishu_tasks.cloud import QuarkCloud, CloudError, share_url, natural
from autocut.feishu_tasks.storage import Journal


class Transport:
    state, error = 'TEST', ''
    def __init__(self): self.sent, self.updated = [], []
    def connected(self): return True
    def start(self): pass
    def stop(self): pass
    def request(self, *args, **kwargs): return {'bot': {'open_id': 'ou_bot'}}
    def send(self, chat, card, key):
        self.sent.append((chat, card, key)); return 'om_' + str(len(self.sent))
    def update(self, message, card): self.updated.append((message, card))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        fixtures.BatchTests.setUp(self)
        self.bridge = self.app.state.task_bridge
        self.bridge.transport = Transport()
        self.bridge.bot_open_id = 'ou_bot'
        self.bridge.journal.configure(enabled=True, accepting=False, chat_id='oc_group', admin_open_id='ou_admin')
        self.bridge.media_root = self.root / 'media'
        form = self.bridge.journal.form('ou_owner', 'oc_group')
        self.task, _ = self.bridge.journal.create_task(form, dict(product_name='测试产品', preset_id='general', market='Brazil', mode='Transport', expected_count=3, share_url='https://pan.quark.cn/s/test123', passcode='', requirements='', owner='ou_owner', chat='oc_group', test=True))
        self.task = self.bridge.journal.update(self.task['id'], job_id=self.id, state='PROCESSING')
        self.sequence = 0
    tearDown = fixtures.BatchTests.tearDown
    raw = fixtures.BatchTests.raw
    state = fixtures.BatchTests.state
    post = fixtures.BatchTests.post

    def test_cloud_failure_blocks_listener_without_mutating_tasks(self):
        before = self.bridge.journal.tasks()
        self.bridge.cloud = MagicMock()
        self.bridge.cloud.health.return_value = dict(ready=False, error='QUARK_-104', notice='', checked_at=1)
        with patch.object(self.bridge.transport, 'start') as start:
            self.bridge.start()
        start.assert_not_called()
        self.assertIsNone(self.bridge.pool)
        self.assertEqual(self.bridge.threads, [])
        self.assertEqual(self.bridge.journal.tasks(), before)
        state = self.bridge.status()
        self.assertEqual(state['state'], 'BLOCKED')
        self.assertFalse(state['quark_health']['ready'])
        self.assertIn('Codex', state['error_message'])

    def test_cloud_check_passes_before_listener_and_is_not_repeated_while_running(self):
        checks = []
        self.bridge.cloud = MagicMock()
        def health():
            checks.append('cloud')
            return dict(ready=True, error='', notice='', checked_at=1)
        self.bridge.cloud.health.side_effect = health
        with patch.object(self.bridge.transport, 'start', side_effect=lambda: checks.append('listener')), \
             patch('autocut.feishu_tasks.bridge.threading.Thread') as thread:
            thread.return_value.is_alive.return_value = True
            self.bridge.start()
            self.bridge.start()
        self.assertEqual(checks, ['cloud', 'listener'])
        self.bridge.stop()

    def test_missing_historical_job_does_not_block_later_delivery(self):
        orphan = self.bridge.journal.update(self.task['id'], job_id='permanently-deleted-job')
        form = self.bridge.journal.form('ou_owner', 'oc_group')
        current, _ = self.bridge.journal.create_task(form, dict(product_name='新任务', preset_id='general', market='Brazil', mode='Transport', expected_count=3, share_url='https://pan.quark.cn/s/new123', passcode='', requirements='', owner='ou_owner', chat='oc_group', test=True))
        current = self.bridge.journal.update(current['id'], job_id=self.id, state='PROCESSING')
        self.store.update(self.id, state='COMPLETED')
        view = dict(phase='RUNNING', items=[dict(rank=f'{r:02}', pipeline_status='COMPLETED') for r in range(1, 4)])
        with patch.object(self.bridge.batch, 'view', return_value=view), \
             patch.object(self.bridge, 'push_review'), patch.object(self.bridge, 'push_status'), \
             patch.object(self.bridge, 'flush_notifications'), \
             patch.object(self.bridge, 'schedule') as schedule, \
             patch('autocut.feishu_tasks.recovery.busy', return_value=False):
            self.bridge.monitor_once()
        schedule.assert_called_once_with(current['id'], 'upload')
        self.assertEqual(self.bridge.journal.task(orphan['id']), orphan)
        self.assertEqual(self.bridge.journal.task(current['id'])['state'], 'READY_UPLOAD')
        with self.assertRaises(KeyError):
            self.store.get('permanently-deleted-job')

    def action(self, name, fields=None, operator='ou_owner', revision=None, **values):
        self.sequence += 1
        event_id = 'event-' + str(self.sequence)
        view = self.bridge.batch.view(self.id)
        body = dict(kind='action', operator=operator, chat='oc_group', value=dict(action=name, task_id=self.task['id'], revision=revision or view['action_token'], **values), fields=fields or {})
        self.bridge.dispatch(event_id, body)
        return event_id, body

    def scripts(self): self.action('approve_scripts')

    def choices(self, values=None):
        self.action('save_titles', fields=values or {'title_01': '1', 'title_02': '2', 'title_03': '3'})

    def test_01_non_owner_callback_rejected_before_any_event_write(self):
        raw = dict(header={'event_id': 'wrong-owner'}, event=dict(operator={'open_id': 'ou_other'}, context={'open_chat_id': 'oc_group'}, action={'value': dict(action='approve_scripts', task_id=self.task['id'], revision=self.bridge.batch.view(self.id)['action_token'])}))
        before = copy.deepcopy(self.store.get(self.id))
        result = self.bridge.receive(raw, 'action')
        self.assertFalse(result['accepted']); self.assertIn('只有', result['message'])
        self.assertIsNone(self.bridge.journal.next_event()); self.assertEqual(self.store.get(self.id), before)

    def test_02_stale_card_does_not_commit(self):
        token = self.bridge.batch.view(self.id)['action_token']
        self.store.item(self.id, 2, script_status='WAITING_APPROVAL')
        with self.assertRaisesRegex(ValueError, 'STALE_ACTION'):
            self.action('approve_scripts', revision=token)
        self.assertEqual(self.queue.enqueued, [])

    def test_03_real_two_stage_batch_choices_and_idempotent_replay(self):
        event_id, body = self.action('approve_scripts')
        self.assertEqual(self.bridge.batch.view(self.id)['phase'], 'TITLES')
        self.assertEqual(self.queue.enqueued, [])
        # Recover a process exit after the CF commit but before event_done.
        self.bridge.dispatch(event_id, body)
        self.choices(); event_id, body = self.action('approve_titles')
        self.assertEqual(self.bridge.batch.view(self.id)['phase'], 'RUNNING')
        self.assertEqual(len(self.queue.batches), 1)
        self.bridge.dispatch(event_id, body)
        self.assertEqual(len(self.queue.batches), 1)
        self.assertEqual([x['selected_title_id'] for x in self.store.get(self.id)['items']], [1, 2, 3])

    def test_04_missing_choice_blocks_entire_title_release(self):
        self.scripts(); self.choices({'title_01': '2'})
        with self.assertRaisesRegex(ValueError, 'BATCH_TITLE_REQUIRED'):
            self.action('approve_titles')
        self.assertEqual(self.queue.batches, [])

    def test_confirm_titles_captures_current_form_without_separate_save(self):
        self.scripts()
        self.action('approve_titles', fields={'title_01': '3', 'title_02': '2', 'title_03': '1'})
        self.assertEqual(self.bridge.batch.view(self.id)['phase'], 'RUNNING')
        self.assertEqual([x['selected_title_id'] for x in self.store.get(self.id)['items']], [3, 2, 1])

    def test_title_page_navigation_saves_current_form_before_final_confirmation(self):
        base = self.store.get(self.id)['items'][0]; base.pop('rank')
        for rank in (4, 5, 6): self.store.item(self.id, rank, **copy.deepcopy(base))
        self.store.update(self.id, source_count=6)
        self.scripts()
        self.action('page', page=1, fields={'title_01': '1', 'title_02': '2', 'title_03': '3'})
        task = self.bridge.journal.task(self.task['id'])
        self.assertEqual(task['page'], 1)
        self.assertEqual(task['selections'], {'01': '1', '02': '2', '03': '3'})
        self.action('approve_titles', fields={'title_04': '3', 'title_05': '2', 'title_06': '1'})
        self.assertEqual([x['selected_title_id'] for x in self.store.get(self.id)['items']], [1, 2, 3, 3, 2, 1])

    def test_single_page_has_no_invalid_navigation_actions(self):
        payload = json.dumps(cards.review(self.task, self.bridge.batch.view(self.id)))
        self.assertNotIn('"action": "page"', payload)

    def test_each_title_has_visible_select_button_and_selection_is_durable(self):
        self.scripts()
        view = self.bridge.batch.view(self.id)
        payload = cards.review(self.task, view)
        buttons = [button for element in payload['elements'] if element.get('tag') == 'action' for button in element['actions'] if button.get('value', {}).get('action') == 'select_title']
        self.assertEqual(len(buttons), 9)
        self.assertFalse(any(e.get('tag') == 'form' for e in payload['elements']))
        for rank, choice in [('01', '2'), ('02', '3'), ('03', '1')]:
            self.action('select_title', rank=rank, title_id=choice)
        task = self.bridge.journal.task(self.task['id'])
        self.assertEqual(task['selections'], {'01': '2', '02': '3', '03': '1'})
        self.assertIn('已选标题 2', json.dumps(cards.review(task, view), ensure_ascii=False))
        self.assertEqual(self.queue.batches, [])
        self.action('approve_titles')
        self.assertEqual(len(self.queue.batches), 1)

    def test_title_recovery_repairs_missing_chinese_without_changing_titles(self):
        from autocut.pipeline import Manager
        self.scripts()
        row = self.store.get(self.id)['items'][2]
        titles = copy.deepcopy(row['title_candidates'])
        for title in titles:
            title['text_zh'] = ''
        self.store.item(self.id, '03', title_candidates=titles)
        original_scripts = [x['final_script'] for x in self.store.get(self.id)['items']]
        manager = Manager(self.store, MagicMock(), MagicMock())
        def translate(rows, model):
            return {'rows': [dict(t, text_zh='这是完整中文对照') for t in rows]}
        try:
            with patch('autocut.copy_presentation.TranslationService.translate', side_effect=translate) as translated, patch('autocut.content_confirmation.ContentConfirmation.title_request') as regenerated:
                manager.prepare_batch_titles(self.id, ['03'])
                manager.active[self.id].result(timeout=3)
            self.assertEqual(translated.call_count, 1)
            regenerated.assert_not_called()
            job = self.store.get(self.id)
            self.assertEqual(job['state'], 'WAITING_TITLE_APPROVAL')
            self.assertTrue(self.bridge.batch.view(self.id)['all_titles_ready'])
            self.assertEqual([t['text'] for t in job['items'][2]['title_candidates']], [t['text'] for t in titles])
            self.assertEqual([x['final_script'] for x in job['items']], original_scripts)
            self.assertFalse(any(x.get('audio_result') for x in job['items']))
        finally:
            manager.queue.shutdown(wait=True)

    def test_05_page_preserves_prior_selection(self):
        self.scripts(); self.choices({'title_01': '2'}); self.choices({'title_02': '3'})
        chosen = self.bridge.journal.task(self.task['id'])['selections']
        self.assertEqual(chosen, {'01': '2', '02': '3'})

    def test_switching_back_to_previous_title_updates_card_again(self):
        self.scripts(); self.bridge.flush_notifications()
        for choice in ('1', '2', '1'):
            self.action('select_title', rank='01', title_id=choice)
            self.bridge.flush_notifications()
        task = self.bridge.journal.task(self.task['id'])
        self.assertEqual(task['selections'], {'01': '1'})
        self.assertEqual(len(self.bridge.transport.updated), 3)
        self.assertIn('已选标题 1', json.dumps(self.bridge.transport.updated[-1][1], ensure_ascii=False))

    def test_06_one_title_regeneration_only_invalidates_that_choice(self):
        self.scripts(); self.choices(); self.action('regenerate_title', rank='02')
        task = self.bridge.journal.task(self.task['id'])
        self.assertEqual(task['selections'], {'01': '1', '03': '3'})
        self.assertEqual(len(self.adapter.calls), 1)

    def test_07_script_reopen_returns_to_script_gate(self):
        self.scripts(); self.choices(); self.action('reopen_script', rank='02')
        self.assertEqual(self.bridge.batch.view(self.id)['phase'], 'SCRIPTS')
        self.assertEqual(self.bridge.journal.task(self.task['id'])['selections'], {'01': '1', '03': '3'})
        self.assertEqual(self.queue.enqueued, [])

    def test_08_callback_dedupe_has_real_durable_queue(self):
        raw = dict(header={'event_id': 'unique-action'}, event=dict(operator={'open_id': 'ou_owner'}, context={'open_chat_id': 'oc_group'}, action={'value': dict(action='page', task_id=self.task['id'], revision=self.bridge.batch.view(self.id)['action_token'], page=0)}))
        self.assertTrue(self.bridge.receive(raw, 'action')['accepted'])
        self.assertTrue(self.bridge.receive(raw, 'action')['accepted'])
        event_id, _ = self.bridge.journal.next_event(); self.bridge.journal.event_done(event_id)
        self.assertIsNone(self.bridge.journal.next_event())

    def test_09_admin_can_approve_but_cannot_use_another_person_edit_form(self):
        self.action('approve_scripts', operator='ou_admin')
        form = self.bridge.journal.form('ou_owner', 'oc_group', kind='edit', task_id=self.task['id'])
        with self.assertRaisesRegex(ValueError, 'FEISHU_TASK_FORBIDDEN'):
            self.bridge.journal.get_form(form, 'ou_admin', 'oc_group')

    def test_10_ack_is_local_no_provider_or_delivery(self):
        self.bridge.transport.request = MagicMock(side_effect=AssertionError('network in callback'))
        raw = dict(header={'event_id': 'ack'}, event=dict(operator={'open_id': 'ou_owner'}, context={'open_chat_id': 'oc_group'}, action={'value': dict(action='page', task_id=self.task['id'], revision=self.bridge.batch.view(self.id)['action_token'], page=0)}))
        started = time.monotonic(); self.assertTrue(self.bridge.receive(raw, 'action')['accepted'])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.adapter.calls, [])

    def test_11_new_submission_requires_explicit_market_without_preset(self):
        fields = dict(product_name='手机包', preset_id='general', market='preset', mode='Transport', expected_count='3', share_url='https://pan.quark.cn/s/test123')
        with self.assertRaisesRegex(ValueError, 'FEISHU_TASK_MARKET_REQUIRED'):
            self.bridge.validate_form(fields)
        fields['preset_id'] = 'bag'
        self.assertEqual(self.bridge.validate_form(fields)['market'], 'Mexico')

    def test_new_submission_defaults_without_preset_and_preserves_explicit_market(self):
        fields = dict(product_name='手机包', share_url='https://pan.quark.cn/s/test123')
        data = self.bridge.validate_form(fields)
        self.assertEqual((data['preset_id'], data['market'], data['expected_count']), ('general', 'Brazil', 10))
        fields.update(market='Mexico', expected_count='7')
        data = self.bridge.validate_form(fields)
        self.assertEqual((data['market'], data['expected_count']), ('Mexico', 7))
        fields['expected_count'] = ''
        with self.assertRaisesRegex(ValueError, 'FEISHU_TASK_COUNT_INVALID'):
            self.bridge.validate_form(fields)

    def test_verified_admin_can_submit_ten_without_enabling_general_intake(self):
        self.bridge.journal.configure(verified_at=time.time())
        self.bridge.dispatch('new-ten', dict(kind='message', operator='ou_admin', chat='oc_group', text='新建任务'))
        with self.bridge.journal.connect() as db:
            row = db.execute('SELECT id,data FROM forms ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertFalse(json.loads(row['data'])['defaults']['test'])
        self.bridge.schedule = MagicMock()
        self.bridge.dispatch('submit-ten', dict(kind='action', operator='ou_admin', chat='oc_group',
                             value=dict(action='submit', form_id=row['id']),
                             fields=dict(product_name='手机包', market='Brazil', expected_count='10', share_url='https://pan.quark.cn/s/test123')))
        task = self.bridge.journal.tasks()[-1]
        self.assertFalse(task['test'])
        self.assertEqual(task['expected_count'], 10)
        self.assertFalse(self.bridge.journal.config()['accepting'])
        self.bridge.schedule.assert_called_once_with(task['id'], 'intake')

    def test_12_notifications_update_single_status_and_mention_on_change(self):
        self.bridge.push_status(self.task, force=True); self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)
        task = self.bridge.journal.update(self.task['id'], state='UPLOAD_ERROR', error='上传未完成', revision=1)
        self.bridge.push_status(task, force=True); self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.updated), 1)
        self.assertEqual(len(self.bridge.transport.sent), 1)
        alert = self.bridge.transport.updated[-1][1]
        self.assertIn('ou_owner', json.dumps(alert))

    def test_share_prose_submission_and_changed_repeat_update_receipt_only(self):
        fid = self.bridge.journal.form('ou_admin', 'oc_group')
        fields = dict(product_name='手机包', share_url='我用夸克网盘分享了「素材」 链接：https://pan.quark.cn/s/abc123 提取码：Ab12')
        body = dict(kind='action', operator='ou_admin', chat='oc_group', message_id='om_form', value=dict(action='submit', form_id=fid), fields=fields)
        self.bridge.schedule = MagicMock()
        self.bridge.dispatch('submit1', body)
        task = self.bridge.journal.submitted_task(fid)
        self.assertEqual(task['share_url'], 'https://pan.quark.cn/s/abc123')
        self.assertEqual(task['passcode'], 'Ab12')
        self.assertEqual(task['status_message_id'], 'om_form')
        body['fields']['share_url'] = 'https://pan.quark.cn/s/other'
        self.bridge.dispatch('submit2', body)
        self.bridge.flush_notifications()
        self.assertEqual(self.bridge.journal.submitted_task(fid)['share_url'], task['share_url'])
        self.bridge.schedule.assert_called_once()
        self.assertEqual(self.bridge.transport.sent, [])
        self.assertNotIn('提交任务', json.dumps(self.bridge.transport.updated[-1]))

    def test_edit_returns_to_same_card_and_retry_keeps_text_without_stale_error(self):
        from test_copy_presentation import BR
        self.bridge.push_review(self.task); self.bridge.flush_notifications()
        self.action('edit_script', rank='01'); self.bridge.flush_notifications()
        editor = self.bridge.journal.task(self.task['id'])['review_editor']
        body = dict(kind='action', operator='ou_owner', chat='oc_group',
                    value=dict(action='apply_edit', task_id=self.task['id'], rank='01', kind='script', form_id=editor['form_id'], revision=editor['revision']),
                    fields=dict(text=BR, language='auto'))
        original = self.bridge.review.draft
        def fail(*args):
            self.store.item(self.id, '01', operation_inflight=True)
            self.store.item(self.id, '01', operation_inflight=False)
            raise ValueError('SCRIPT_DRAFT_INVALID')
        self.bridge.review.draft = fail
        with self.assertRaisesRegex(ValueError, 'SCRIPT_DRAFT_INVALID'):
            self.bridge.dispatch('failed-edit', body)
        self.bridge.action_error('failed-edit', body, ValueError('SCRIPT_DRAFT_INVALID'))
        self.bridge.flush_notifications()
        self.assertIn(BR, json.dumps(self.bridge.transport.updated[-1][1], ensure_ascii=False))
        # A different rank changing must not discard this editor either.
        self.store.item(self.id, '02', final_script=BR+' Veja agora')
        self.bridge.review.draft = original
        self.bridge.dispatch('retry-edit', body)
        self.bridge.flush_notifications()
        self.assertIsNone(self.bridge.journal.task(self.task['id'])['review_editor'])
        self.assertEqual(len(self.bridge.transport.sent), 1)
        self.assertEqual(self.queue.enqueued, [])
        self.bridge.review.draft = MagicMock(side_effect=AssertionError('repeat provider'))
        self.bridge.dispatch('double-click', body)
        self.bridge.review.draft.assert_not_called()

    def test_editor_rejects_actual_content_change_on_same_rank(self):
        self.action('edit_script', rank='01')
        editor = self.bridge.journal.task(self.task['id'])['review_editor']
        self.store.item(self.id, '01', final_script='Outra versão em português para esta bolsa')
        with self.assertRaisesRegex(ValueError, 'STALE_ACTION'):
            self.action('apply_edit', rank='01', kind='script', form_id=editor['form_id'], revision=editor['revision'], fields=dict(text='Outra frase', language='auto'))

    def test_multipart_edit_is_combined_before_provider_and_duplicate_guard(self):
        self.action('edit_script', rank='01')
        editor = self.bridge.journal.task(self.task['id'])['review_editor']
        original = 'a'*1000+'segundo trecho completo'
        body = dict(kind='action', operator='ou_owner', chat='oc_group',
                    value=dict(action='apply_edit', task_id=self.task['id'], rank='01', kind='script',
                               form_id=editor['form_id'], revision=editor['revision']),
                    fields=dict(text=original[:1000], text_part_2=original[1000:], language='auto'))
        duplicate = json.loads(json.dumps(body))
        self.bridge.review.draft = MagicMock(return_value={})
        self.bridge.dispatch('multipart-edit', body)
        self.assertEqual(self.bridge.review.draft.call_args.args[2]['text'], original)
        self.bridge.dispatch('multipart-double-click', duplicate)
        self.assertEqual(self.bridge.review.draft.call_count, 1)
        self.assertEqual(self.queue.enqueued, [])

    def test_multipart_edit_error_card_preserves_complete_manuscript(self):
        self.action('edit_script', rank='01')
        editor = self.bridge.journal.task(self.task['id'])['review_editor']
        original = 'a'*1000+'preservar esta parte final'
        body = dict(kind='action', operator='ou_owner', chat='oc_group',
                    value=dict(action='apply_edit', task_id=self.task['id'], rank='01', kind='script',
                               form_id=editor['form_id'], revision=editor['revision']),
                    fields=dict(text=original[:1000], text_part_2=original[1000:], language='auto'))
        self.bridge.review.draft = MagicMock(side_effect=ValueError('SCRIPT_DRAFT_INVALID'))
        with self.assertRaisesRegex(ValueError, 'SCRIPT_DRAFT_INVALID'):
            self.bridge.dispatch('multipart-failed-edit', body)
        self.bridge.action_error('multipart-failed-edit', body, ValueError('SCRIPT_DRAFT_INVALID'))
        current = self.bridge.journal.task(self.task['id'])['review_editor']
        self.assertEqual(current['fields']['text'], original)
        self.assertFalse(current['busy'])

    def test_save_and_next_uses_same_review_message(self):
        from test_copy_presentation import BR
        self.action('edit_script', rank='01'); self.bridge.flush_notifications()
        editor = self.bridge.journal.task(self.task['id'])['review_editor']
        self.action('apply_edit', rank='01', kind='script', form_id=editor['form_id'], next_rank=True, fields=dict(text=BR, language='auto'))
        self.bridge.flush_notifications()
        self.assertEqual(self.bridge.journal.task(self.task['id'])['review_editor']['rank'], '02')
        self.assertEqual(len(self.bridge.transport.sent), 1)
        self.action('cancel_edit')
        self.assertIsNone(self.bridge.journal.task(self.task['id'])['review_editor'])

    def test_all_forms_use_full_width_controls(self):
        for payload in [cards.new_task('form', []), cards.new_task('form', [], media_only=True),
                        cards.edit('form', self.task, '01', 'script', 'v'), cards.edit('form', self.task, '01', 'title', 'v')]:
            controls = [e for form in payload['elements'] if form.get('tag') == 'form' for e in form['elements'] if e['tag'] in {'input','select_static'}]
            self.assertTrue(controls)
            self.assertTrue(all(e.get('width') == 'fill' for e in controls))

    def test_reject_ambiguous_or_non_quark_share(self):
        for url in ['https://pan.quark.cn.evil/s/abc', 'https://evil.test/s/abc',
                    '链接：https://pan.quark.cn/s/abc https://pan.quark.cn/s/xyz']:
            with self.assertRaisesRegex(ValueError, 'QUARK_LINK_INVALID'):
                share_url(url)

    def test_13_expired_status_not_sent_after_newer_status(self):
        self.bridge.push_status(self.task, force=True)
        task = self.bridge.journal.update(self.task['id'], state='UPLOAD_ERROR', error='失败', revision=1)
        self.bridge.push_status(task, force=True); self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)
        self.assertIn('上传失败', self.bridge.transport.sent[0][1]['header']['title']['content'])

    def test_14_long_copy_is_complete_and_cards_bounded(self):
        view = self.bridge.batch.view(self.id)
        long_text = '长口播测试' * 8000
        view['items'][0]['script']['text'] = long_text
        chunks = [part[1] for page in cards.review_pages(view) for part in page if part[0]['rank'] == '01']
        self.assertIn(long_text, ''.join(chunks))
        for page in range(len(cards.review_pages(view))):
            payload = cards.review(dict(self.task, page=page), view)
            self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), 30000)

    def test_15_web_folder_authorization_still_required(self):
        spec = dict(request_id='ungranted-folder', input_path=str(self.root), product_profile_id='general', product_name='非法选择', market='Brazil')
        response = self.c.post('/jobs', json=spec)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.store.by_request('ungranted-folder'))

    def test_16_integration_config_requires_local_auth_and_verified_gate(self):
        self.assertEqual(self.c.post('/integrations/feishu-tasks', json={'accepting': True}).status_code, 400)
        self.assertEqual(self.c.get('/integrations/feishu-tasks', headers={'Authorization': 'bad'}).status_code, 401)

    def test_17_completed_bridge_status_never_queries_moved_media(self):
        task = self.bridge.journal.update(self.task['id'], state='DONE', delivered_ranks=['01', '02', '03'])
        self.bridge.batch.view = MagicMock(side_effect=AssertionError('historical output check'))
        self.bridge.push_status(task, force=True)
        self.bridge.flush_notifications()
        self.assertIn('已完成', self.bridge.transport.sent[0][1]['header']['title']['content'])

    def test_delivery_requires_admin_confirmation_and_does_not_auto_open_intake(self):
        task = self.bridge.journal.update(self.task['id'], state='DONE', delivered_ranks=['01', '02', '03'],
                                         script_confirmed=True, title_confirmed=True,
                                         delivery={'share_url': 'https://pan.quark.cn/s/done'})
        self.assertFalse(self.bridge.journal.config().get('verified_at'))
        with self.assertRaisesRegex(ValueError, 'FEISHU_TASK_FORBIDDEN'):
            self.action('accept_delivery', revision=str(task['revision']))
        self.action('accept_delivery', operator='ou_admin', revision=str(task['revision']))
        self.assertTrue(self.bridge.journal.config()['verified_at'])
        self.assertFalse(self.bridge.journal.config()['accepting'])
        self.assertTrue(self.bridge.journal.task(task['id'])['delivery_accepted'])

    def test_cloud_notice_survives_success_and_is_displayed_verbatim(self):
        message = '官方提示原文\nhttps://pan.quark.cn'
        work = self.bridge.journal.root / 'cloud' / self.task['id']
        self.bridge.record_cloud_notice(message, work)
        self.bridge.record_cloud_notice(message, work)
        task = self.bridge.journal.update(self.task['id'], state='DONE', error='', delivered_ranks=['01', '02', '03'])
        self.assertEqual(task['cloud_notices'], [message])
        self.bridge.push_status(task, force=True)
        self.bridge.flush_notifications()
        payload = self.bridge.transport.sent[-1][1]
        self.assertIn(message, [r.get('text', {}).get('content') for r in payload['elements']])
        with self.assertRaisesRegex(ValueError, 'PATH_UNSAFE'):
            self.bridge.record_cloud_notice(message, self.root / self.task['id'])

    def test_completion_posts_actionable_delivery_at_bottom_without_duplicate_on_restart(self):
        self.bridge.push_status(self.task, force=True)
        self.bridge.flush_notifications()
        task = self.bridge.journal.update(self.task['id'], state='DONE', delivered_ranks=['01', '02', '03'],
                                         delivery={'share_url': 'https://pan.quark.cn/s/done', 'passcode': 'abcd'})
        self.bridge.push_status(task, force=True)
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 2)
        payload = json.dumps(self.bridge.transport.sent[-1][1], ensure_ascii=False)
        self.assertIn('https://pan.quark.cn/s/done', payload)
        self.assertIn('确认三条成片交付', payload)
        self.assertIn('ou_owner', payload)
        self.bridge.journal = Journal(self.store.home)
        self.bridge.push_status(self.bridge.journal.task(self.task['id']), force=True)
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 2)
        task = self.bridge.journal.update(self.task['id'], delivery_accepted=True)
        self.bridge.push_status(task, force=True)
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 2)
        self.assertIn('交付验收已确认', json.dumps(self.bridge.transport.updated[-1][1], ensure_ascii=False))

    def test_card_input_schema_respects_feishu_maximum(self):
        payloads = [cards.new_task('test', self.store.products()),
                    cards.edit('test', self.task, '01', 'script', 'revision')]
        def walk(value):
            if isinstance(value, dict):
                if value.get('tag') == 'input':
                    self.assertLessEqual(value['max_length'], 1000)
                    self.assertIn(value['input_type'], {'text', 'multiline_text'})
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        for payload in payloads: walk(payload)

    def recovery_fixture(self):
        self.store.update(self.id, submission_origin={'type': 'feishu', 'task_id': self.task['id']})
        self.queue.lock = threading.RLock()
        self.queue.active = {}
        self.queue.item_pending = set()

    def test_retry_keeps_completed_items_and_approved_copy(self):
        self.recovery_fixture(); self.scripts(); self.choices(); self.action('approve_titles')
        # Seed an already completed historical row; production PASS guards are
        # tested separately, this test verifies the retry leaves it untouched.
        with self.store.connect() as db:
            completed = self.store.get(self.id)['items'][0]
            completed.update(status='PASS', output='already-done.mp4')
            db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',
                       (json.dumps(completed), self.id, '01'))
        self.store.item(self.id, '02', status='FAILED', issues=['TTS_NETWORK_ERROR'])
        self.store.item(self.id, '03', status='FAILED', issues=['TTS_NETWORK_ERROR'])
        before = self.store.get(self.id)['items']; self.queue.batches.clear()
        self.assertTrue(recovery.resume(self.queue, self.id, self.task['id']))
        self.assertEqual([rank for rank, _ in self.queue.batches[0]], ['02', '03'])
        after = self.store.get(self.id)['items']
        self.assertEqual(after[0], before[0])
        self.assertEqual([x['copy'] for x in after], [x['copy'] for x in before])

    def test_restart_during_analysis_resumes_but_waiting_approval_does_not(self):
        self.recovery_fixture(); self.store.update(self.id, state='INTERRUPTED')
        self.queue.enqueue = MagicMock()
        self.assertFalse(recovery.resume(self.queue, self.id, self.task['id'], automatic=True))
        self.queue.enqueue.assert_not_called()
        self.store.item(self.id, '03', copy=None, final_script=None, localized_script=None, script_candidates=[])
        self.assertTrue(recovery.resume(self.queue, self.id, self.task['id'], automatic=True))
        self.queue.enqueue.assert_called_once_with(self.id)

    def test_restart_does_not_overwrite_manual_copy_or_resume_paused_job(self):
        self.recovery_fixture(); self.store.update(self.id, state='INTERRUPTED')
        self.store.item(self.id, '03', copy=None, final_script=None, localized_script=None, script_candidates=[])
        self.store.item(self.id, '01', script_draft={'id': 'manual'})
        self.queue.enqueue = MagicMock()
        with self.assertRaisesRegex(ValueError, 'FEISHU_TASK_SCRIPT_REVIEW_REQUIRED'):
            recovery.resume(self.queue, self.id, self.task['id'], automatic=True)
        self.store.update(self.id, state='PAUSED', control_request='PAUSE')
        self.assertFalse(recovery.resume(self.queue, self.id, self.task['id'], automatic=True))
        self.queue.enqueue.assert_not_called()

    def test_feishu_preparation_reaches_script_gate_without_downstream_plan_or_tts(self):
        from concurrent.futures import Future
        from contextlib import ExitStack, nullcontext
        from autocut.pipeline import Manager
        from test_review_flow import ANALYSIS
        self.recovery_fixture()
        sources = [dict(rank=n, filename=f'{n}.mp4', path=str(self.source),
                        sha256='fixture'+str(n), status='VALID', duration=23,
                        width=1080, height=1920) for n in (1, 2, 3)]
        manager = Manager(self.store, None, MagicMock())
        future = Future(); future.set_result({'text': 'source speech', 'language': 'en'})
        manager.judge = MagicMock(return_value=copy.deepcopy(ANALYSIS))
        manager.prepare_copies = lambda contexts, *_: {c['script_group_id']: {'response': copy.deepcopy(self.copy)} for c in contexts}
        manager.catalog = MagicMock(side_effect=AssertionError('TTS before human approval'))
        harness = MagicMock(); harness.profiler.span.side_effect = lambda *a: nullcontext()
        try:
            with ExitStack() as stack:
                for name, value in {'HOME': self.store.home, 'preserve_job_sources': lambda store, j, check: j,
                                    'scan': lambda *_: sources, 'source_sheet': lambda *_: str(self.root/'sheet.jpg'),
                                    'asr_job': lambda *a, **k: future}.items():
                    stack.enter_context(patch('autocut.pipeline.' + name, value))
                stack.enter_context(patch('autocut.pipeline.Harness')).return_value.__enter__.return_value = harness
                manager.run(self.id)
            job = self.store.get(self.id)
            self.assertEqual(job['state'], 'WAITING_SCRIPT_APPROVAL', job.get('issues'))
            self.assertTrue(self.bridge.batch.view(self.id)['all_scripts_ready'])
            self.assertEqual([call.args[2] for call in manager.judge.call_args_list], ['VIDEO_ANALYSIS'] * 3)
            self.assertTrue(all(item.get('decision') is None for item in job['items']))
            manager.catalog.assert_not_called()
        finally:
            manager.queue.shutdown(wait=True)


class JournalTests(unittest.TestCase):
    def test_same_submission_receipt_cannot_change_owner_or_payload(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root); form = journal.form('ou_owner', 'oc_group')
            data = dict(owner='ou_owner', chat='oc_group', expected_count=3)
            first, created = journal.create_task(form, data)
            repeated, created = journal.create_task(form, data)
            self.assertFalse(created); self.assertEqual(first['id'], repeated['id'])
            with self.assertRaisesRegex(ValueError, 'STALE_ACTION'):
                journal.create_task(form, dict(data, expected_count=4))

    def test_recovery_and_no_job_database(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            self.assertTrue(journal.record_event('one', {'kind': 'action'}))
            self.assertFalse(journal.record_event('one', {'kind': 'action'}))
            self.assertIsNotNone(journal.next_event())
            journal = Journal(root)
            self.assertIsNotNone(journal.next_event())
            self.assertFalse((Path(root) / 'autocut.sqlite3').exists())
            code = journal.bind_code(); journal.bind('oc_group', 'ou_admin', code)
            with self.assertRaises(ValueError): journal.bind('oc_other', 'ou_other', code)


class CloudTests(unittest.TestCase):
    def test_health_is_read_only_and_does_not_expose_account_data(self):
        cloud = QuarkCloud('fake', Path('/tmp'))
        with patch.object(cloud, 'run', return_value=({'code': 0, 'data': {'account': 'private'}, 'notice': {'text': '官方提示'}}, [])) as run:
            result = cloud.health()
        self.assertTrue(result['ready'])
        self.assertEqual(result['notice'], '官方提示')
        self.assertNotIn('private', str(result))
        run.assert_called_once_with(['get-user-info'], Path('/tmp/_health'), timeout=30, notify=False)

    def test_health_reports_official_runtime_error_without_retry(self):
        cloud = QuarkCloud('fake', Path('/tmp'))
        with patch.object(cloud, 'run', side_effect=CloudError(-104, '官方原文')) as run:
            result = cloud.health()
        self.assertFalse(result['ready'])
        self.assertEqual(result['error'], 'QUARK_-104')
        self.assertEqual(result['notice'], '官方原文')
        run.assert_called_once()

    def test_health_masks_process_errors_and_does_not_leak_command(self):
        import subprocess
        cloud = QuarkCloud('fake', Path('/tmp'))
        with patch.object(cloud, 'run', side_effect=subprocess.TimeoutExpired('private-command', 30)):
            result = cloud.health()
        self.assertEqual(result['error'], 'QUARK_HEALTH_FAILED')
        self.assertNotIn('private', str(result))

    def test_successful_cli_notice_is_forwarded_without_changing_result(self):
        import io
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cli = root / 'tool.cjs'; cli.touch()
            result = {'type': 'result', 'code': 0, 'data': {}, 'notice': {'text': '原样提示'}}
            process = MagicMock()
            process.stdout = io.StringIO(json.dumps(result) + '\n')
            process.poll.return_value = 0
            notices = []
            cloud = QuarkCloud(cli, root, notice=lambda text, work: notices.append((text, work)))
            cloud.node = str(cli)
            with patch('autocut.feishu_tasks.cloud.subprocess.Popen', return_value=process) as launch:
                actual, _ = cloud.run(['browse'], root)
            self.assertEqual(actual, result)
            self.assertEqual(notices, [('原样提示', root)])
            self.assertNotIn('env', launch.call_args.kwargs)
            self.assertEqual(launch.call_args.args[0][-2:], ['--session-id', cloud.session_id])

    def test_own_share_downloads_owned_files_without_recopying(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); calls = []; saved = {}
            def run(args, work, observe):
                calls.append(args[0])
                if args[0] == 'create-folder': return ({'data': {'fid': 'intake'}}, [])
                if args[0] == 'saveas': raise CloudError(41017)
                if args[0] == 'download':
                    (Path(args[args.index('--output-dir') + 1]) / 'one.mp4').write_bytes(b'video')
                    return ({'data': {}}, [])
                raise AssertionError(args)
            cloud = QuarkCloud('fake', root / 'work', runner=run)
            task = dict(id='CF-20261013-1234ABCD', share_url='https://pan.quark.cn/s/source', expected_count=1,
                        media={'source_files': [{'fid': 'owned', 'file_name': 'one.mp4', 'size': 5}]})
            info = {'streams': [{'codec_type': 'video', 'width': 1080, 'height': 1920}], 'format': {'duration': '5'}}
            with patch('app.s3.headless_media.probe', return_value=info):
                folder, media = cloud.intake(task, root / 'media', lambda value: saved.update(copy.deepcopy(value)))
            self.assertTrue(media['own_share'])
            self.assertEqual((folder / 'one.mp4').read_bytes(), b'video')
            self.assertNotIn('browse', calls)
            self.assertEqual(len(media['manifest']), 1)

    def test_validated_natural_order_survives_lexical_scan_and_restart(self):
        from autocut.feishu_tasks.source_order import ordered_sources
        scanned = [dict(rank=1, path='/media/Rank10.mp4', sha256='ten'), dict(rank=2, path='/media/Rank2.mp4', sha256='two')]
        ordered = ordered_sources(scanned, ['Rank2.mp4', 'Rank10.mp4'])
        self.assertEqual([(x['rank'], x['sha256']) for x in ordered], [(1, 'two'), (2, 'ten')])
        self.assertEqual(ordered_sources(ordered, ['Rank2.mp4', 'Rank10.mp4']), ordered)
        with self.assertRaisesRegex(ValueError, 'SOURCE_HASH_CHANGED'):
            ordered_sources(scanned, ['Rank2.mp4', 'Rank3.mp4'])

    def test_folder_share_password_is_carried_to_child_requests(self):
        calls = []
        def run(args, *unused):
            calls.append(args)
            files = [dict(fid='folder', filename='素材', file_type='0')] if args[args.index('--pdir-fid') + 1] == '0' else [dict(fid='video', filename='Rank1.mp4', file_type='1', size=10)]
            return ({'data': {'files': files}}, [])
        cloud = QuarkCloud('fake', Path('/tmp'), runner=run)
        files = cloud.share_files('https://pan.quark.cn/s/test123', 'abcd', '/tmp')
        self.assertEqual(len(files), 1)
        self.assertTrue(all('pwd=abcd' in args[args.index('--url') + 1] for args in calls))

    def test_real_cli_folder_and_filename_shapes(self):
        folder = QuarkCloud.normalize(dict(fid='directory', filename='素材', file_type='0', category=0, size=0, share_fid_token='private'))
        video = QuarkCloud.normalize(dict(fid='video', filename='Rank01.mp4', file_type='1', size=200, share_fid_token='private'))
        self.assertTrue(QuarkCloud.is_directory(folder)); self.assertFalse(QuarkCloud.is_directory(video))
        self.assertEqual(video['file_name'], 'Rank01.mp4'); self.assertNotIn('share_fid_token', video)

    def test_source_url_only_quark(self):
        for url in ['http://127.0.0.1:52857/', 'https://pan.quark.cn.evil/s/test', 'https://pan.quark.cn@evil/s/test', 'file:///etc/passwd']:
            with self.assertRaises(ValueError): share_url(url)
        self.assertEqual(sorted(['Rank10.mp4', 'Rank2.mp4', 'Rank1.mp4'], key=natural), ['Rank1.mp4', 'Rank2.mp4', 'Rank10.mp4'])

    def test_browse_consumes_full_artifact_not_five_item_preview(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); artifact = root / 'files.jsonl'
            rows = [dict(fid=str(i), file_name=str(i) + '.mp4', size=10) for i in range(10)]
            artifact.write_text('\n'.join(json.dumps(r) for r in rows))
            cloud = QuarkCloud('fake', root, runner=lambda *a: ({'data': {'file_path': str(artifact), 'file_list': rows[:5]}}, []))
            self.assertEqual(len(cloud.browse('folder', root)), 10)

    def test_upload_retry_reuses_successful_file(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); a, b = root / 'one.mp4', root / 'two.mp4'; a.write_bytes(b'one'); b.write_bytes(b'two')
            calls, saved, failing = [], {}, [True]
            def run(args, task_dir, observe):
                calls.append(args)
                if args[0] == 'create-folder': return ({'data': {'fid': 'output-dir'}}, [])
                if args[0] == 'upload':
                    if args[1] == str(b) and failing[0]: raise CloudError('FAILED')
                    observe({'type': 'list', 'code': 0, 'data': {'fileId': Path(args[1]).stem}})
                    return ({'data': {'fids': [Path(args[1]).stem]}}, [])
                if args[0] == 'share': return ({'data': {'share_url': 'https://pan.quark.cn/s/output123', 'passcode': 'abcd'}}, [])
                raise AssertionError(args)
            cloud = QuarkCloud('fake', root, runner=run)
            task = dict(id='CF-20261013-1234ABCD', delivery={})
            with self.assertRaises(CloudError): cloud.deliver(task, {'01': str(a), '02': str(b)}, lambda value: saved.update(copy.deepcopy(value)))
            failing[0] = False; task['delivery'] = saved
            result = cloud.deliver(task, {'01': str(a), '02': str(b)}, lambda value: saved.update(copy.deepcopy(value)))
            self.assertEqual(sum(c[0] == 'upload' and c[1] == str(a) for c in calls), 1)
            self.assertEqual(len(result['uploaded']), 2)
            self.assertEqual(result['passcode'], 'abcd')


if __name__ == '__main__': unittest.main()
