import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from autocut import context
from autocut.title_recovery import request, validate_title
from app.s3.headless_runtime import sha


class TitleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = Path(self.temp.name) / 'paid.mp3'
        self.audio.write_bytes(b'existing voice')
        self.item = dict(rank='02', status='FAILED', issues=['TITLE_SHORTEN_REQUIRED'],
                         script_status='USER_APPROVED', title_status='USER_APPROVED',
                         content_confirmed=True, final_title={'text': 'unchanged title'},
                         final_script={'text': 'unchanged script'},
                         audio_result=dict(path=str(self.audio), sha256=sha(self.audio), fingerprint='paid'))
        self.completed = dict(rank='01', status='PASS', output='untouched.mp4')
        self.job = dict(id='job', state='PARTIAL_COMPLETED',
                        batch_review=dict(phase='RUNNING', ranks=['01', '02'], receipts={}),
                        items=[self.completed, self.item])
        self.manager = MagicMock()
        self.manager.lock = threading.RLock()
        self.manager.item_pending = set()
        self.store = self.manager.store
        self.store.lock = threading.RLock()
        self.store.get.return_value = self.job
        self.store.item.side_effect = lambda ident, rank, **fields: self.item.update(fields)
        self.store.update.side_effect = lambda ident, **fields: self.job.update(fields)

    def test_reopens_only_failed_title_without_selecting_or_changing_copy_audio(self):
        before = copy.deepcopy(self.item)
        completed = copy.deepcopy(self.completed)
        with patch('autocut.title_recovery.audit'):
            request(self.manager, 'job', '02')
        self.assertEqual(self.job['batch_review']['ranks'], ['02'])
        self.assertEqual(self.item['title_status'], 'WAITING_APPROVAL')
        for key in ['final_title', 'final_script', 'audio_result']:
            self.assertEqual(before[key], self.item[key])
        self.assertEqual(self.completed, completed)
        self.manager.enqueue_batch.assert_not_called()
        self.manager.enqueue_item.assert_not_called()
        self.store.done.assert_called_once()
        request(self.manager, 'job', '02')
        self.store.done.assert_called_once()

    def test_running_job_and_changed_paid_audio_are_blocked(self):
        self.job['state'] = 'RUNNING'
        with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
            request(self.manager, 'job', '02')
        self.job['state'] = 'PARTIAL_COMPLETED'
        self.audio.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'RECOVERY_CACHE_INVALID'):
            request(self.manager, 'job', '02')
        self.store.item.assert_not_called()

    def test_actual_rejected_title_cannot_be_reconfirmed_without_shortening(self):
        text = 'Aposto que 99% das pessoas não sabe como tapar um buraco no drywall sem chamar ninguém.'
        with self.assertRaisesRegex(ValueError, 'TITLE_SHORTEN_REQUIRED'):
            validate_title(text)
        validate_title('Como tapar um buraco no drywall sem chamar ninguém')
        from autocut.feishu_tasks.bridge import human
        self.assertIn('缩短标题', human(ValueError('TITLE_SHORTEN_REQUIRED')))


if __name__ == '__main__':
    unittest.main()
