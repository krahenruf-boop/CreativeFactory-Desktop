import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image
from autocut import context
from autocut.qa_evidence import VERSION, review_images, request
from autocut.ai_recovery import resume_final
from app.s3.headless_media import FRACTIONS
from app.s3.headless_runtime import sha, write


class QAEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'jobs/job/work'
        folder = self.work / 'qa/02'
        folder.mkdir(parents=True)
        self.contact = folder / 'contact.jpg'
        Image.new('RGB', (810, 1530), 'white').save(self.contact)
        self.output = self.root / 'existing.mp4'
        self.output.write_bytes(b'unchanged existing MP4')
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'original source')
        self.audio = self.root / 'voice.mp3'
        self.audio.write_bytes(b'already paid audio')
        entry = dict(path=str(self.source), sha256=sha(self.source))
        self.plan = dict(rank='02', output=str(self.output), final_script='Este produto ajuda.',
                         title='Um presente especial', source=entry,
                         audio=dict(audio_path=str(self.audio), sha256=sha(self.audio)),
                         montage_segments=[dict(entry, output_start=0, output_end=29.5),
                                           dict(entry, output_start=29.5, output_end=30)])
        times = [30 * f for f in FRACTIONS] + [0, 14.75, 29.5 - 1 / 30, 29.5, 29.75, 30 - 1 / 30]
        frames = sorted({round(t * 30) for t in times})
        samples = []
        for index, frame in enumerate(frames):
            path = folder / f'frame_{index:03}.png'
            Image.new('RGB', (54, 96), (index * 10 % 255, 80, 160)).save(path)
            samples.append(dict(path=str(path), frame=frame, time=frame / 30))
        self.technical = dict(status='PASS', output_sha256=sha(self.output), duration=30,
                              full_decode_count=1, issues=[], samples=samples,
                              artifacts=[dict(path=s['path'], sha256=sha(s['path'])) for s in samples])
        self.arrow = dict(status='PASS', output_sha256=sha(self.output))
        write(self.work / 'qa/02/qa.json', self.technical)
        write(self.work / 'plans/02.json', self.plan)
        self.item = dict(rank='02', status='FAILED', issues=['SEVERE_VISUAL_QA'],
                         script_status='USER_APPROVED', title_status='USER_APPROVED',
                         content_confirmed=True, final_script=dict(text=self.plan['final_script'], language='pt-BR'),
                         final_title=dict(text=self.plan['title'], language='pt-BR'),
                         plan=str(self.work / 'plans/02.json'), output=str(self.output))
        self.job = dict(id='job', state='PARTIAL_COMPLETED', market='Brazil',
                        batch_review={'phase': 'RUNNING'}, items=[self.item])
        self.rendered = dict(input_sha='original-render', data=dict(output_sha256=sha(self.output),
                                                                  cta_verification=self.arrow))
        self.previous = dict(technical=self.technical, status='FAIL',
                             visual=dict(status='REVIEW_REQUIRED', severity='SEVERE',
                                         product_preserved=False, issues=['Missing final clip evidence']))
        self.records = {'FINAL_RENDERED': self.rendered,
                        'QA_DONE': dict(input_sha='original-render', data=self.previous)}
        self.manager = MagicMock()
        self.manager.lock = threading.RLock()
        self.manager.item_pending = set()
        self.store = self.manager.store
        self.store.home = self.root
        self.store.lock = threading.RLock()
        self.store.get.return_value = self.job
        self.store.item.side_effect = lambda ident, rank, **fields: self.item.update(fields)
        self.store.done.side_effect = lambda ident, stage, rank, fingerprint, data: self.records.update(
            {stage: dict(input_sha=fingerprint, data=copy.deepcopy(data))})
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE checkpoints(job_id,stage,item,input_sha,data)')
        db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)',
                   ('job', 'VISUAL_QA', 'final-02', 'old-judge', json.dumps(self.previous['visual'])))
        self.store.connect.side_effect = lambda: db
        patcher = patch('autocut.qa_evidence.checkpoints', side_effect=lambda *args: self.records)
        patcher.start()
        self.addCleanup(patcher.stop)
        existing = patch('autocut.ai_recovery.checkpoints', side_effect=lambda *args: self.records)
        existing.start()
        self.addCleanup(existing.stop)

    def test_last_half_second_is_shown_without_changing_contact_or_mp4(self):
        before = [sha(p) for p in (self.contact, self.output, self.audio)]
        images, coverage = review_images(self.work, '02', self.plan, self.technical)
        self.assertEqual(images[0], str(self.contact))
        self.assertEqual(len(images), 2)
        self.assertTrue(any(s['time'] >= 29.96 for s in coverage['required_samples']))
        self.assertTrue(any(s['time'] == 29.5 for s in coverage['required_samples']))
        self.assertEqual(before, [sha(p) for p in (self.contact, self.output, self.audio)])
        for evidence in coverage['additional_images']:
            self.assertEqual(sha(evidence['path']), evidence['sha256'])

    def test_missing_or_changed_frame_is_blocking(self):
        Path(self.technical['samples'][-1]['path']).unlink()
        with self.assertRaisesRegex(ValueError, 'QA_EVIDENCE_MISSING'):
            review_images(self.work, '02', self.plan, self.technical)

    def test_changed_decoded_frame_hash_is_rejected(self):
        Image.new('RGB', (54, 96), 'red').save(self.technical['samples'][-1]['path'])
        with self.assertRaisesRegex(ValueError, 'QA_EVIDENCE_CHANGED'):
            review_images(self.work, '02', self.plan, self.technical)

    def test_completed_or_unconfirmed_job_is_never_reopened(self):
        for fields in ({'state': 'COMPLETED'}, {'state': 'PAUSED'}):
            self.job.update(fields)
            with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
                request(self.manager, 'job', ['02'])
        self.job['state'] = 'PARTIAL_COMPLETED'
        self.item['title_status'] = 'WAITING_APPROVAL'
        with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
            request(self.manager, 'job', ['02'])
        self.store.item.assert_not_called()
        self.manager.enqueue_batch.assert_not_called()

    def test_queue_is_item_only_and_archives_old_verdict_once(self):
        with patch('autocut.item_review.audit'):
            request(self.manager, 'job', ['02'])
        self.manager.enqueue_batch.assert_called_once_with('job', [('02', 'AI_FINAL_QA')])
        self.assertEqual(self.records[VERSION]['data']['previous_qa']['data'], self.previous)
        self.assertEqual(self.records[VERSION]['data']['previous_visual']['input_sha'], 'old-judge')
        self.item.update(status='FAILED', issues=['SEVERE_VISUAL_QA'])
        self.manager.enqueue_batch.reset_mock()
        request(self.manager, 'job', ['02'])
        self.manager.enqueue_batch.assert_not_called()

    def test_changed_source_is_rejected_before_queue(self):
        self.source.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError, 'RECOVERY_CACHE_INVALID'):
            request(self.manager, 'job', ['02'])
        self.store.item.assert_not_called()
        self.manager.enqueue_batch.assert_not_called()

    def test_real_severe_verdict_still_fails_and_never_renders_or_pays(self):
        self.manager.judge.return_value = dict(status='REVIEW_REQUIRED', severity='SEVERE',
                                              product_preserved=False, issues=['PRODUCT_OBSCURED'])
        with patch('autocut.ai_recovery.checkpoints', return_value=self.records), \
             patch('autocut.ai_recovery.Harness'), patch('autocut.ai_recovery.audit'), \
             patch('autocut.ai_recovery.review_issues', return_value=[]), \
             patch('autocut.item_pipeline.tts_job') as tts, \
             patch('autocut.automatic_finish.final_job') as render:
            resume_final(self.manager, 'job', '02')
        self.assertEqual(self.item['status'], 'FAILED')
        self.assertEqual(self.item['issues'], ['SEVERE_VISUAL_QA'])
        self.assertEqual(len(self.manager.judge.call_args.args[-1]), 2)
        tts.assert_not_called()
        render.assert_not_called()

    def test_success_reuses_actual_bytes_and_original_render_fingerprint(self):
        before = [sha(p) for p in (self.output, self.audio, self.source)]
        self.manager.judge.return_value = dict(status='PASS', severity='NONE', issues=[])
        with patch('autocut.ai_recovery.checkpoints', return_value=self.records), \
             patch('autocut.ai_recovery.Harness'), patch('autocut.ai_recovery.audit'), \
             patch('autocut.ai_recovery.review_issues', return_value=[]), \
             patch('autocut.ai_recovery.final_ready', return_value=True):
            resume_final(self.manager, 'job', '02')
        self.assertEqual(self.item['status'], 'PASS')
        self.assertEqual(self.records['QA_DONE']['input_sha'], 'original-render')
        self.assertEqual(before, [sha(p) for p in (self.output, self.audio, self.source)])


if __name__ == '__main__':
    unittest.main()
