"""0.1.12: real SQLite/API and mocked paid services, no video pipeline run."""
import copy,json,threading,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from test_ownership import OwnershipTests
from test_copy_presentation import response,BR,MX,ZH
from test_review_flow import CopyAdapter,ANALYSIS,DEC,future
from autocut.ownership import checkpoints,item_state
from autocut.content_confirmation import content_view
from autocut.script_language import script_fields
from autocut.pipeline import Manager
from autocut.item_pipeline import resume_item
from autocut.storage import Store

class TitleAdapter(CopyAdapter):
 def complete(self,model,messages,**kw):
  c=json.loads(messages[-1]['content'])
  if 'generation' not in c:return super().complete(model,messages,**kw)
  self.calls.append(c);lang=c['target_language'];suffix=' Veja agora' if lang=='pt-BR' else ' Mira ahora'
  rows=[dict(text=t['text']+suffix+(' · Para você' if lang=='pt-BR' else ' · Para ti') if c['generation']%2==0 else t['text']+suffix,text_zh=t['text_zh']+'，现在看看',language=lang) for t in response(lang)['titles']]
  return dict(data={'title':rows[0]} if 'input_title' in c else {'titles':rows})

class ContentTests(unittest.TestCase):
 setUp=OwnershipTests.setUp
 tearDown=OwnershipTests.tearDown
 raw=OwnershipTests.raw
 state=OwnershipTests.state
 post=OwnershipTests.post
 final_fixture=OwnershipTests.final_fixture
 def view(self):return self.c.get('/jobs/'+self.id).json()['items'][0]
 def adapter(self):
  a=TitleAdapter();self.app.state.translations.factory=lambda:a;return a
 def confirm(self,title=1):return self.post('content/approve',dict(title_id=title))
 def test_01_best_bilingual_script_and_three_titles_A_default(self):
  c=self.view()['content_confirmation'];self.assertEqual(c['script']['text'],BR);self.assertTrue(c['script']['text_zh']);self.assertEqual(len(c['titles']),3);self.assertEqual(c['default_title_id'],1)
 def test_02_atomic_both_approvals_not_pass(self):
  r=self.confirm(2);self.assertEqual(r.status_code,200,r.text);x=self.raw();cp=checkpoints(self.store,self.id,1)
  self.assertTrue({'SCRIPT_APPROVED','TITLE_APPROVED'}<=cp.keys());self.assertNotIn('FINAL_APPROVED',cp);self.assertEqual(x['status'],'QUEUED');self.assertEqual(x['selected_title_id'],2);self.assertEqual(x['title'],self.copy['titles'][1]['text']);self.assertEqual(len(self.queue.enqueued),1)
 def test_03_invalid_selection_no_partial_write(self):
  before=self.raw();self.assertEqual(self.confirm(99).status_code,400);self.assertEqual(self.raw(),before);self.assertNotIn('SCRIPT_APPROVED',checkpoints(self.store,self.id,1));self.assertEqual(self.queue.enqueued,[])
 def test_04_script_guard_no_partial_approval(self):
  self.store.item(self.id,1,final_script='This is an English script with your product.',final_script_language='en');self.assertEqual(self.confirm().status_code,400);self.assertNotIn('TITLE_APPROVED',checkpoints(self.store,self.id,1));self.assertEqual(self.queue.enqueued,[])
 def test_05_title_guard(self):
  self.store.item(self.id,1,content_titles=[dict(id=1,text='This is your new product',language='en',text_zh=ZH)]);self.assertEqual(self.confirm().status_code,400);self.assertEqual(self.queue.enqueued,[])
 def test_06_missing_zh_blocks(self):
  self.store.item(self.id,1,final_script_zh='');self.assertEqual(self.confirm().json()['error'],'CONTENT_TRANSLATION_REQUIRED')
 def test_07_stale_action_rejected(self):
  token=self.state()['action_token'];self.post('content/titles',{},token=True);self.store.item(self.id,1,selected_title_id=2)
  self.assertEqual(self.post('content/approve',dict(title_id=1,action_token=token)).status_code,409)
 def test_08_system_no_actions_and_early_approve_rejected(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,1,status='ANALYZING');self.assertEqual(self.view()['ui_status'],'处理中');self.assertEqual(self.view()['allowed_actions'],[]);self.assertEqual(self.confirm().status_code,409);self.assertEqual(self.post('review/accept').status_code,409)
 def test_09_refresh_one_request_current_item_only(self):
  a=self.adapter();self.store.item(self.id,2,status='VOICE',copy=self.copy);other=self.store.get(self.id)['items'][1];before=self.raw();r=self.post('content/titles');self.assertEqual(r.status_code,200,r.text)
  after=self.raw();self.assertEqual(after['copy'],before['copy']);self.assertEqual(after['final_script'],BR);self.assertEqual(len(after['content_titles']),3);self.assertNotEqual(after['content_titles'][0]['text'],before['title_candidates'][0]['text']);self.assertEqual(after['title_generation'],1);self.assertEqual(len(a.calls),1);self.assertEqual(self.store.get(self.id)['items'][1],other);self.assertEqual(self.queue.enqueued,[])
 def test_10_refresh_failure_keeps_existing(self):
  before=self.raw();self.app.state.content.title_request=MagicMock(side_effect=RuntimeError('fixture'));r=self.post('content/titles');self.assertEqual(r.json()['error'],'TITLE_GENERATION_FAILED');self.assertEqual(self.raw()['title_candidates'],before['title_candidates']);self.assertNotIn('content_titles',self.raw());self.assertIn('APPROVE_CONTENT',self.state()['allowed_actions'])
 def test_11_concurrent_refresh_is_single_request(self):
  entered=threading.Event();release=threading.Event();a=self.adapter();original=self.app.state.content.title_request
  def blocked(*args):entered.set();release.wait(5);return original(*args)
  self.app.state.content.title_request=blocked;token=self.state()['action_token'];out=[]
  t=threading.Thread(target=lambda:out.append(self.post('content/titles',dict(action_token=token))));t.start();self.assertTrue(entered.wait(3))
  try:self.assertEqual(self.post('content/titles',dict(action_token=token)).status_code,409);self.assertEqual(self.view()['allowed_actions'],[])
  finally:release.set();t.join(5)
  self.assertEqual(out[0].status_code,200);self.assertEqual(len(a.calls),1)
 def test_12_custom_target_text_retained(self):
  self.adapter();text='Olha esta fivela para sua roupa';r=self.post('content/custom-title',dict(text=text));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.raw()['content_custom_title']['text'],text);self.assertEqual(self.confirm(4).status_code,200);self.assertEqual(self.raw()['title'],text)
 def test_13_custom_chinese_mexico_localized_and_isolated(self):
  self.adapter();r=response('es-MX');self.store.update(self.id,market='Mexico');j=self.store.get(self.id);self.store.item(self.id,1,copy=r,title_candidates=r['titles'],**script_fields(r,j))
  out=self.post('content/custom-title',dict(text='看看这个带扣'));self.assertEqual(out.status_code,200,out.text);self.assertEqual(self.raw()['content_custom_title']['language'],'es-MX');self.assertEqual(self.confirm(4).status_code,200);self.assertEqual(self.raw()['final_title']['language'],'es-MX');self.assertNotIn('original_text',self.raw()['copy']['titles'][-1])
 def test_14_paste_replaces_pending_script_no_queue_then_one_confirm(self):
  text='Olha esta fivela! Fica linda.';r=self.post('script/draft',dict(mode='PASTED',text=text,language='pt-BR'));self.assertEqual(r.status_code,200,r.text);c=self.view()['content_confirmation'];self.assertEqual(c['script']['text'],text);self.assertEqual(self.queue.enqueued,[]);self.assertEqual(self.confirm().status_code,200);self.assertEqual(self.raw()['final_script'],text)
 def test_15_table_reuses_chinese_snapshot(self):
  a=self.adapter();ref={'filename':'scripts.csv','rows':[{'编号':'W4','pt-BR':BR,'中文':ZH}]}
  from autocut.workbench import table_scripts
  # The importer already exposes canonical records; use a real parsed fixture.
  p=self.root/'table.csv';p.write_text('编号,葡语文案,中文翻译\nW4,"'+BR+'","'+ZH+'"\n');self.c.post('/native/grant',json={'path':str(p)})
  info=self.c.post('/references/inspect',json={'path':str(p)}).json();row=info['scripts']['Brazil'][0]
  out=self.post('script/draft',dict(mode='TABLE',reference_path=str(p),script_reference_id=row['id']));self.assertEqual(out.status_code,200,out.text);self.assertEqual(a.calls,[]);self.assertEqual(self.view()['content_confirmation']['source']['type'],'TABLE');self.assertEqual(self.confirm().status_code,200)
 def test_16_selling_default_recommended_returns_same_content(self):
  self.adapter();r=self.post('script/draft',dict(mode='SELLING_POINTS'));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.view()['content_confirmation']['script']['id'],'1');self.assertEqual(len(self.view()['content_confirmation']['titles']),3);self.assertEqual(self.queue.enqueued,[]);self.assertEqual(self.confirm().status_code,200)
 def test_17_music_still_requires_title_only(self):
  d=dict(id='music',mode='MUSIC_ONLY',source={'type':'MUSIC_ONLY'},music={'path':'fixture.wav'},original_text='',original_language='none',candidates=[],titles=self.copy['titles'],base_revision=0)
  self.store.item(self.id,1,script_draft=d);self.assertEqual(self.view()['content_confirmation']['voiceover_mode'],'MUSIC_ONLY');self.assertEqual(self.confirm(3).status_code,200);self.assertIsNone(self.raw()['final_script']);self.assertEqual(self.raw()['voiceover_mode'],'MUSIC_ONLY');self.assertEqual(self.raw()['selected_title_id'],3)
 def test_18_completed_title_only_edit_keeps_tts_upstream(self):
  output=self.final_fixture();self.post('approve-final');before=self.raw();self.store.done(self.id,'ASR_DONE',1,'keep',{});self.store.done(self.id,'SUBTITLE',1,'keep',{})
  self.assertEqual(self.post('content/begin').status_code,200);self.assertTrue(output.exists());self.assertEqual(self.confirm(2).status_code,200);cp=checkpoints(self.store,self.id,1)
  self.assertTrue({'TTS_DONE','SUBTITLE','ASR_DONE'}<=cp.keys());self.assertFalse({'FINAL_RENDERED','QA_DONE','FINAL_APPROVED'}&cp.keys());self.assertEqual(self.raw()['audio_result'],before['audio_result']);self.assertTrue(output.exists());self.assertEqual(self.queue.enqueued[-1][-1],'TITLE')
 def test_19_completed_script_edit_only_current_downstream(self):
  output=self.final_fixture();self.post('approve-final');self.store.item(self.id,2,status='VOICE');other=self.store.get(self.id)['items'][1]
  for stage in ['ASR_DONE','VIDEO_ANALYSIS','ALIGNMENT','SUBTITLE']:self.store.done(self.id,stage,1,'keep',{})
  self.post('content/begin');d=self.post('script/draft',dict(mode='PASTED',text='Olha esta fivela! Fica linda.',language='pt-BR',confirmed_rework=True));self.assertEqual(d.status_code,200,d.text);self.assertEqual(self.confirm().status_code,200);cp=checkpoints(self.store,self.id,1)
  self.assertTrue({'ASR_DONE','VIDEO_ANALYSIS'}<=cp.keys());self.assertFalse({'TTS_DONE','ALIGNMENT','SUBTITLE','FINAL_RENDERED','QA_DONE'}&cp.keys());self.assertIsNone(self.raw()['audio_result']);self.assertEqual(self.store.get(self.id)['items'][1],other);self.assertTrue(output.exists())
 def test_20_exception_separate_from_content(self):
  self.store.item(self.id,1,status='WAITING_SUPPLEMENT_MEDIA',issues=['VISUAL_DURATION_INSUFFICIENT']);x=self.view();self.assertEqual(x['ui_status'],'需要处理');self.assertIn('ADD_SUPPLEMENT_MEDIA',x['allowed_actions']);self.assertNotIn('APPROVE_CONTENT',x['allowed_actions'])
 def test_21_counts_pending_and_processing(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,2,status='VOICE');j=self.c.get('/jobs/'+self.id).json();self.assertEqual(j['content_counts'],dict(processing=1,pending=1,completed=0,exception=0))
 def test_22_readonly_legacy_and_restart_schema3(self):
  before=self.store.get(self.id);self.view();self.assertEqual(before,self.store.get(self.id));s=Store(self.store.home)
  with s.connect() as db:self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)
  self.assertEqual(s.get(self.id)['items'][0],self.raw())
 def test_23_no_second_gate_after_confirmation_real_resume_mock_tts(self):
  self.assertEqual(self.confirm().status_code,200);m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h
  def stop_tts(h,payload,**kw):self.assertEqual(payload['tts_text'],BR);self.assertNotIn(ZH,json.dumps(payload,ensure_ascii=False));raise ValueError('FIXTURE_AFTER_TTS_REQUEST')
  with patch('autocut.item_pipeline.Harness',return_value=h),patch.object(m,'catalog',return_value={'voices':[{'id':'v','name':'Carla'}],'scope':'fixture'}),patch('autocut.item_pipeline.tts_payload',return_value=({'tts_text':BR},None)),patch('autocut.item_pipeline.tts_job',side_effect=stop_tts) as tts,patch.object(m,'judge') as analysis:
   resume_item(m,self.id,'01','TITLE');tts.assert_called_once();analysis.assert_not_called()
  m.queue.shutdown();self.assertNotIn(self.raw()['status'],['PASS','USER_APPROVED'])
 def test_24_content_required_legacy_single_approval_cannot_skip(self):
  self.store.update(self.id,content_confirmation_required=True);self.store.item(self.id,1,script_status='USER_APPROVED',status='QUEUED');m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event()
  with patch('autocut.item_pipeline.tts_job') as tts:resume_item(m,self.id,1,'SCRIPT');tts.assert_not_called()
  self.assertEqual(self.raw()['status'],'WAITING_SCRIPT_APPROVAL');m.queue.shutdown()
 def test_25_ui_one_confirm_and_four_replacements(self):
  ui=''.join(Path('web/'+n).read_text() for n in ['app.js','review.js','content.js'])
  for old in ['等待确认脚本','等待确认标题','>确认脚本<','>确认标题<','>确认成片<','Owner SYSTEM','Owner USER']:self.assertNotIn(old,ui)
  for t in ['确认内容并继续','从文案表选择','直接粘贴文案','纯音乐','根据卖点自动生成','重新生成3个标题','自己填写']:self.assertIn(t,ui)
 def test_26_default_manual_content_reuses_single_batch_script(self):
  from autocut.workbench import approval_contexts
  rows=[{'script_group_id':'x','source_script':'source'}];self.assertEqual(approval_contexts(rows,{'script_mode':'REQUIRE_APPROVAL'}),rows)
 def test_27_deleted_no_content_actions(self):
  self.c.post('/jobs/'+self.id+'/delete');self.assertEqual(self.view()['allowed_actions'],[]);self.assertEqual(self.confirm().status_code,400)
 def test_28_refresh_version_and_snapshot_persist(self):
  self.adapter();self.post('content/titles');self.post('content/titles');self.assertEqual(self.raw()['title_generation'],2);before=self.raw()['content_titles'];s=Store(self.store.home);self.assertEqual(s.get(self.id)['items'][0]['content_titles'],before)
 def test_29_failure_does_not_erase_successful_refresh(self):
  self.adapter();self.post('content/titles');before=self.raw()['content_titles'];self.app.state.content.title_request=MagicMock(side_effect=ValueError('fixture'));self.post('content/titles');self.assertEqual(self.raw()['content_titles'],before);self.assertEqual(self.raw()['title_generation'],1)
 def test_30_double_confirm_only_one_resume(self):
  token=self.state()['action_token'];self.confirm();r=self.post('content/approve',dict(title_id=2,action_token=token));self.assertEqual(r.status_code,409);self.assertEqual(len(self.queue.enqueued),1)

 def test_31_new_job_initial_content_gate_before_any_tts(self):
  from test_copy_presentation import MockAdapter
  from app.s3.deepseek_batch import generate_batch
  self.store.update(self.id,content_confirmation_required=True,script_mode='AUTO',title_approval_mode='auto')
  media=dict(self.raw()['source_snapshot'],filename='source.mp4',width=1080,height=1920,fps=30,status='VALID')
  m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h;a=MockAdapter();contexts=[]
  def copies(rows,work,model):contexts.extend(rows);return generate_batch(rows,work,lambda:a,model=model)
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=h),patch('autocut.pipeline.scan',return_value=[media]),patch('autocut.pipeline.asr_job',return_value=future({'text':'Look at this compact buckle','language':'en','segments':[]})),patch('autocut.pipeline.source_sheet',return_value='sheet.jpg'),patch.object(m,'judge',side_effect=lambda *args:copy.deepcopy(ANALYSIS if args[5]=='analyze' else DEC)),patch.object(m,'copy_batch',side_effect=copies),patch.object(m,'catalog') as cat,patch('autocut.pipeline.tts_job') as tts:
   m.run(self.id);tts.assert_not_called();cat.assert_not_called()
  m.queue.shutdown();x=self.view();self.assertEqual(x['ui_status'],'待确认');self.assertEqual(len(x['content_confirmation']['titles']),3);self.assertEqual(len(contexts),1);self.assertEqual(len(a.calls),1)

 def test_32_cache_write_failure_preserves_title_generation(self):
  self.adapter();self.post('content/titles');before=self.raw()['content_titles'];generation=self.raw()['title_generation']
  with patch.object(self.app.state.translations.cache,'put',side_effect=OSError('fixture disk failure')):r=self.post('content/titles')
  self.assertEqual(r.json()['error'],'TITLE_GENERATION_FAILED');self.assertEqual(self.raw()['content_titles'],before);self.assertEqual(self.raw()['title_generation'],generation);self.assertIn('APPROVE_CONTENT',self.state()['allowed_actions'])
