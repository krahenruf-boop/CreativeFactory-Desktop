"""History reads and failed draft edits preserve real confirmation barriers."""
import copy
import json
import unittest
from unittest.mock import patch, MagicMock

import test_content_confirmation as fixtures
from autocut.content_confirmation import content_view
from autocut.copy_presentation import TranslationCache
from autocut.provider_models import job_models
from autocut.source_library import POLICY
from autocut.feishu_tasks.bridge import human


class RecentWorkflowTests(unittest.TestCase):
    setUp = fixtures.ContentTests.setUp
    tearDown = fixtures.ContentTests.tearDown
    raw = fixtures.ContentTests.raw
    state = fixtures.ContentTests.state
    post = fixtures.ContentTests.post

    def test_completed_history_skips_layout_without_reading_or_recovering_moved_output(self):
        job = self.store.get(self.id)
        job['production_preflight_policy'] = POLICY
        item = copy.deepcopy(self.raw())
        item.update(status='PASS', output='/retained/moved-original-output.mp4')
        before = copy.deepcopy(item)
        with patch('autocut.production_preflight.title_check', side_effect=AssertionError('history must not refit')):
            view = content_view(job, item)
        self.assertTrue(all(t['preflight']==dict(status='NOT_APPLICABLE', reason='COMPLETED_ARTIFACT') for t in view['titles']))
        self.assertEqual(item, before)

    def test_reopening_pending_draft_and_busy_items_still_check_every_title(self):
        job = self.store.get(self.id)
        original = self.raw()
        changes = [dict(content_edit=True), dict(operation_inflight=True),
                   dict(script_draft=dict(id='new-draft', mode='LOCALIZATION', candidates=[original['copy']['localized_script']], titles=original['title_candidates']))]
        for changed in changes:
            with self.subTest(changed=list(changed)):
                item = dict(copy.deepcopy(original), status='PASS', output='/retained/output.mp4', **changed)
                with patch('autocut.production_preflight.title_check', return_value=dict(status='BLOCKED', error='TITLE_SHORTEN_REQUIRED')) as check:
                    view = content_view(job, item)
                self.assertEqual(check.call_count, 3)
                self.assertTrue(all(t['preflight']['status']=='BLOCKED' for t in view['titles']))

    def test_exact_target_custom_title_reuses_validated_comparison_without_approval(self):
        job = self.store.get(self.id)
        content = self.app.state.content
        text = 'Olha esta fivela para sua roupa'
        model = job_models(job)['deepseek']
        content.translations.cache.put(text, 'pt-BR', model, '看看这个适合你穿搭的带扣')
        adapter = MagicMock(side_effect=AssertionError('cached comparison must not use provider'))
        content.translations.adapter = adapter
        before = copy.deepcopy(self.raw())
        for _ in range(2):
            response = self.post('content/custom-title', dict(text=text))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.raw()['content_custom_title']['text'], text)
        adapter.assert_not_called()
        self.assertTrue(TranslationCache(self.store.home).get(text, 'pt-BR', model))
        self.assertEqual(self.raw()['final_script'], before['final_script'])
        self.assertNotEqual(self.raw().get('title_status'), 'USER_APPROVED')
        self.assertEqual(self.queue.enqueued, [])

    def test_wrong_model_comparison_is_not_reused(self):
        text = 'Olha esta fivela para sua roupa'
        self.app.state.translations.cache.put(text, 'pt-BR', 'other-model', '仅用于其它模型的对照')
        adapter = fixtures.ContentTests.adapter(self)
        response = self.post('content/custom-title', dict(text=text))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(self.raw()['content_custom_title']['text'], text)
        self.assertEqual(self.queue.enqueued, [])

    def test_comparison_failure_preserves_titles_and_records_specific_safe_reason(self):
        text = 'Olha esta fivela para sua roupa'
        adapter = MagicMock()
        adapter.complete.return_value = dict(data=dict(title=dict(text=text, language='pt-BR', text_zh='unavailable')))
        self.app.state.translations.factory = lambda: adapter
        before = copy.deepcopy(self.raw())
        response = self.post('content/custom-title', dict(text=text))
        self.assertEqual(response.json()['error'], 'CONTENT_TRANSLATION_REQUIRED')
        self.assertEqual(self.raw()['title_candidates'], before['title_candidates'])
        self.assertFalse(self.raw()['operation_inflight'])
        data = json.loads((self.store.home/'jobs'/self.id/'work/operational-events.json').read_text())
        event = data['events'][-1]
        self.assertEqual(event['diagnostic']['code'], 'CONTENT_TRANSLATION_REQUIRED')
        self.assertEqual(event['stage'], 'CUSTOM_TITLE')
        self.assertNotIn(text, json.dumps(data))
        self.assertIn('原有标题和输入已保留', human(ValueError('CONTENT_TRANSLATION_REQUIRED')))
        self.assertEqual(self.queue.enqueued, [])

    def test_unknown_provider_details_are_never_written_to_diagnostics(self):
        private = 'provider raw secret and request text must remain private'
        self.app.state.content.title_request = MagicMock(side_effect=RuntimeError(private))
        response = self.post('content/titles')
        self.assertEqual(response.json()['error'], 'TITLE_GENERATION_FAILED')
        data = (self.store.home/'jobs'/self.id/'work/operational-events.json').read_text()
        self.assertNotIn(private, data)
        self.assertEqual(json.loads(data)['events'][-1]['diagnostic']['code'], 'STAGE_FAILED')


if __name__ == '__main__':
    unittest.main()
