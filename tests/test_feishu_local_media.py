"""Existing Feishu tasks resume verified local originals with normal barriers."""
import copy
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from test_feishu_tasks import BridgeTests
from autocut.feishu_tasks.local_media import source_snapshot, intake, VERSION


INFO = {'streams': [{'codec_type': 'video', 'width': 1080, 'height': 1920}],
        'format': {'duration': '12'}}


class LocalMediaTests(unittest.TestCase):
    tearDown = BridgeTests.tearDown
    raw = BridgeTests.raw

    def setUp(self):
        BridgeTests.setUp(self)
        self.folder = self.root / 'originals'
        self.folder.mkdir()
        self.source_files = []
        for n in range(1, 4):
            p = self.folder / ('Rank' + str(n) + '.mp4')
            p.write_bytes(b'original source ' + str(n).encode())
            self.source_files.append(dict(fid='original-' + str(n), file_name=p.name, size=p.stat().st_size))
        self.task = self.bridge.journal.update(self.task['id'], job_id='', state='MEDIA_ERROR',
            media=dict(source_files=self.source_files, saved=True, cloud_folder='kept-folder'))
        self.bridge.pool = MagicMock()
        self.bridge.cloud.intake = MagicMock(side_effect=AssertionError('no cloud download'))
        self.path = '/integrations/feishu-tasks/' + self.task['id'] + '/local-media'
        self.body = dict(path=str(self.folder), revision=self.task['revision'],
                         source_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.iterdir()})

    def grant(self):
        r = self.c.post('/native/grant', json=dict(path=str(self.folder)))
        self.assertEqual(r.status_code, 200)

    def register(self):
        self.grant()
        r = self.c.post(self.path, json=self.body)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def current(self):
        return self.bridge.journal.task(self.task['id'])

    def test_registration_requires_authenticated_selected_folder(self):
        before = self.current()
        self.assertEqual(self.c.post(self.path, json=self.body).status_code, 403)
        self.grant()
        self.assertEqual(self.c.post(self.path, json=self.body, headers={'Authorization': ''}).status_code, 401)
        self.assertEqual(self.current(), before)
        self.bridge.pool.submit.assert_not_called()

    def test_registration_is_durable_idempotent_and_keeps_original_binding(self):
        before = self.current()
        self.register()
        task = self.current()
        for key in ['id', 'request_id', 'owner', 'chat', 'expected_count', 'share_url']:
            self.assertEqual(task[key], before[key])
        self.assertEqual(task['state'], 'RECEIVED')
        self.assertEqual(task['local_media']['version'], VERSION)
        self.assertEqual(len(task['local_media']['files']), 3)
        self.assertEqual(self.bridge.pool.submit.call_count, 1)
        repeated = self.c.post(self.path, json=self.body)
        self.assertTrue(repeated.json()['replayed'])
        self.assertEqual(self.current(), task)
        self.assertEqual(self.bridge.pool.submit.call_count, 1)
        self.assertEqual(self.queue.enqueued, [])

    def test_stale_task_revision_cannot_attach_local_sources(self):
        self.grant()
        self.body['revision'] += 1
        before = self.current()
        self.assertEqual(self.c.post(self.path, json=self.body).status_code, 409)
        self.assertEqual(self.current(), before)
        self.bridge.pool.submit.assert_not_called()

    def test_bound_or_completed_task_cannot_be_replaced(self):
        self.grant()
        for updates in [dict(job_id=self.id), dict(state='DONE'), dict(state='STOPPED')]:
            with self.subTest(updates=updates):
                self.bridge.journal.update(self.task['id'], **updates)
                before = self.current()
                self.assertEqual(self.c.post(self.path, json=self.body).status_code, 409)
                self.assertEqual(self.current(), before)
                self.bridge.journal.update(self.task['id'], job_id='', state='MEDIA_ERROR')

    def test_other_group_task_cannot_be_attached(self):
        self.grant()
        self.bridge.journal.update(self.task['id'], chat='oc_other')
        before = self.current()
        self.assertEqual(self.c.post(self.path, json=self.body).status_code, 400)
        self.assertEqual(self.current(), before)

    def test_existing_history_manual_control_and_wrong_origin_are_not_resumed(self):
        self.grant()
        for change in [dict(state='COMPLETED'), dict(state='PAUSED'), dict(deleted_at=1),
                       dict(control_request={'action': 'STOP'}), dict(submission_origin={'task_id': 'other'})]:
            with self.subTest(change=change), patch.object(self.store, 'by_request', return_value=dict(
                    dict(state='QUEUED', submission_origin={'type': 'feishu', 'task_id': self.task['id']}), **change)):
                before = self.current()
                self.assertEqual(self.c.post(self.path, json=self.body).status_code, 409)
                self.assertEqual(self.current(), before)
        self.bridge.pool.submit.assert_not_called()

    def test_missing_or_extra_source_never_partially_changes_task(self):
        self.grant()
        p = self.folder / 'Rank3.mp4'
        p.rename(self.folder / 'wrong.mp4')
        before = self.current()
        self.assertEqual(self.c.post(self.path, json=self.body).status_code, 400)
        self.assertEqual(self.current(), before)
        self.assertFalse(self.bridge.media_root.exists())

    def test_size_mismatch_and_cached_hash_mismatch_are_rejected(self):
        self.grant()
        p = self.folder / 'Rank1.mp4'
        original = p.read_bytes()
        for data in [original + b'extra', b'x' * len(original)]:
            with self.subTest(length=len(data)):
                self.bridge.journal.update(self.task['id'], media=dict(source_files=self.source_files,
                    downloads={p.name: dict(sha256=hashlib.sha256(original).hexdigest())}))
                p.write_bytes(data)
                before = self.current()
                self.assertEqual(self.c.post(self.path, json=self.body).status_code, 400)
                self.assertEqual(self.current(), before)

    def test_symlink_source_is_rejected(self):
        self.grant()
        p = self.folder / 'Rank1.mp4'
        p.rename(self.root / 'elsewhere.mp4')
        p.symlink_to(self.root / 'elsewhere.mp4')
        before = self.current()
        self.assertEqual(self.c.post(self.path, json=self.body).status_code, 400)
        self.assertEqual(self.current(), before)

    def test_complete_source_selection_is_rechecked_before_any_copy(self):
        self.register()
        (self.folder / 'Rank3.mp4').write_bytes(b'changed bytes!!!')
        with patch('app.s3.headless_media.probe', return_value=INFO), patch.object(
                self.bridge.submission.feishu, 'matches', return_value=[]), patch.object(
                self.bridge.submission.feishu, 'binding', return_value={}):
            self.bridge.intake(self.task['id'])
        self.assertEqual(self.current()['state'], 'MEDIA_ERROR')
        self.assertFalse(self.current().get('job_id'))
        self.assertEqual(list((self.bridge.media_root / self.task['id']).iterdir()), [])
        self.bridge.cloud.intake.assert_not_called()

    def test_valid_copy_is_reused_and_conflicting_copy_is_never_overwritten(self):
        self.register()
        task = self.current()
        folder = self.bridge.media_root / self.task['id']
        folder.mkdir(parents=True)
        (folder / 'Rank1.mp4').write_bytes(b'different source')
        with patch('app.s3.headless_media.probe', return_value=INFO):
            with self.assertRaisesRegex(ValueError, 'STORAGE_PATH_CONFLICT'):
                intake(task, self.bridge.media_root, lambda value: None)
        self.assertEqual((folder / 'Rank1.mp4').read_bytes(), b'different source')
        (folder / 'Rank1.mp4').write_bytes((self.folder / 'Rank1.mp4').read_bytes())
        import shutil
        with patch('app.s3.headless_media.probe', return_value=INFO), patch(
                'autocut.feishu_tasks.local_media.shutil.copy2', wraps=shutil.copy2) as copied:
            _, media = intake(task, self.bridge.media_root, lambda value: None)
        self.assertEqual(copied.call_count, 2)
        self.assertEqual(len(media['manifest']), 3)
        self.assertTrue(all((self.folder / row['name']).is_file() for row in media['manifest']))

    def test_original_feishu_intake_creates_one_normal_job_with_real_confirmation_gates(self):
        before_jobs = len(self.store.jobs())
        before = self.store.get(self.id)
        self.register()
        with patch('app.s3.headless_media.probe', return_value=INFO), patch.object(
                self.bridge.submission.feishu, 'matches', return_value=[]), patch.object(
                self.bridge.submission.feishu, 'binding', return_value={}):
            self.bridge.intake(self.task['id'])
        task = self.current()
        self.assertEqual(task['state'], 'PROCESSING', task['error'])
        job = self.store.get(task['job_id'])
        self.assertEqual(job['submission_origin'], dict(type='feishu', task_id=self.task['id']))
        self.assertEqual(job['request_id'], self.task['request_id'])
        self.assertTrue(job['content_confirmation_required'])
        self.assertTrue(job['batch_review_required'])
        self.assertFalse(job.get('scripts_approved'))
        self.assertFalse(job.get('titles_approved'))
        self.assertEqual(len(task['media']['manifest']), 3)
        self.assertEqual(len(self.store.jobs()), before_jobs + 1)
        self.assertEqual(self.store.get(self.id), before)
        from autocut.feishu_tasks import cards
        self.assertIn('无需另建网页任务', json.dumps(cards.status(task, '分析中'), ensure_ascii=False))
        self.bridge.intake(self.task['id'])
        self.assertEqual(len(self.store.jobs()), before_jobs + 1)
        self.bridge.cloud.intake.assert_not_called()

    def test_local_receipt_survives_restart_and_source_hash_remains_frozen(self):
        self.register()
        from autocut.feishu_tasks.storage import Journal
        persisted = Journal(self.store.home).task(self.task['id'])
        self.assertEqual(persisted['local_media'], self.current()['local_media'])
        with patch('app.s3.headless_media.probe', return_value=INFO):
            _, media = intake(persisted, self.bridge.media_root, lambda value: None)
        self.assertEqual([x['sha256'] for x in media['manifest']],
                         [x['sha256'] for x in persisted['local_media']['files']])

    def test_client_hashes_cannot_be_omitted_or_changed_on_replay(self):
        self.register()
        before = self.current()
        body = copy.deepcopy(self.body)
        body['source_hashes']['Rank1.mp4'] = '0' * 64
        self.assertEqual(self.c.post(self.path, json=body).status_code, 409)
        self.assertEqual(self.current(), before)
        body.pop('source_hashes')
        self.assertEqual(self.c.post(self.path, json=body).status_code, 422)

    def test_service_stop_prevents_local_intake_from_submitting_a_job(self):
        self.register()
        before = len(self.store.jobs())
        self.bridge.stop_event.set()
        self.bridge.intake(self.task['id'])
        self.assertEqual(len(self.store.jobs()), before)
        self.assertFalse(self.current().get('job_id'))
        self.assertFalse(self.bridge.media_root.exists())

    def test_missing_local_folder_leaves_task_unchanged(self):
        before = self.current()
        self.body['path'] = str(self.root / 'gone')
        self.assertEqual(self.c.post(self.path, json=self.body).status_code, 400)
        self.assertEqual(self.current(), before)
