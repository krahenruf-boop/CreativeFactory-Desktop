"""Pasted manuscript regressions: isolated SQLite, no paid providers/render."""
import copy,json,unittest
from unittest.mock import MagicMock
import test_batch_workspace as batch_fixture
from test_copy_presentation import BR,ZH,response
from autocut.provider_models import job_models
from autocut.script_language import check

class PastedScriptTests(unittest.TestCase):
 setUp=batch_fixture.BatchTests.setUp
 tearDown=batch_fixture.BatchTests.tearDown
 raw=batch_fixture.BatchTests.raw
 state=batch_fixture.BatchTests.state
 post=batch_fixture.BatchTests.post
 public=batch_fixture.BatchTests.public
 view=batch_fixture.BatchTests.view
 def missing(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=['LOCALIZATION_REQUIRED'],final_script=None,localized_script=None,copy={},script_candidates=[],title_candidates=[],script_status=None)
 def use(self,text=BR):return self.post('script/draft',dict(mode='PASTED',text=text,language='pt-BR'))
 def test_exact_job_response_reused_without_provider(self):
  self.missing();other=copy.deepcopy(self.store.get(self.id)['items'][1:])
  p=self.store.home/'jobs'/self.id/'work/scripts/copy_results.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'prepared':{'response':response()}}))
  self.adapter.complete=MagicMock(side_effect=AssertionError('No provider needed'))
  r=self.use();self.assertEqual(r.status_code,200,r.text)
  d=self.raw()['script_draft'];self.assertEqual(d['candidates'][0]['text'],BR);self.assertEqual(d['candidates'][0]['text_zh'],ZH);self.assertEqual(len(d['titles']),3)
  self.assertTrue(self.view()['items'][0]['script_ready']);self.assertEqual(self.store.get(self.id)['items'][1:],other);self.assertEqual(self.queue.enqueued,[])
 def test_bad_generated_title_cannot_discard_valid_manuscript(self):
  self.missing();bad=dict(text_zh=ZH,titles=[dict(text='This bag is easy to use',text_zh='这个包很方便',language='pt-BR')]*3)
  self.adapter.complete=MagicMock(return_value={'data':bad})
  r=self.use();self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['presentation_warning'],'TITLE_GENERATION_FAILED')
  self.assertEqual(self.raw()['script_draft']['candidates'][0]['text'],BR);self.assertEqual(self.raw()['script_draft']['titles'],[])
  b=self.view();self.assertTrue(b['items'][0]['script_ready']);self.assertFalse(b['items'][0]['title_ready']);self.assertEqual(self.queue.enqueued,[])
  # Retry the saved draft uses its cache, including the Chinese comparison.
  self.assertEqual(self.use().status_code,200);self.assertEqual(self.adapter.complete.call_count,1)
  with self.assertRaisesRegex(ValueError,'TITLE_LANGUAGE_MISMATCH'):check(bad['titles'][0],'pt-BR','TITLE',True)
 def test_failure_preserves_existing_script_and_new_token_can_retry(self):
  old=copy.deepcopy(self.raw());token=self.state()['action_token'];self.adapter.complete=MagicMock(side_effect=ValueError('DEEPSEEK_CONNECTION_FAILED'))
  r=self.use(BR+' Veja este detalhe.');self.assertEqual(r.status_code,400)
  self.assertEqual(self.raw()['final_script'],old['final_script']);self.assertFalse(self.raw()['operation_inflight']);self.assertNotEqual(token,self.state()['action_token'])
  self.app.state.translations.cache.put(BR+' Veja este detalhe.','pt-BR',job_models(self.store.get(self.id))['deepseek'],ZH)
  self.assertEqual(self.use(BR+' Veja este detalhe.').status_code,200);self.assertEqual(self.queue.enqueued,[])
 def test_unrelated_cached_manuscript_never_reused(self):
  self.missing();r=response();r['localized_script']['text']=BR+' Veja agora.'
  p=self.store.home/'jobs'/self.id/'work/scripts/copy_results.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'other':{'response':r}}))
  self.adapter.complete=MagicMock(side_effect=ValueError('DEEPSEEK_CONNECTION_FAILED'))
  self.assertEqual(self.use().status_code,400);self.assertNotIn('script_draft',self.raw());self.assertEqual(self.queue.enqueued,[])
 def test_exact_foreign_manuscript_reuses_valid_translation_without_other_rank_titles(self):
  original='This little bag has room for your phone and your cards.'
  candidate=dict(text=BR,text_zh=ZH,language='pt-BR')
  self.store.item(self.id,'02',script_draft=dict(mode='PASTED',original_text=original,original_language='en',candidates=[candidate],titles=[dict(text='Other hook')]))
  self.missing()
  self.adapter.complete=MagicMock(side_effect=AssertionError('Exact translation already exists'))
  r=self.post('script/draft',dict(mode='PASTED',text=original,language='auto'))
  self.assertEqual(r.status_code,200,r.text)
  draft=self.raw()['script_draft']
  self.assertEqual(draft['candidates'][0]['text'],BR)
  self.assertEqual(draft['original_text'],original)
  self.assertEqual(draft['titles'],[])
  self.assertTrue(self.view()['items'][0]['script_ready'])
  self.adapter.complete.assert_not_called()
  self.assertEqual(self.queue.enqueued,[])
