"""0.1.14 focused regressions. Isolated DB and mocked paid services only."""
import copy,json,threading,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from test_batch_workspace import BatchTests
from test_content_confirmation import TitleAdapter
from test_copy_presentation import BR,MX,ZH,response,MockAdapter
from test_review_flow import ANALYSIS,DEC,future
from autocut.ownership import checkpoints
from autocut.pipeline import Manager
from autocut.item_pipeline import resume_item
from autocut.script_language import script_fields
from autocut.provider_models import job_models
from autocut.storage import Store
from app.s3.deepseek_batch import generate_batch

class DirectAdapter(TitleAdapter):
 def complete(self,model,messages,**kw):
  c=json.loads(messages[-1]['content'])
  if 'existing_zh' in c:
   self.calls.append(c);return {'data':dict(text_zh=c['existing_zh'] or ZH,titles=[{k:t[k] for k in ('language','text','text_zh')} for t in response(c['target_language'])['titles']])}
  return super().complete(model,messages,**kw)

class ReadinessTests(unittest.TestCase):
 setUp=BatchTests.setUp
 tearDown=BatchTests.tearDown
 raw=BatchTests.raw
 state=BatchTests.state
 post=BatchTests.post
 public=BatchTests.public
 view=BatchTests.view
 payload=BatchTests.payload
 submit=BatchTests.submit
 scripts=BatchTests.scripts
 rows=BatchTests.rows
 def use(self,text=BR,**extra):return self.post('script/draft',dict(mode='PASTED',text=text,**extra))
 def test_01_bilingual_editor_notes_ready_and_replaceable(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=['Use a more natural word'],copy={},script_candidates=[],title_candidates=[],script_status=None)
  before=self.raw();b=self.view();self.assertTrue(b['items'][0]['script_ready']);self.assertEqual(b['script_ready_count'],3);self.assertEqual(self.public()['items'][0]['allowed_actions'],['CHANGE_SCRIPT']);self.assertEqual(self.raw(),before);self.assertEqual(self.adapter.calls,[])
 def test_02_missing_script_still_blocks_entire_batch(self):
  self.store.item(self.id,3,status='REVIEW_REQUIRED',issues=['INVALID_DEEPSEEK_CONTEXT'],final_script=None,localized_script=None,copy={},script_candidates=[])
  before=self.rows();self.assertEqual(self.submit('scripts').status_code,400);self.assertEqual(self.rows(),before);self.assertEqual(self.view()['items'][2]['script_error'],'暂无可用口播，请更换脚本');self.assertEqual(self.public()['items'][2]['allowed_actions'],['CHANGE_SCRIPT'])
 def test_03_bad_language_not_marked_ready(self):
  self.store.item(self.id,1,final_script='This bag is so easy to use with your clothes.',final_script_language='pt-BR');self.assertFalse(self.view()['items'][0]['script_ready']);self.assertEqual(self.submit('scripts').status_code,400)
 def test_04_missing_chinese_not_marked_ready(self):
  self.store.item(self.id,1,final_script_zh='');self.assertFalse(self.view()['items'][0]['script_ready']);self.assertEqual(self.submit('scripts').status_code,400)
 def test_05_inflight_keeps_old_script_unready_and_uneditable(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,1,status='SCRIPTING',operation_inflight=True);self.assertFalse(self.view()['items'][0]['script_ready']);self.assertEqual(self.state()['allowed_actions'],[]);self.assertEqual(self.use().json()['error'],'ITEM_BUSY')
 def test_06_default_br_original_and_paragraphs_not_rewritten(self):
  text=BR+'\n\nVeja este detalhe!';r=self.use(text);self.assertEqual(r.status_code,200,r.text);d=self.raw()['script_draft'];self.assertEqual(d['candidates'][0]['text'],text);self.assertEqual(d['original_language'],'pt-BR');self.assertEqual(len(self.adapter.calls),1);self.assertIn('items',self.adapter.calls[0]);self.assertEqual(self.queue.enqueued,[]);self.assertTrue(self.view()['items'][0]['script_ready'])
 def test_07_default_mx_keeps_spanish_original(self):
  self.store.update(self.id,market='Mexico');j=self.store.get(self.id);r=response('es-MX');self.store.item(self.id,1,copy=r,title_candidates=r['titles'],**script_fields(r,j));out=self.use(MX+' Mira ahora.');self.assertEqual(out.status_code,200,out.text);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text'],MX+' Mira ahora.');self.assertEqual(self.raw()['script_draft']['candidates'][0]['language'],'es-MX');self.assertIn('items',self.adapter.calls[0])
 def test_08_cached_chinese_zero_requests(self):
  self.app.state.translations.cache.put(BR,'pt-BR',job_models(self.store.get(self.id))['deepseek'],ZH);r=self.use();self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text_zh'],ZH)
 def test_09_default_does_not_allow_actual_english_through(self):
  text='This buckle is easy to use with your clothes.';r=self.use(text);self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text'],BR);self.assertEqual(self.raw()['script_draft']['original_text'],text);self.assertEqual(self.adapter.calls[0]['input_language'],'en')
 def test_10_default_does_not_allow_chinese_through(self):
  r=self.use('这个带扣搭配衣服很方便。');self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text'],BR);self.assertNotIn(ZH,self.raw()['script_draft']['candidates'][0]['text'])
 def test_11_table_with_chinese_used_without_provider(self):
  p=self.root/'table.csv';p.write_text('编号,葡语文案,中文翻译\nW4,"'+BR+'","'+ZH+'"\n');self.c.post('/native/grant',json={'path':str(p)});info=self.c.post('/references/inspect',json={'path':str(p)}).json();row=info['scripts']['Brazil'][0]
  r=self.post('script/draft',dict(mode='TABLE',reference_path=str(p),script_reference_id=row['id']));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text'],BR)
 def test_12_missing_titles_and_zh_one_metadata_request(self):
  a=DirectAdapter();self.app.state.translations.factory=lambda:a;self.store.item(self.id,1,copy={},title_candidates=[],script_candidates=[]);r=self.use();self.assertEqual(r.status_code,200,r.text);d=self.raw()['script_draft'];self.assertEqual(d['candidates'][0]['text'],BR);self.assertEqual(len(d['titles']),3);self.assertEqual(len(a.calls),1);self.assertIn('existing_zh',a.calls[0]);self.assertNotIn('mode',a.calls[0])
 def test_13_translation_failure_retains_current_script(self):
  self.adapter.complete=MagicMock(side_effect=RuntimeError('fixture'));before=self.raw();self.assertEqual(self.use(BR+' Veja este detalhe.').status_code,400);self.assertEqual(self.raw()['final_script'],before['final_script']);self.assertFalse(self.raw()['operation_inflight']);self.assertNotIn('script_draft',self.raw());self.assertEqual(self.queue.enqueued,[])
 def test_14_repeated_paste_reuses_cache_no_extra_requests(self):
  self.assertEqual(self.use(BR+' Veja este detalhe.').status_code,200);self.assertEqual(self.use(BR+' Veja este detalhe.').status_code,200);self.assertEqual(len(self.adapter.calls),1);self.assertEqual(self.queue.enqueued,[])
 def test_15_legacy_cached_titles_restore_without_ai(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',copy={},title_candidates=[],script_candidates=[],script_status=None,issues=['Wording suggestion'])
  p=self.store.home/'jobs'/self.id/'work/scripts/copy_results.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'failed':{'error':'INVALID_DEEPSEEK_CONTEXT'},'matching':{'response':response()}}))
  self.scripts();self.assertEqual(len(self.raw()['title_candidates']),3);self.assertEqual(self.raw()['final_script'],BR);self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.queue.enqueued,[]);self.assertEqual(self.view()['phase'],'TITLES')
 def test_16_legacy_other_script_cache_never_used(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',copy={},title_candidates=[],script_candidates=[]);r=response();r['localized_script']['text']+=' Veja agora.';p=self.store.home/'jobs'/self.id/'work/scripts/copy_results.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'other':{'response':r}}));self.scripts();self.assertEqual(self.raw()['title_candidates'],[]);self.assertFalse(self.view()['all_titles_ready'])
 def test_17_soft_editor_veto_new_preparation_still_ready(self):
  self.store.update(self.id,source_count=1,content_confirmation_required=True,script_mode='AUTO',title_approval_mode='auto')
  with self.store.connect() as db:db.execute("DELETE FROM job_items WHERE job_id=? AND rank!='01'",(self.id,))
  media=dict(self.raw()['source_snapshot'],filename='source.mp4',width=1080,height=1920,fps=30,status='VALID');m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h;a=MockAdapter()
  dec=dict(DEC,copy_approved=False,issues=['Use a more natural word'])
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=h),patch('autocut.pipeline.scan',return_value=[media]),patch('autocut.pipeline.asr_job',return_value=future({'text':'Look at this compact buckle','language':'en','segments':[]})),patch('autocut.pipeline.source_sheet',return_value='sheet.jpg'),patch.object(m,'judge',side_effect=lambda *args:copy.deepcopy(ANALYSIS if args[5]=='analyze' else dec)),patch.object(m,'copy_batch',side_effect=lambda rows,work,model:generate_batch(rows,work,lambda:a,model=model)),patch('autocut.pipeline.tts_job') as tts:
   m.run(self.id);tts.assert_not_called()
  m.queue.shutdown();self.assertTrue(self.view()['all_scripts_ready']);self.assertEqual(len(self.raw()['title_candidates']),3);self.assertEqual(self.raw()['decision']['issues'],dec['issues']);self.assertNotEqual(self.raw()['status'],'PASS')
 def test_18_confirmed_original_not_vetoed_again_before_tts(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',decision=None,issues=['Wording suggestion']);self.scripts();self.assertEqual(self.submit('titles').status_code,200)
  m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h
  def stop(h,payload,**kw):self.assertEqual(payload['tts_text'],BR);self.assertNotIn(ZH,json.dumps(payload,ensure_ascii=False));raise ValueError('FIXTURE_AFTER_TTS_PAYLOAD')
  with patch('autocut.item_pipeline.Harness',return_value=h),patch.object(m,'judge',return_value=dict(DEC,copy_approved=False,issues=['Wording suggestion'])),patch.object(m,'catalog',return_value={'voices':[{'id':'v','name':'Carla'}],'scope':'fixture'}),patch('autocut.item_pipeline.tts_payload',return_value=({'tts_text':BR},None)),patch('autocut.item_pipeline.tts_job',side_effect=stop) as tts:
   resume_item(m,self.id,1,'TITLE');tts.assert_called_once()
  m.queue.shutdown();self.assertNotIn('COPY_REVIEW_REQUIRED',self.raw()['issues']);self.assertNotIn(self.raw()['status'],['PASS','USER_APPROVED'])
 def test_19_replacement_stale_action_still_rejected(self):
  token=self.state()['action_token'];self.use();self.assertEqual(self.use(action_token=token).status_code,409)
 def test_20_ready_is_not_approved_no_tts_until_titles(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=['Wording suggestion']);self.assertTrue(self.view()['all_scripts_ready']);self.assertNotIn('SCRIPT_APPROVED',checkpoints(self.store,self.id,1));self.scripts();self.assertEqual(self.queue.enqueued,[]);self.assertNotIn('FINAL_APPROVED',checkpoints(self.store,self.id,1));self.assertEqual(self.raw()['status'],'WAITING_TITLE_APPROVAL')
 def test_21_music_exempt_and_still_no_production(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',voiceover_mode='MUSIC_ONLY',music={'path':'fixture.wav'},final_script=None,final_script_zh='',issues=[]);self.assertTrue(self.view()['items'][0]['script_ready']);self.scripts();self.assertEqual(self.queue.enqueued,[])
 def test_22_restart_schema_and_get_readonly(self):
  self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=['Wording suggestion']);before=self.store.get(self.id);self.public();self.assertEqual(self.store.get(self.id),before);s=Store(self.store.home);self.assertEqual(s.get(self.id),before)
  with s.connect() as db:self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)

 def test_23_existing_snapshot_chinese_reused_without_requests(self):
  r=self.use();self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text'],BR);self.assertEqual(self.raw()['script_draft']['candidates'][0]['text_zh'],self.raw()['final_script_zh'])
