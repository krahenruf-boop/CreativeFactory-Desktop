"""Completed history survives exported files moving; production still validates."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from app.s3.headless_runtime import sha, write


class CompletionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'data')
        job, _ = self.store.create(dict(request_id='completion-history',
            input_path=str(self.root), product_name='历史完成状态',
            product_profile_id='general', market='Brazil', mode='Transport'))
        self.id = job['id']
        self.output = self.root/'final.mp4'
        shutil.copyfile('build/review019/smoke/music_supplement_smoke.mp4', self.output)
        plan = self.root/'plan.json'
        write(plan, dict(output=str(self.output), voiceover_mode='MUSIC_ONLY',
                         visual={'subtitle': {'captions': []}}))
        self.store.item(self.id, 1, status='CHECKING', output=str(self.output),
            plan=str(plan), voiceover_mode='MUSIC_ONLY', script_status='USER_APPROVED',
            title_status='USER_APPROVED', content_confirmed=True,
            final_title=dict(text='Olha esta bolsa!', language='pt-BR', text_zh='看看这个包！'))
        self.store.done(self.id, 'FINAL_RENDERED', 1, 'render-one',
                        dict(output=str(self.output), output_sha256=sha(self.output)))
        self.store.done(self.id, 'QA_DONE', 1, 'render-one', dict(status='WARNING'))
        # Real file and ffprobe validation at the production transition.
        self.store.item(self.id, 1, status='PASS')
        self.store.update(self.id, state='COMPLETED', source_count=1,
            batch_review=dict(phase='RUNNING', ranks=['01'], receipts={}))
        self.manager = MagicMock()
        self.manager.control.view.return_value = dict(label=None)
        self.client = TestClient(create_app(self.store, MagicMock(), MagicMock(),
            self.manager, 'fixture', recover_on_startup=False),
            base_url='http://127.0.0.1:9999', headers={'Authorization': 'Bearer fixture'})
        self.addCleanup(self.client.close)

    def assert_completed(self, job):
        self.assertEqual(job['ui_status'], '已完成')
        self.assertEqual(job['batch_review_view']['label'], '已完成')
        self.assertEqual(job['ownership_counts']['completed'], 1)
        self.assertEqual(job['items'][0]['ui_status'], '已完成')
        self.assertEqual(job['items'][0]['ui_progress'], 100)

    def test_moved_and_deleted_exports_keep_history_without_probing(self):
        before = self.store.get(self.id)
        moved = self.root/'moved.mp4'
        self.output.rename(moved)
        with patch('autocut.ownership.final_ready', side_effect=AssertionError('must not revalidate history')), \
             patch('autocut.ownership.output_stat', side_effect=AssertionError('must not track exports')):
            self.assert_completed(self.client.get('/jobs/'+self.id).json())
            moved.unlink()  # Isolated fixture only.
            self.assert_completed(self.client.get('/jobs').json()[0])
            restarted = Store(self.store.home)
            self.assertEqual(restarted.get(self.id), before)
            self.assert_completed(self.client.get('/jobs/'+self.id).json())
        self.assertEqual(self.store.get(self.id), before)
        self.manager.enqueue.assert_not_called()
        self.manager.enqueue_item.assert_not_called()

    def test_missing_file_blocks_new_completion_but_not_recorded_history(self):
        self.store.item(self.id, 1, status='CHECKING')
        self.output.unlink()
        with self.assertRaisesRegex(ValueError, 'FINAL_NOT_READY'):
            self.store.item(self.id, 1, status='PASS')
        self.assertEqual(self.store.get(self.id)['items'][0]['status'], 'CHECKING')

    def test_open_missing_export_explains_without_changing_completion(self):
        before = self.store.get(self.id)
        self.output.unlink()
        for route in [f'/jobs/{self.id}/outputs/open', f'/outputs/{self.id}/01/reveal']:
            result = self.client.post(route)
            self.assertEqual(result.json()['error'], 'OUTPUT_NOT_FOUND')
        self.assertEqual(self.client.get(f'/outputs/{self.id}/01/video').status_code, 404)
        self.assert_completed(self.client.get('/jobs/'+self.id).json())
        self.assertEqual(self.store.get(self.id), before)

    def test_unverified_pass_cannot_make_header_disagree_with_items(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM checkpoints WHERE job_id=? AND stage='QA_DONE'", (self.id,))
        job = self.client.get('/jobs/'+self.id).json()
        self.assertEqual(job['ui_status'], '失败')
        self.assertEqual(job['batch_review_view']['label'], job['ui_status'])
        self.assertEqual(job['ownership_counts']['completed'], 0)
        self.assertNotEqual(job['items'][0]['ui_status'], '已完成')


if __name__ == '__main__':
    unittest.main()
