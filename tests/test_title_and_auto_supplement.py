"""Focused planning/raster tests. No paid providers or production render."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import autocut.context
from autocut.title_layout import apply, MIN_FONT_SIZE
from autocut.auto_supplement import prepare, adapt, apply_montage
from autocut.review_media import base_clips
from autocut.duration_fit import solve
from autocut.pipeline import make_plan, Manager
from autocut.storage import Store
from autocut.item_pipeline import resume_item
from app.s3.visual_preset import build_visual_plan, validate_visual_plan, text_layer, fit_text
from app.s3.headless_runtime import sha
from app.s3.headless_media import validate_montage
from test_copy_presentation import response
from test_review_flow import DEC

LONG = 'POV: Ya vives en el 3026 y esta es la bolsita que todas van a querer'


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = []
        for rank, duration in [(1, 16.933333), (2, 24.), (3, 18.)]:
            path = self.root / f'{rank}.mp4'; path.write_bytes(f'isolated source {rank}'.encode())
            self.sources.append(dict(rank=rank, path=str(path), sha256=sha(path),
                duration=duration, width=720, height=1280, fps=30, status='VALID'))
        self.source = self.sources[0]
        self.job = dict(id='isolated', input_path=str(self.root), items=[dict(rank=f'{s["rank"]:02}',
            source_snapshot=s, analysis={'product_description':'phone wallet'}) for s in self.sources])
        self.clips = base_clips(self.source, {}, {})
        self.asset = dict(id='test-asset', file_path=self.source['path'], sha256=self.source['sha256'])
        self.probe = patch('autocut.auto_supplement.inspect_media', side_effect=self.inspect).start()
        self.addCleanup(patch.stopall)

    def inspect(self, path):
        s = next(s for s in self.sources if s['path'] == path)
        return dict(s, sha256=sha(path))

    def visual(self):
        return build_visual_plan(20, LONG, [], asset=self.asset)

    def test_01_real_problem_title_three_lines_and_raster(self):
        p = self.visual()
        with self.assertRaisesRegex(ValueError, 'TITLE_SHORTEN_REQUIRED'):
            fit_text(LONG, p['title'], p['canvas'])
        fit = apply(p, LONG)
        self.assertEqual(len(fit['lines']), 3)
        self.assertEqual(' '.join(fit['lines']), LONG)
        self.assertEqual(validate_visual_plan(p), [])
        layer = text_layer(p['title']['text'], p['title'], p['canvas'], self.root/'title.png')
        self.assertEqual(layer['lines'], fit['lines'])
        self.assertGreaterEqual(layer['reference_font_size'], MIN_FONT_SIZE)
        self.assertLess(layer['bbox'][2], 1080)

    def test_02_smaller_font_preserves_every_word(self):
        p = self.visual(); text = LONG + ' en todos los colores para tu día'
        fit = apply(p, text)
        self.assertLess(fit['reference_font_size'], 68)
        self.assertGreaterEqual(fit['reference_font_size'], MIN_FONT_SIZE)
        self.assertLessEqual(len(fit['lines']), 3)
        self.assertEqual(' '.join(fit['lines']), text)

    def test_03_normal_title_unchanged(self):
        p = self.visual(); p['title']['text'] = 'Mira qué linda bolsita'
        before = copy.deepcopy(p); apply(p, p['title']['text'])
        self.assertEqual(p, before)

    def test_approved_sentence_titles_fit_without_rewriting(self):
        titles = [
            'La bolsita más inteligente del 2026: tu celular queda exacto, con espacio para dinero y tarjetas, y es resistente al agua.',
            '¿Cansada de buscar tu tarjeta en el fondo de la bolsa? Esta bolsita tiene ranuras especiales para que todo esté en su lugar.',
        ]
        for canvas in [(1080, 1920), (720, 1280)]:
            for number, text in enumerate(titles):
                with self.subTest(canvas=canvas, title=number):
                    p = build_visual_plan(30, text, [], asset=self.asset, canvas=canvas)
                    before = copy.deepcopy(p)
                    fit = apply(p, text)
                    self.assertEqual(' '.join(fit['lines']), text)
                    self.assertEqual(len(fit['lines']), 3)
                    # Scaled raster sizes round to whole pixels; at 720p the
                    # same text can fit one reference-size step earlier.
                    if canvas[0] == 1080:
                        self.assertLess(fit['reference_font_size'], 56)
                    self.assertGreaterEqual(fit['reference_font_size'], MIN_FONT_SIZE)
                    self.assertEqual(validate_visual_plan(p), [])
                    layer = text_layer(p['title']['text'], p['title'], canvas, self.root/'sentence.png')
                    self.assertEqual(layer['lines'], fit['lines'])
                    x1, y1, x2, y2 = layer['bbox']
                    self.assertGreaterEqual(x1, canvas[0] * .055)
                    self.assertLessEqual(x2, canvas[0] * .945)
                    self.assertGreaterEqual(y1, 0)
                    self.assertLessEqual(y2, canvas[1])
                    for key in ['font_file', 'fill', 'stroke', 'start', 'end', 'anchor_x', 'anchor_y']:
                        self.assertEqual(p['title'][key], before['title'][key])
                    self.assertEqual(p['subtitle'], before['subtitle'])
                    self.assertEqual(p['cta'], before['cta'])

    def test_unreadably_long_title_still_requires_edit_without_mutation(self):
        p = self.visual(); before = copy.deepcopy(p)
        with self.assertRaisesRegex(ValueError, 'TITLE_SHORTEN_REQUIRED'):
            apply(p, 'Extraordinariamente' * 12)
        self.assertEqual(p, before)

    def test_04_small_gap_appends_and_fits(self):
        clips, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        self.assertTrue(fit['fits']); self.assertEqual(record['status'], 'READY')
        self.assertLessEqual(record['missing_ratio'], 1/3)
        self.assertEqual(clips[0]['path'], self.source['path'])
        self.assertEqual(clips[-1]['path'], self.sources[1]['path'])
        self.assertAlmostEqual(record['added_duration'], 51/30)
        self.assertLessEqual(fit['voice_speed'], 1.5)
        self.assertEqual(fit['video_speed'], 1)
        self.assertTrue(fit['audio_filter'].startswith('atempo='))

    def test_05_threshold_inclusive_and_over_one_third_waits(self):
        s = dict(self.source, duration=20); clips = base_clips(s, {}, {})
        self.assertTrue(prepare(self.job, s, clips, 45, {})[1]['fits'])
        result = prepare(self.job, s, clips, 45.05, {})
        self.assertFalse(result[1]['fits']); self.assertEqual(result[2]['status'], 'GAP_TOO_LARGE')

    def test_remix_donor_uses_validated_original_clip_and_preserves_order(self):
        # Every donor is a montage, as in the real 30-source / 15-output task.
        donor = self.job['items'][1]['source_snapshot']
        donor['remix_clips'] = [dict(self.clips[0]), dict(path=self.sources[2]['path'],
            sha256=self.sources[2]['sha256'], source_start=2., source_end=10.,
            source_duration=self.sources[2]['duration'], source_rank=3)]
        self.job['items'] = self.job['items'][:2]
        before = copy.deepcopy(self.clips)
        clips, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        self.assertTrue(fit['fits'])
        self.assertEqual(self.clips, before)
        self.assertEqual(clips[0]['path'], before[0]['path'])
        self.assertEqual(clips[0]['sha256'], before[0]['sha256'])
        self.assertEqual(clips[0]['source_start'], before[0]['source_start'])
        self.assertAlmostEqual(clips[0]['source_end'], before[0]['source_end'], delta=1/30)
        self.assertEqual(clips[-1]['path'], self.sources[2]['path'])
        self.assertEqual(clips[-1]['source_rank'], 3)
        self.assertGreaterEqual(clips[-1]['source_start'], 2.)
        self.assertLessEqual(clips[-1]['source_end'], 10.)
        self.assertEqual(record['status'], 'READY')

    def test_remix_secondary_changed_hash_is_never_used(self):
        donor = self.job['items'][1]['source_snapshot']
        donor['remix_clips'] = [dict(path=self.sources[2]['path'],
            sha256='changed-hash', source_start=2., source_end=10.,
            source_duration=self.sources[2]['duration'], source_rank=3)]
        self.job['items'] = self.job['items'][:2]
        clips, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        self.assertFalse(fit['fits'])
        self.assertEqual(clips, self.clips)
        self.assertEqual(record['skipped'][0]['reason'], 'SOURCE_HASH_CHANGED')

    def test_remix_secondary_other_folder_is_never_opened(self):
        donor = self.job['items'][1]['source_snapshot']
        donor['remix_clips'] = [dict(path=str(self.root/'other'/'3.mp4'),
            sha256=self.sources[2]['sha256'], source_start=2., source_end=10.,
            source_duration=self.sources[2]['duration'], source_rank=3)]
        self.job['items'] = self.job['items'][:2]
        self.assertFalse(prepare(self.job, self.source, self.clips, 27.92, {})[1]['fits'])
        self.probe.assert_not_called()

    def test_remix_larger_gap_preserves_at_least_half_original_footage(self):
        self.job['mode'] = 'Remix'
        source = dict(self.source, duration=20.)
        original = base_clips(source, {}, {})
        source['remix_clips'] = copy.deepcopy(original)
        clips, fit, record = prepare(self.job, source, original, 47., {})
        self.assertTrue(fit['fits'])
        self.assertEqual(record['max_missing_ratio'], .5)
        self.assertLessEqual(record['missing_ratio'], .5)
        self.assertGreater(sum(c['source_end']-c['source_start'] for c in original),
                           sum(c['source_end']-c['source_start'] for c in clips)/2)
        self.assertEqual(clips[0]['path'], original[0]['path'])
        self.assertEqual(clips[0]['source_start'], original[0]['source_start'])
        # The same gap in Transport still asks for explicit supplements.
        self.job['mode'] = 'Transport'
        self.assertEqual(prepare(self.job, source, original, 47., {})[2]['status'], 'GAP_TOO_LARGE')

    def test_remix_more_than_half_still_waits(self):
        self.job['mode'] = 'Remix'
        source = dict(self.source, duration=20.)
        original = base_clips(source, {}, {})
        source['remix_clips'] = copy.deepcopy(original)
        clips, fit, record = prepare(self.job, source, original, 61., {})
        self.assertFalse(fit['fits'])
        self.assertEqual(record['status'], 'GAP_TOO_LARGE')
        self.assertEqual(clips, original)
        self.probe.assert_not_called()

    def test_06_sufficient_and_music_never_borrow(self):
        for voice, music in [(20, False), (100, True)]:
            self.assertIsNone(prepare(self.job, self.source, self.clips, voice, {}, music)[2])
        self.probe.assert_not_called()

    def test_07_no_other_folder_current_duplicate_unanalysed(self):
        self.job['items'][1]['source_snapshot'] = dict(self.sources[1], path=str(self.root/'other'/'2.mp4'))
        self.job['items'][2]['source_snapshot'] = dict(self.sources[2], sha256=self.source['sha256'])
        clips, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        self.assertEqual(clips, self.clips); self.assertFalse(fit['fits']); self.probe.assert_not_called()
        self.job['items'][2]['source_snapshot'] = self.sources[2]
        self.job['items'][2]['analysis'] = {}
        self.assertFalse(prepare(self.job, self.source, self.clips, 27.92, {})[1]['fits'])

    def test_08_corrupt_or_changed_candidate_skipped(self):
        Path(self.sources[1]['path']).write_bytes(b'changed')
        self.job['items'] = self.job['items'][:2]
        _, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        self.assertFalse(fit['fits']); self.assertEqual(record['skipped'][0]['reason'], 'SOURCE_HASH_CHANGED')

    def test_09_repeat_plan_deterministic_and_inputs_unchanged(self):
        before = copy.deepcopy(self.job)
        one = prepare(self.job, self.source, self.clips, 27.92, {})
        two = prepare(self.job, self.source, self.clips, 27.92, {})
        self.assertEqual(one, two); self.assertEqual(self.job, before)
        self.assertIsNone(prepare(self.job, self.source, one[0], 27.92, {})[2])

    def test_10_append_timeline_preserves_order_and_duration(self):
        clips, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        source = adapt(self.source, clips, record)
        p = dict(target_duration=fit['target_duration'], video_speed=1., source_ranges=[[0,source['duration']]])
        apply_montage(p, source); validate_montage(p)
        self.assertEqual(p['montage_segments'][0]['source_start'], 0)
        self.assertAlmostEqual(p['montage_segments'][0]['output_end'], 508/30)
        self.assertEqual(p['montage_segments'][1]['output_start'], p['montage_segments'][0]['output_end'])
        self.assertAlmostEqual(p['montage_segments'][-1]['output_end'], fit['target_duration'])

    def test_11_real_plan_builder_title_append_subtitles_cta(self):
        clips, fit, record = prepare(self.job, self.source, self.clips, 27.92, {})
        source = adapt(self.source, clips, record); response_mx = response('es-MX')
        response_mx['titles'][0]['text'] = LONG
        align = self.root/'align.json'; align.write_text(json.dumps({'alignment':{'characters':['M'], 'character_start_times_seconds':[0.], 'character_end_times_seconds':[.2]}}))
        audio = dict(path=str(self.root/'paid.wav'), duration=27.92, alignment_path=str(align))
        captions = [{'start':0,'end':2,'text':'Mira esta bolsita'}]
        with patch('autocut.pipeline.asset', return_value=self.asset), patch('autocut.pipeline.subtitle_guard'), patch('autocut.pipeline.caption_events',return_value=captions):
            p = make_plan(source,audio,response_mx,DEC,LONG,self.root,self.root/'output.mp4',{},target='es-MX')
        self.assertEqual(p['title'], LONG)
        self.assertEqual(p['final_script'], response_mx['localized_script']['text'])
        self.assertEqual(p['visual']['subtitle']['captions'], captions)
        self.assertEqual(p['audio']['path'], audio['path'])
        self.assertEqual(validate_visual_plan(p['visual']), [])
        self.assertTrue(p['visual']['cta']['enabled'])
        self.assertAlmostEqual(p['visual']['cta']['start'], p['target_duration']-5)
        self.assertEqual(len(p['montage_segments']), 2)

    def test_12_resume_reuses_paid_audio_and_reaches_render_boundary(self):
        store = Store(self.root/'data'); job,_ = store.create(dict(request_id='one',input_path=str(self.root),product_name='Phone bag',market='Mexico',mode='Transport',product_profile_id='bag'))
        id = job['id']; r = response('es-MX'); r['titles'][0]['text'] = LONG
        audio = self.root/'paid.wav'; audio.write_bytes(b'already paid audio')
        cached = dict(path=str(audio),sha256=sha(audio),duration=27.92)
        store.update(id,state='PARTIAL_COMPLETED',batch_review={'phase':'RUNNING'})
        for i,s in enumerate(self.sources,1):
            store.item(id,i,status='FAILED',source_snapshot=s,analysis={'product_description':'phone wallet'},decision=DEC,source_sheet='cached.jpg')
        store.item(id,1,copy=r,final_script=r['localized_script']['text'],final_script_language='es-MX',audio_result=cached,script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,final_title=r['titles'][0],selected_title_id=1)
        peer = store.get(id)['items'][1:]
        manager = Manager(store,MagicMock(),MagicMock()); self.addCleanup(manager.queue.shutdown)
        manager.stops[id] = threading.Event()
        with patch('autocut.item_pipeline.Harness'), patch('autocut.item_pipeline.tts_job') as paid, patch('autocut.pipeline.make_plan',return_value={'rank':'01','final_script':r['localized_script']['text']}) as build, patch.object(manager,'finish_plans') as render, patch('autocut.item_pipeline.ItemReview.evidence',return_value=(self.source,{'product_description':'phone wallet'},'cached.jpg')):
            resume_item(manager,id,'01','TITLE')
            paid.assert_not_called(); manager.router.resolve.assert_not_called()
            build.assert_called_once(); render.assert_called_once()
            self.assertEqual(len(build.call_args.args[0]['remix_clips']),2)
        x = store.get(id)['items'][0]
        self.assertTrue(x['duration_match']['fits']); self.assertEqual(x['audio_result']['sha256'],cached['sha256'])
        self.assertEqual(x['final_title'],r['titles'][0]); self.assertEqual(store.get(id)['items'][1:],peer)
        with store.connect() as db:
            self.assertIsNotNone(db.execute("select data from checkpoints where stage='AUTO_SUPPLEMENT_PLAN'").fetchone())

    def test_13_float_frame_boundary_not_lost(self):
        self.assertTrue(solve(18.63333333333333,27.92,{'conservative_motion':True})['fits'])


if __name__ == '__main__':
    unittest.main()
