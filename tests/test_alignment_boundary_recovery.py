import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from autocut import context
from autocut.subtitle_alignment import request, VERSION
from app.s2.tts_text import prepare_tts_text
from app.s3.headless_runtime import sha


class AlignmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = 'Com essa ferramenta o trabalho fica mais fácil.'
        characters = list(prepare_tts_text(self.text))
        starts = [18.16 * i / (len(characters) - 1) for i in range(len(characters))]
        self.alignment = dict(characters=characters,
                              character_start_times_seconds=starts,
                              character_end_times_seconds=starts[1:] + [18.48])
        self.alignment_path = self.root / 'alignment.json'
        self.alignment_path.write_text(json.dumps(dict(alignment=self.alignment)))
        self.audio = self.root / 'paid.mp3'
        self.audio.write_bytes(b'original paid audio')
        source = self.root / 'source.mp4'
        source.write_bytes(b'original source')
        self.item = dict(rank='06', status='FAILED', issues=['JOB_REVIEW_REQUIRED'],
                         source_snapshot=dict(path=str(source), sha256=sha(source)),
                         audio_result=dict(path=str(self.audio), sha256=sha(self.audio),
                           fingerprint='paid-request', alignment_path=str(self.alignment_path),
                           duration=18.4, script_text=self.text),
                         final_script=self.text, final_script_language='pt-BR',
                         final_title=dict(text='Mais fácil no trabalho', language='pt-BR'),
                         script_status='USER_APPROVED', title_status='USER_APPROVED',
                         content_confirmed=True, analysis=dict(context='same tool'),
                         decision=dict(selected_title_id=1), copy=dict(titles=[dict(id=1)]))
        self.job = dict(id='job', market='Brazil', state='PARTIAL_COMPLETED',
                        batch_review=dict(phase='RUNNING'), items=[self.item])
        report = self.root / 'jobs/job/work/RUN_REPORT.jsonl'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps(dict(rank='06', event='ITEM_RECOVERY_ERROR',
                           error='ALIGNMENT_NOT_READY：时间不单调或越界')) + '\n')
        self.manager = MagicMock()
        self.manager.lock = threading.RLock()
        self.manager.item_pending = set()
        self.manager.control.busy.return_value = False
        self.manager.store.home = self.root
        self.manager.store.lock = threading.RLock()
        self.manager.store.get.return_value = self.job
        self.cp = dict(TTS_DONE=dict(input_sha='paid-request',
                                   data=dict(sha256=sha(self.audio))))
        self.patch_cp = patch('autocut.ownership.checkpoints', return_value=self.cp)
        self.patch_cp.start()
        self.addCleanup(self.patch_cp.stop)

    def test_targeted_request_preserves_audio_alignment_and_approved_copy(self):
        before = copy.deepcopy(self.item)
        alignment_before = self.alignment_path.read_bytes()
        request(self.manager, 'job', '06')
        self.manager.enqueue_batch.assert_called_once_with('job', [('06', 'SCRIPT')])
        self.assertEqual(self.item, before)
        self.assertEqual(self.alignment_path.read_bytes(), alignment_before)
        self.assertEqual(sha(self.audio), before['audio_result']['sha256'])

    def test_changed_audio_is_rejected_without_queue(self):
        self.audio.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'RECOVERY_CACHE_INVALID'):
            request(self.manager, 'job', '06')
        self.manager.enqueue_batch.assert_not_called()

    def test_approval_is_still_required(self):
        self.item['content_confirmed'] = False
        with self.assertRaisesRegex(ValueError, 'SCRIPT_CONFIRMATION_REQUIRED'):
            request(self.manager, 'job', '06')
        self.manager.store.item.assert_not_called()

    def test_real_overshoot_is_rejected(self):
        self.alignment['character_end_times_seconds'][-1] = 18.49
        self.alignment_path.write_text(json.dumps(dict(alignment=self.alignment)))
        with self.assertRaises(ValueError):
            request(self.manager, 'job', '06')
        self.manager.enqueue_batch.assert_not_called()

    def test_wrong_failure_and_existing_output_are_rejected(self):
        self.item['issues'] = ['AI_DRIVER_TIMEOUT']
        with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
            request(self.manager, 'job', '06')
        self.item['issues'] = ['JOB_REVIEW_REQUIRED']
        self.item['output'] = 'existing.mp4'
        with self.assertRaisesRegex(ValueError, 'RECOVERY_CACHE_INVALID'):
            request(self.manager, 'job', '06')
        self.manager.enqueue_batch.assert_not_called()

    def test_pending_and_completed_items_do_not_enqueue_again(self):
        self.manager.item_pending.add(('job', '06'))
        request(self.manager, 'job', '06')
        self.manager.item_pending.clear()
        self.item['status'] = 'PASS'
        request(self.manager, 'job', '06')
        self.manager.enqueue_batch.assert_not_called()

    def test_same_receipt_is_not_retried_again_after_failure(self):
        request(self.manager, 'job', '06')
        call = self.manager.store.done.call_args
        self.cp[VERSION] = dict(input_sha=call.args[3])
        self.manager.enqueue_batch.reset_mock()
        with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
            request(self.manager, 'job', '06')
        self.manager.enqueue_batch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
