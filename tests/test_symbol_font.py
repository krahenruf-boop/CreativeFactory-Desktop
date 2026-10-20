"""Exercise actual glyph rendering; no providers, live Store or production MP4."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import autocut.context
from PIL import Image
from app.s3.visual_preset import load_preset, load_font, text_layer
from autocut.symbol_font import SymbolFont, compatible_font


class SymbolFontTests(unittest.TestCase):
    def setUp(self):
        self.style = copy.deepcopy(load_preset()['title'])

    def test_approved_heart_title_renders_and_records_glyph_font(self):
        text = 'Amor no carro 💕'
        font, primary_path, primary_hash = load_font(self.style, 64, text)
        self.assertIsInstance(font, SymbolFont)
        self.assertEqual(primary_hash, self.style['font_sha256'])
        self.assertEqual(Path(primary_path).name, Path(self.style['font_file']).name)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'title.png'
            evidence = text_layer(text, self.style, (1080, 1920), path)
            self.assertEqual(evidence['text'], text)
            self.assertTrue(evidence['symbol_font']['primary_font_unchanged'])
            self.assertEqual(evidence['symbol_font']['glyphs'], ['💕'])
            self.assertEqual(evidence['font_sha256'], self.style['font_sha256'])
            with Image.open(path) as image:
                self.assertTrue(image.getchannel('A').getbbox())

    def test_ordinary_font_and_accents_remain_original(self):
        font, _, _ = load_font(self.style, 64, 'Coração e carinho')
        self.assertNotIsInstance(font, SymbolFont)

    def test_poppins_subtitle_preserves_approved_emoji(self):
        style = copy.deepcopy(load_preset()['subtitle'])
        with tempfile.TemporaryDirectory() as directory:
            evidence = text_layer('dia 💕', style, (1080, 1920),
                                  Path(directory)/'subtitle.png', subtitle=True)
            self.assertEqual(evidence['text'], 'dia 💕')
            self.assertIn('Poppins', evidence['font_file'])
            self.assertEqual(evidence['symbol_font']['glyphs'], ['💕'])

    def test_emoji_does_not_hide_unsupported_ordinary_characters(self):
        with self.assertRaisesRegex(ValueError, 'unsupported non-emoji'):
            load_font(self.style, 64, 'Amor 💕 中文')

    def test_font_hash_guard_remains_required(self):
        self.style['font_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'font hash changed'):
            load_font(self.style, 64, 'Amor 💕')

    def test_distinct_emoji_have_distinct_masks_and_fit_sizes(self):
        from app.s3.visual_preset import ROOT
        for size in (48, 55, 64, 68):
            font, _, _ = compatible_font(self.style, size, '💕💙', ROOT)
            self.assertNotEqual(bytes(font.getmask('💕')), bytes(font.getmask('💙')))
            self.assertGreater(font.getbbox('💕')[2], 0)


class SymbolRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        work = self.root/'jobs/job/work'
        work.mkdir(parents=True)
        (work/'RUN_REPORT.jsonl').write_text(json.dumps(dict(rank='01',
            event='ITEM_RECOVERY_ERROR', error='FONT_LOAD_FAILED: font: missing glyph U+1F495'))+'\n')
        source, audio = self.root/'source.mp4', self.root/'audio.mp3'
        source.write_bytes(b'original source'); audio.write_bytes(b'already paid audio')
        from autocut.symbol_font import digest
        spoken = 'Este kit deixa seu carro muito mais bonito 💕'
        self.item = dict(rank='01', status='FAILED', issues=['JOB_REVIEW_REQUIRED'],
            content_confirmed=True, script_status='USER_APPROVED', title_status='USER_APPROVED',
            final_script=dict(text=spoken, language='pt-BR'),
            final_title=dict(text='Seu carro muito mais bonito', language='pt-BR'),
            analysis={'context':'car decoration'}, decision={'approved':True},
            source_snapshot=dict(path=str(source), sha256=digest(source)),
            audio_result=dict(path=str(audio), sha256=digest(audio), script_text=spoken))
        self.job = dict(id='job', market='Brazil', batch_review={'phase':'RUNNING'},
                        items=[self.item, dict(rank='02', status='PASS')])
        self.manager = MagicMock()
        self.manager.lock = threading.RLock()
        self.manager.control.busy.return_value = False
        self.store = self.manager.store
        self.store.home, self.store.lock = self.root, threading.RLock()
        self.store.get.return_value = self.job
        self.store.item.side_effect = lambda ident, rank, **values: next(
            x for x in self.job['items'] if x['rank']==rank).update(values)

    def test_only_failed_rank_is_queued_with_existing_audio(self):
        from autocut.symbol_font import recover
        before = copy.deepcopy(self.item['audio_result'])
        recover(self.manager, 'job', ['01'])
        self.manager.enqueue_batch.assert_called_once_with('job', [('01','SCRIPT')])
        self.assertEqual(self.item['audio_result'], before)
        self.assertEqual(self.job['items'][1]['status'], 'PASS')

    def test_changed_audio_is_rejected_before_mutating(self):
        from autocut.symbol_font import recover
        Path(self.item['audio_result']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'RECOVERY_CACHE_INVALID'):
            recover(self.manager, 'job', ['01'])
        self.store.item.assert_not_called()
        self.manager.enqueue_batch.assert_not_called()

    def test_running_job_is_not_enqueued_twice(self):
        from autocut.symbol_font import recover
        self.manager.control.busy.return_value = True
        with self.assertRaisesRegex(ValueError, 'STATE_TRANSITION_REJECTED'):
            recover(self.manager, 'job', ['01'])
        self.store.item.assert_not_called()

    def test_human_approval_is_required(self):
        from autocut.symbol_font import recover
        self.item['content_confirmed'] = False
        with self.assertRaisesRegex(ValueError, 'SCRIPT_CONFIRMATION_REQUIRED'):
            recover(self.manager, 'job', ['01'])
        self.store.item.assert_not_called()


if __name__ == '__main__':
    unittest.main()

class TextSymbolFallbackTests(unittest.TestCase):
    def test_confirmed_mixed_emoji_and_female_symbol_remain_visible(self):
        from autocut.symbol_font import runs,TEXT_SYMBOL_FONT,supported
        style=copy.deepcopy(load_preset()['title'])
        # The confirmed title contains a literal escape, which must not be rewritten.
        text='50% OFF! 🌾🏃\\u200d♀️ Clica no carrinho!'
        self.assertEqual(''.join(s for s,_ in runs(text)),text)
        font,_,_=load_font(style,48,text)
        self.assertTrue(font.symbol_mask('♀️').getbbox())
        self.assertNotEqual(font.symbol_mask('♀️').tobytes(),font.symbol_mask('🌾').tobytes())
        self.assertEqual(font.symbol_evidence['text_symbol_font']['font_file'],str(TEXT_SYMBOL_FONT))
        with tempfile.TemporaryDirectory() as d:
            result=text_layer(text,style,(1080,1920),Path(d)/'title.png')
            self.assertEqual(result['text'],text)
            self.assertTrue(result['symbol_font']['text_symbol_font']['font_sha256'])

    def test_real_joined_runner_is_one_cluster_and_unknown_symbol_still_fails(self):
        from autocut.symbol_font import runs
        joined='🏃\u200d♀️';self.assertEqual(runs(joined),[(joined,True)])
        font,_,_=load_font(copy.deepcopy(load_preset()['title']),48,joined)
        self.assertTrue(font.symbol_mask(joined).getbbox())
        with self.assertRaisesRegex(ValueError,'SYMBOL_GLYPH_UNAVAILABLE'):
            load_font(copy.deepcopy(load_preset()['title']),48,'\U0001faff')
