"""Deterministic deadlines and real queue ordering; no external sends/providers."""
import copy
import json
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from test_feishu_tasks import BridgeTests
from autocut.confirmation_wait import ConfirmationWait, VERSION
from autocut.pipeline import Manager


class ConfirmationWaitTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    raw = BridgeTests.raw
    state = BridgeTests.state
    post = BridgeTests.post
    action = BridgeTests.action

    def policy(self):
        service = self.app.state.confirmation_wait
        self.now = self.store.get(self.id)['updated']
        service.clock = lambda: self.now
        return service

    def approve(self, phase):
        view = self.app.state.batch.view(self.id)
        data = dict(action_token=view['action_token'], request_id='fixture-' + phase + '-' + view['action_token'][:12])
        if phase == 'titles':
            data['selections'] = {r['rank']: 1 for r in view['items'] if r['in_scope']}
        response = self.c.post('/jobs/' + self.id + '/batch/approve-' + phase, json=data)
        self.assertEqual(response.status_code, 200, response.text)
        return data

    def test_deadlines_one_owner_mention_restart_and_no_production_mutation(self):
        service = self.policy()
        before = self.store.get(self.id)
        service.tick()
        self.now += 599
        service.tick()
        self.bridge.flush_notifications()
        self.assertEqual(self.bridge.transport.sent, [])
        self.now += 1
        service.tick()
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)
        sent = self.bridge.transport.sent[0]
        self.assertEqual(sent[0], 'oc_group')
        self.assertIn('ou_owner', json.dumps(sent[1]))
        self.now += 599
        service.tick()
        self.assertFalse(service.data['jobs'][self.id]['suspended'])
        self.now += 1
        service.tick()
        self.assertEqual(service.view(self.store.get(self.id))['status'], 'SUSPENDED')
        restarted = ConfirmationWait(self.store, self.queue, self.bridge, clock=lambda: self.now)
        self.queue.confirmation_wait = restarted
        for _ in range(3):
            restarted.tick()
            self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)
        self.assertEqual(self.store.get(self.id), before)
        self.assertEqual(self.queue.enqueued, [])
        self.assertNotIn('action_token', restarted.path.read_text())

    def test_stale_queued_reminder_cancelled_after_real_confirmation(self):
        service = self.policy()
        service.tick()
        self.now += 600
        service.tick()
        self.approve('scripts')
        self.bridge.flush_notifications()
        self.assertEqual(self.bridge.transport.sent, [])
        service.tick()
        row = service.data['jobs'][self.id]
        self.assertEqual(row['phase'], 'TITLES')
        self.assertEqual(row['waiting_since'], self.now)
        self.assertFalse(row['reminder_enqueued'])
        self.now += 600
        service.tick()
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)
        self.assertIn('标题', json.dumps(self.bridge.transport.sent[0][1], ensure_ascii=False))

    def test_web_job_stays_local_but_still_suspends_and_keeps_approval(self):
        service = self.policy()
        self.bridge.journal.update(self.task['id'], job_id='unrelated-old-job')
        tasks_before = self.bridge.journal.tasks()
        service.tick()
        self.now += 600
        service.tick()
        self.bridge.flush_notifications()
        self.assertEqual(self.bridge.transport.sent, [])
        self.assertEqual(self.bridge.journal.pending_notifications(), [])
        self.assertFalse(service.data['jobs'][self.id]['reminder_enqueued'])
        self.assertEqual(service.view(self.store.get(self.id))['reminder_suppressed_reason'], 'LOCAL_JOB_NO_FEISHU')
        self.now += 600
        service.tick()
        self.assertEqual(service.view(self.store.get(self.id))['status'], 'SUSPENDED')
        self.assertIn('APPROVE_SCRIPTS', self.app.state.batch.view(self.id)['allowed_actions'])
        self.assertEqual(self.bridge.journal.tasks(), tasks_before)

    def test_pending_local_reminder_is_skipped_before_transport(self):
        service = self.policy();service.tick();self.now += 600;service.tick()
        key = self.bridge.journal.pending_notifications()[0]['k']
        self.bridge.journal.update(self.task['id'], job_id='unrelated-old-job')
        self.assertFalse(service.can_send(key))
        self.bridge.flush_notifications()
        self.assertEqual(self.bridge.transport.sent, [])
        with self.bridge.journal.connect() as db:
            row=db.execute('SELECT state,error,attempts FROM outbox WHERE k=?',(key,)).fetchone()
        self.assertEqual(tuple(row), ('SKIPPED','LOCAL_JOB_NO_FEISHU',0))

    def test_paused_stopped_deleted_completed_never_remind_or_resume(self):
        service = self.policy()
        for state, fields in [('PAUSED', {'control_request': 'PAUSE'}),
                              ('STOPPED', {'control_request': 'STOP'}),
                              ('COMPLETED', {'control_request': None})]:
            self.store.update(self.id, state=state, **fields)
            service.tick()
            self.now += 2000
            service.tick()
            self.bridge.flush_notifications()
            self.assertEqual(service.view(self.store.get(self.id))['status'], 'INACTIVE')
        self.store.update(self.id, state='WAITING_SCRIPT_APPROVAL', control_request=None)
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET deleted_at=? WHERE id=?', (self.now, self.id))
        service.tick()
        self.assertEqual(self.bridge.transport.sent, [])
        self.assertEqual(self.queue.enqueued, [])

    def test_analysis_and_inflight_editor_do_not_start_timer(self):
        service = self.policy()
        self.store.update(self.id, state='RUNNING')
        self.now += 2000
        service.tick()
        self.assertNotIn(self.id, service.data['jobs'])
        self.store.update(self.id, state='WAITING_SCRIPT_APPROVAL')
        self.store.item(self.id, 1, operation_inflight=True)
        service.tick()
        self.assertNotIn(self.id, service.data['jobs'])
        self.assertEqual(self.bridge.transport.sent, [])

    def test_no_recipient_does_not_mark_reminder_delivered_or_skip_suspend(self):
        service = self.policy()
        self.bridge.journal.configure(enabled=False)
        service.tick()
        self.now += 1200
        service.tick()
        row = service.data['jobs'][self.id]
        self.assertFalse(row['reminder_enqueued'])
        self.assertTrue(row['suspended'])
        self.bridge.journal.configure(enabled=True)
        service.tick()
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)

    def test_existing_explicit_manual_reminder_is_reused(self):
        service = self.policy()
        (self.store.home / 'runtime/manual-review-reminder-20261016T103300.json').write_text(json.dumps(
            {'targets': [dict(job_id=self.id, phase='SCRIPTS', status='MENTION_REMINDER_SENT', finished_at=self.now)]}))
        service.tick()
        self.now += 1200
        service.tick()
        self.bridge.flush_notifications()
        self.assertTrue(service.data['jobs'][self.id]['suspended'])
        self.assertEqual(self.bridge.transport.sent, [])

    def test_suspended_approval_still_requires_real_owner_and_fresh_selection(self):
        service = self.policy()
        service.tick()
        self.now += 1200
        service.tick()
        before = copy.deepcopy(self.store.get(self.id))
        with self.assertRaisesRegex(ValueError, 'FEISHU_TASK_FORBIDDEN'):
            self.action('approve_scripts', operator='ou_other')
        self.assertEqual(self.store.get(self.id), before)
        with self.assertRaisesRegex(ValueError, 'STALE_ACTION'):
            self.action('approve_scripts', revision='old-revision')
        self.assertEqual(self.store.get(self.id), before)
        self.action('approve_scripts')
        self.assertEqual(self.app.state.batch.view(self.id)['phase'], 'TITLES')
        self.assertEqual(self.queue.enqueued, [])

    def test_confirmed_work_waits_for_current_product_without_using_spare_slot(self):
        service = self.policy()
        manager = Manager(self.store, None, None)
        manager.confirmation_wait = service
        service.manager = manager
        self.app.state.batch.manager = manager
        self.bridge.submission.manager = manager
        release, started, target = threading.Event(), threading.Event(), threading.Event()
        blocker = self.store.create(dict(request_id='blocker', product_name='前序任务', market='Brazil', mode='Transport'))[0]['id']

        def run(ident):
            self.store.update(ident, state='RUNNING')
            started.set()
            release.wait(5)
            self.store.update(ident, state='WAITING_SCRIPT_APPROVAL')

        def item_work(*args):
            target.set()
            self.store.item(self.id, args[2], status='WAITING_SUPPLEMENT_MEDIA')

        manager.run = run
        try:
            service.tick()
            self.now += 1200
            service.tick()
            manager.enqueue(blocker)
            self.assertTrue(started.wait(2))
            with patch('autocut.item_pipeline.resume_item', side_effect=item_work):
                self.approve('scripts')
                self.approve('titles')
                service.tick()
                self.assertFalse(target.wait(.05))
                self.assertNotIn(self.id, manager.active)
                self.assertFalse(any(j == self.id for j, _ in manager.item_pending))
                row = service.data['jobs'][self.id]
                self.assertEqual(row['resume_after'], [blocker])
                self.assertEqual(len(row['pending']), 1)
                restarted = ConfirmationWait(self.store, manager, self.bridge, clock=lambda: self.now)
                manager.confirmation_wait = restarted
                restarted.tick()
                self.assertFalse(target.is_set())
                release.set()
                manager.active[blocker].result(2)
                restarted.tick()
                self.assertTrue(target.wait(2))
                manager.active[self.id].result(2)
                self.assertEqual(restarted.data['jobs'][self.id]['pending'], [])
        finally:
            release.set()
            manager.queue.shutdown(wait=True)

    def test_readonly_api_exposes_policy_and_keeps_approval_buttons_after_suspend(self):
        service = self.policy()
        service.tick()
        self.now += 1200
        service.tick()
        result = self.c.get('/jobs').json()[0]
        self.assertEqual(result['confirmation_wait']['status'], 'SUSPENDED')
        self.assertIn('APPROVE_SCRIPTS', result['batch_review_view']['allowed_actions'])
        status = self.c.get('/runtime/confirmation-wait').json()
        self.assertEqual(status['policy']['version'], VERSION)
        self.assertEqual(status['policy']['remind_after_seconds'], 600)
        self.assertEqual(status['policy']['suspend_after_seconds'], 1200)

    def test_unconfirmed_retry_remains_suspended_without_a_worker(self):
        service = self.policy()
        service.tick()
        self.now += 1200
        service.tick()
        self.assertTrue(service.defer(self.id, dict(kind='job')))
        for _ in range(2):
            service.tick()
        self.assertTrue(service.data['jobs'][self.id]['suspended'])
        self.assertEqual(self.queue.enqueued, [])

    def test_editing_does_not_reset_deadline_cycle_or_repeat_reminder(self):
        service = self.policy()
        service.tick()
        original = copy.deepcopy(service.data['jobs'][self.id])
        self.now += 600
        service.tick()
        self.bridge.flush_notifications()
        self.store.item(self.id, 1, operation_inflight=True)
        self.now += 601
        service.tick()
        self.store.item(self.id, 1, operation_inflight=False)
        service.tick()
        row = service.data['jobs'][self.id]
        self.assertEqual(row['waiting_since'], original['waiting_since'])
        self.assertEqual(row['cycle'], original['cycle'])
        self.assertTrue(row['suspended'])
        self.bridge.flush_notifications()
        self.assertEqual(len(self.bridge.transport.sent), 1)


if __name__ == '__main__':
    unittest.main()
