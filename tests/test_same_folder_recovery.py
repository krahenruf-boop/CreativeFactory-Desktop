"""Targeted duration recovery must preserve audio and respect the gap limit."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import autocut.context
from autocut.auto_supplement import recover
from app.s3.headless_runtime import sha


class SameFolderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source, audio, donor = [self.root/name for name in ('source.mp4','audio.mp3','donor.mp4')]
        for path in (source, audio, donor):
            path.write_bytes(path.name.encode())
        spoken = 'Este kit deixa seu carro muito mais bonito.'
        self.item = dict(rank='03', status='WAITING_SUPPLEMENT_MEDIA', issues=['DURATION_FIT_FAILED'],
            script_status='USER_APPROVED', title_status='USER_APPROVED', content_confirmed=True,
            final_script=dict(text=spoken, language='pt-BR'),
            final_title=dict(text='Seu carro muito mais bonito', language='pt-BR'),
            analysis={'product_description':'car ornament'}, decision={'checked':True},
            source_snapshot=dict(path=str(source), sha256=sha(source), rank=3, duration=16.933333, status='VALID'),
            audio_result=dict(path=str(audio), sha256=sha(audio), duration=27.92, script_text=spoken))
        other = dict(rank='04', status='PASS', analysis={'product_description':'car ornament'},
            source_snapshot=dict(path=str(donor), sha256=sha(donor), rank=4, duration=24., status='VALID'))
        self.job = dict(id='job', market='Brazil', input_path=str(self.root),
                        batch_review={'phase':'RUNNING'}, items=[self.item, other])
        self.manager = MagicMock(); self.manager.lock = threading.RLock()
        self.manager.control.busy.return_value = False
        self.store = self.manager.store; self.store.home = self.root
        self.store.lock = threading.RLock(); self.store.get.return_value = self.job
        self.store.item.side_effect = lambda ident,rank,**values: next(
            x for x in self.job['items'] if x['rank']==rank).update(values)
        self.probe = patch('autocut.auto_supplement.inspect_media',
            return_value=dict(path=str(donor), sha256=sha(donor), duration=24.)).start()
        self.addCleanup(patch.stopall)

    def test_only_missing_stage_is_queued_and_paid_audio_preserved(self):
        before = copy.deepcopy(self.item['audio_result'])
        recover(self.manager, 'job', ['03'])
        self.manager.enqueue_batch.assert_called_once_with('job', [('03','SCRIPT')])
        self.assertEqual(self.item['audio_result'], before)
        self.assertEqual(self.job['items'][1]['status'], 'PASS')

    def test_large_gap_stays_waiting_without_queue_mutation(self):
        self.item['audio_result']['duration'] = 60
        with self.assertRaisesRegex(ValueError, 'VISUAL_DURATION_INSUFFICIENT'):
            recover(self.manager, 'job', ['03'])
        self.store.item.assert_not_called(); self.manager.enqueue_batch.assert_not_called()
        self.probe.assert_not_called()

    def test_changed_audio_is_rejected_before_any_retry(self):
        Path(self.item['audio_result']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'RECOVERY_CACHE_INVALID'):
            recover(self.manager, 'job', ['03'])
        self.store.item.assert_not_called()

    def test_busy_and_unconfirmed_jobs_are_rejected(self):
        self.manager.control.busy.return_value = True
        with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
            recover(self.manager, 'job', ['03'])
        self.manager.control.busy.return_value = False
        self.item['content_confirmed'] = False
        with self.assertRaisesRegex(ValueError, 'SCRIPT_CONFIRMATION_REQUIRED'):
            recover(self.manager, 'job', ['03'])
        self.store.item.assert_not_called()
