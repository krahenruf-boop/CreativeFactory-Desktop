"""Long editing cards stay valid and save exactly the submitted manuscript."""
import copy
import unittest

from autocut.feishu_tasks import cards
from autocut.feishu_tasks.edit_fields import merge_fields, split_text


def inputs(card):
    form = next(x for x in card['elements'] if x.get('tag') == 'form')
    return [x for x in form['elements'] if x.get('tag') == 'input']


class EditPartTests(unittest.TestCase):
    task = dict(id='CF-test', expected_count=10)

    def test_real_1074_character_editor_keeps_every_character(self):
        original = 'Uma ferramenta para seu carro. ' * 35 + 'final completo'
        original = (original + 'x'*1074)[:1074]
        payload = cards.edit('form', self.task, '04', 'script', 'revision',
                             dict(text=original, language='auto'))
        fields = inputs(payload)
        self.assertEqual(len(fields), 2)
        self.assertTrue(all(len(x['default_value'].encode('utf-16-le'))//2 <= x['max_length'] for x in fields))
        self.assertTrue(all(x['width']=='fill' for x in fields))
        posted = {x['name']:x['default_value'] for x in fields}
        self.assertEqual(merge_fields(posted)['text'], original)

    def test_editor_edits_both_parts_and_keeps_language(self):
        fields = dict(text='Primeira parte ', text_part_2='nova segunda parte.', language='pt-BR')
        self.assertEqual(merge_fields(fields), dict(text='Primeira parte nova segunda parte.', language='pt-BR'))
        self.assertEqual(fields['text_part_2'], 'nova segunda parte.')

    def test_empty_or_omitted_part_is_an_intentional_deletion(self):
        self.assertEqual(merge_fields(dict(text='Novo roteiro', text_part_2=''))['text'], 'Novo roteiro')
        self.assertEqual(merge_fields(dict(text_part_2='Somente a segunda parte'))['text'], 'Somente a segunda parte')

    def test_failed_save_card_preserves_raw_submitted_parts(self):
        raw = dict(text='a'*900, text_part_2='b'*200, language='auto')
        before = copy.deepcopy(raw)
        payload = cards.edit('form', self.task, '04', 'script', 'revision', raw, 'Tente novamente')
        self.assertEqual(merge_fields({x['name']:x['default_value'] for x in inputs(payload)})['text'], 'a'*900+'b'*200)
        self.assertEqual(raw, before)

    def test_title_and_old_single_input_remain_lossless(self):
        for kind,text in [('title','Título ' * 70), ('script','Texto curto.')]:
            payload = cards.edit('form', self.task, '01', kind, 'revision', dict(text=text))
            fields = inputs(payload)
            self.assertEqual(merge_fields({x['name']:x['default_value'] for x in fields})['text'], text)
            self.assertTrue(all(len(x['default_value']) <= x['max_length'] for x in fields))

    def test_unicode_emoji_never_splits_or_exceeds_utf16_units(self):
        text = '💕' * 501 + 'coração'
        parts = split_text(text, 1000)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(x.encode('utf-16-le'))//2 <= 1000 for x in parts))

    def test_invalid_parts_and_oversized_text_rejected(self):
        for fields in [dict(text=3), dict(text='ok',text_part_2=3), dict(text='ok',text_part_99='x')]:
            with self.assertRaisesRegex(ValueError, 'INVALID_REQUEST'):
                merge_fields(fields)
        with self.assertRaisesRegex(ValueError, 'SCRIPT_DRAFT_INVALID'):
            merge_fields(dict(text='a'*12001))
