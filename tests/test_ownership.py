"""0.1.11: isolated actions, real SQLite + ffprobe, no paid providers or rendering."""
import copy,json,os,shutil,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from autocut.ownership import item_state,checkpoints,final_ready,require_action
from autocut.pipeline import Manager
from autocut.workbench import copy_candidates
from autocut.script_language import script_fields
from test_copy_presentation import response,BR
from test_review_flow import CopyAdapter,ANALYSIS,DEC,Queue
from app.s3.headless_runtime import sha,write

class StateQueue(Queue):
 def __init__(self,store):super().__init__();self.store=store
 def enqueue_item(self,id,rank,phase='SCRIPT'):
  from autocut.ownership import invalidate_final
  self.enqueued.append((id,str(rank),phase));invalidate_final(self.store,id,rank);self.store.item(id,rank,status='QUEUED',resume_phase=phase)

class OwnershipTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data');self.queue=StateQueue(self.store);self.app=create_app(self.store,None,MagicMock(),self.queue,'fixture');self.app.state.translations.factory=CopyAdapter;self.c=TestClient(self.app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer fixture'})
  self.job,_=self.store.create(dict(request_id='one',input_path=str(self.root),product_name='状态验收',product_profile_id='general',market='Brazil',mode='Transport',script_mode='REQUIRE_APPROVAL',title_approval_mode='manual',watermark_mode='handled'));self.id=self.job['id'];self.store.update(self.id,state='WAITING_SCRIPT_APPROVAL');self.source=self.root/'source.mp4';self.source.write_bytes(b'fixture source');self.copy=response();self.candidates=copy_candidates({'g':{'response':self.copy}},'g');self.store.item(self.id,1,status='WAITING_SCRIPT_APPROVAL',filename='one.mp4',copy=self.copy,script_candidates=self.candidates,title_candidates=self.copy['titles'],selected_title_id=1,script_status='WAITING_APPROVAL',source_path=str(self.source),source_snapshot=dict(path=str(self.source),sha256=sha(self.source),duration=23,rank=1),analysis=ANALYSIS,decision=DEC,**script_fields(self.copy,self.job))
 def tearDown(self):self.c.close();self.tmp.cleanup()
 def raw(self):return self.store.get(self.id)['items'][0]
 def state(self):return item_state(self.store,self.store.get(self.id),self.raw())
 def post(self,route,body=None,token=True):
  body=dict(body or {})
  if token:body.setdefault('action_token',self.state()['action_token'])
  return self.c.post('/jobs/'+self.id+'/items/01/'+route,json=body)
 def final_fixture(self,music=False):
  output=self.root/'final.mp4';shutil.copy2('build/review019/smoke/music_supplement_smoke.mp4',output)
  audio=self.root/'voice.wav';audio.write_bytes(b'fixture audio');a=dict(path=str(audio),sha256=sha(audio),script_text=BR,technical_status='PASS',duration=6)
  plan=dict(output=str(output),final_script=None if music else BR,final_script_language='pt-BR',audio=a,visual={'subtitle':{'captions':[] if music else [{'text':BR}]}},voiceover_mode='MUSIC_ONLY' if music else 'VOICEOVER')
  path=self.root/'plan.json';write(path,plan);self.store.item(self.id,1,status='CHECKING',output=str(output),plan=str(path),audio_result=a,script_status='USER_APPROVED',voiceover_mode=plan['voiceover_mode'],issues=[])
  for stage,data in [('TTS_DONE',a),('FINAL_RENDERED',dict(output=str(output),output_sha256=sha(output))),('QA_DONE',dict(status='REVIEW_REQUIRED',issues=['TEXT_GHOSTING']))]:self.store.done(self.id,stage,1,'same-render',data)
  self.store.item(self.id,1,status='WAITING_FINAL_REVIEW');return output
 def test_01_early_asr_old_accept_rejected(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,1,status='ANALYZING',copy=None,final_script=None);self.store.done(self.id,'ASR_DONE',1,'source',{})
  r=self.post('review/accept');self.assertEqual(r.status_code,409);self.assertEqual(self.raw()['status'],'ANALYZING');self.assertEqual(self.queue.enqueued,[])
  self.assertEqual(self.state()['current_owner'],'SYSTEM');self.assertEqual(self.state()['allowed_actions'],[])
 def test_02_script_only_then_system(self):
  r=self.c.post('/jobs/'+self.id+'/approve-scripts',json=dict(selections={'01':self.candidates[0]['id']},action_tokens={'01':self.state()['action_token']}));self.assertEqual(r.status_code,200,r.text)
  self.assertEqual(self.raw()['script_status'],'USER_APPROVED');self.assertEqual(self.raw()['status'],'QUEUED');self.assertIn('SCRIPT_APPROVED',checkpoints(self.store,self.id,1));self.assertNotIn('FINAL_APPROVED',checkpoints(self.store,self.id,1));self.assertEqual(self.state()['current_owner'],'SYSTEM')
 def test_03_title_only(self):
  self.store.item(self.id,1,status='WAITING_TITLE_APPROVAL');r=self.post('approve-title',{'candidate':1});self.assertEqual(r.status_code,200,r.text);self.assertIn('TITLE_APPROVED',checkpoints(self.store,self.id,1));self.assertEqual(self.raw()['status'],'QUEUED')
 def test_04_overlay_only(self):
  image=self.root/'preview.jpg';image.write_bytes(b'preview');self.store.update(self.id,state='REVIEW_REQUIRED');self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=['TEXT_GHOSTING'],source_sheet=str(image));r=self.post('approve-overlay');self.assertEqual(r.status_code,200,r.text);self.assertIn('OVERLAY_APPROVED',checkpoints(self.store,self.id,1));self.assertEqual(self.raw()['status'],'QUEUED');self.assertNotIn('FINAL_APPROVED',checkpoints(self.store,self.id,1))
 def test_05_final_complete_only(self):
  self.final_fixture();self.assertIn('APPROVE_FINAL',self.state()['allowed_actions']);r=self.post('approve-final');self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.raw()['status'],'PASS');self.assertEqual(self.queue.enqueued,[]);self.assertIn('FINAL_APPROVED',checkpoints(self.store,self.id,1))
 def test_06_missing_qa_blocks(self):
  self.final_fixture()
  with self.store.connect() as d:d.execute("DELETE FROM checkpoints WHERE stage='QA_DONE'")
  self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions']);self.assertEqual(self.post('approve-final').status_code,409)
 def test_07_missing_render_blocks(self):
  self.final_fixture()
  with self.store.connect() as d:d.execute("DELETE FROM checkpoints WHERE stage='FINAL_RENDERED'")
  self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_08_missing_output_blocks(self):
  self.final_fixture().unlink();self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_09_invalid_ffprobe_blocks(self):
  output=self.final_fixture();output.write_bytes(b'not a video');self.store.done(self.id,'FINAL_RENDERED',1,'same-render',dict(output=str(output),output_sha256=sha(output)));self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_10_tts_or_subtitle_missing_blocks(self):
  self.final_fixture();self.store.item(self.id,1,audio_result={'technical_status':'FAIL'});self.assertFalse(final_ready(self.store,self.store.get(self.id),self.raw()))
 def test_11_music_branch_exempt(self):
  self.final_fixture(music=True);self.store.item(self.id,1,final_script=None,copy={'titles':self.copy['titles']});self.assertTrue(final_ready(self.store,self.store.get(self.id),self.raw()))
 def test_12_stale_token(self):
  old=self.state()['action_token'];self.store.item(self.id,1,selected_title_id=2);r=self.post('approve-title',{'candidate':1,'action_token':old});self.assertEqual(r.json()['error'],'STALE_ACTION');self.assertEqual(self.queue.enqueued,[])
 def test_13_system_actions_hidden_all_phases(self):
  self.store.update(self.id,state='RUNNING')
  for status in ['ANALYZING','ASR','LOCALIZING','SCRIPTING','VOICE','EDITING','OVERLAY','RENDERING','CHECKING']:
   self.store.item(self.id,1,status=status);self.assertEqual(self.state()['current_owner'],'SYSTEM',status);self.assertEqual(self.state()['allowed_actions'],[],status)
 def test_14_waiting_nodes(self):
  for status,action,label in [('WAITING_SCRIPT_APPROVAL','APPROVE_SCRIPT','等待确认脚本'),('WAITING_TITLE_APPROVAL','APPROVE_TITLE','等待确认标题'),('WAITING_SUPPLEMENT_MEDIA','ADD_SUPPLEMENT_MEDIA','等待补充画面')]:
   self.store.item(self.id,1,status=status);self.assertEqual(self.state()['user_action_status'],action);self.assertEqual(self.state()['state_title'],label)
 def test_15_get_never_changes_state(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,1,status='VOICE');before=self.store.get(self.id)
  for _ in range(3):self.c.get('/jobs/'+self.id);self.c.get('/jobs')
  self.assertEqual(before,self.store.get(self.id));self.assertEqual(self.queue.enqueued,[])
 def test_16_mixed_owner_counts(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,2,status='VOICE');r=self.c.get('/jobs/'+self.id).json();self.assertEqual(r['ownership_counts'],dict(processing=1,waiting=1,completed=0));self.assertTrue(r['has_user_actions']);self.assertEqual(r['current_owner'],'USER')
 def test_17_other_item_keeps_processing(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,2,status='VOICE');before=self.store.get(self.id)['items'][1]
  r=self.c.post('/jobs/'+self.id+'/approve-scripts',json=dict(selections={'01':self.candidates[0]['id']},action_tokens={'01':self.state()['action_token']}));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.store.get(self.id)['items'][1],before);self.assertEqual(len(self.queue.enqueued),1)
 def test_18_store_forbids_direct_early_pass(self):
  for state in ['PASS','USER_APPROVED']:
   with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):self.store.item(self.id,1,status=state)
  self.assertEqual(self.raw()['status'],'WAITING_SCRIPT_APPROVAL')
 def test_19_legacy_false_pass_readonly(self):
  with self.store.connect() as d:
   x=self.raw();x['status']='PASS';d.execute('UPDATE job_items SET data=? WHERE job_id=?',(json.dumps(x),self.id))
  before=self.raw();r=self.c.get('/jobs/'+self.id).json();self.assertNotEqual(r['items'][0]['pipeline_status'],'COMPLETED');self.assertNotIn('APPROVE_FINAL',r['items'][0]['allowed_actions']);self.assertEqual(before,self.raw())
 def test_20_restart_schema_preserved(self):
  again=Store(self.store.home);self.assertEqual(again.get(self.id)['items'][0],self.raw())
  with again.connect() as db:self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)
 def test_21_deleted_read_only(self):
  self.c.post('/jobs/'+self.id+'/delete');r=self.c.get('/jobs/'+self.id).json();self.assertEqual(r['items'][0]['allowed_actions'],[]);self.assertFalse(r['has_user_actions'])
 def test_22_ui_no_generic_approval(self):
  ui=''.join(Path('web/'+n).read_text() for n in ['app.js','review.js','content.js'])
  for text in ['确认通过','>确认脚本<','>确认标题<','>确认成片<','等待确认脚本','等待确认标题']:self.assertNotIn(text,ui)
  for text in ['allowed_actions','待确认','确认内容并继续','无需操作']:self.assertIn(text,ui)
 def test_23_missing_token_rejected(self):self.assertEqual(self.post('approve-final',token=False).json()['error'],'STALE_ACTION')
 def test_24_revision_change_back_rejects_old_action(self):
  token=self.state()['action_token'];self.store.item(self.id,1,status='VOICE');self.store.item(self.id,1,status='WAITING_SCRIPT_APPROVAL');r=self.c.post('/jobs/'+self.id+'/approve-scripts',json={'action_tokens':{'01':token}});self.assertEqual(r.json()['error'],'STALE_ACTION')
 def test_25_replacement_script_never_pass(self):
  d=self.post('script/draft',dict(mode='PASTED',text=BR,language='pt-BR')).json();r=self.post('approve-script',dict(draft_id=d['id'],candidate='1',action_token=d['action_token']));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.raw()['status'],'QUEUED');self.assertNotIn('FINAL_RENDERED',checkpoints(self.store,self.id,1))
 def test_26_action_generation_in_progress_has_no_actions(self):
  adapter=CopyAdapter();seen=[]
  class Observe:
   def complete(obj,*a,**kw):seen.append(self.state());return adapter.complete(*a,**kw)
   def close(obj):pass
  self.app.state.translations.factory=Observe
  self.post('script/draft',dict(mode='PASTED',text=BR,language='pt-BR'));self.assertEqual(seen[0]['current_owner'],'SYSTEM');self.assertEqual(seen[0]['allowed_actions'],[])
 def test_27_final_previous_revision_not_reused(self):
  self.final_fixture();self.store.done(self.id,'QA_DONE',1,'old-render',{'status':'PASS'});self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_28_modify_final_invalidates_only_downstream(self):
  self.final_fixture();self.store.done(self.id,'ASR_DONE',1,'keep',{});r=self.post('review/continue',dict(action='MODIFY_FINAL'));self.assertEqual(r.status_code,200,r.text);cp=checkpoints(self.store,self.id,1);self.assertIn('ASR_DONE',cp);self.assertIn('TTS_DONE',cp);self.assertNotIn('FINAL_RENDERED',cp)

 def finish_fixture(self,visual='PASS',technical='PASS',manual=False):
  from test_review_flow import future
  self.final_fixture();plan=json.loads(Path(self.raw()['plan']).read_text());plan.update(rank='01',target_duration=6,title=self.copy['titles'][0]['text'],final_title_language='pt-BR',source=self.raw()['source_snapshot']);output=self.raw()['output'];preview=self.root/'preview.mp4';shutil.copy2(output,preview)
  self.store.item(self.id,1,status='EDITING');self.store.update(self.id,final_review_mode='MANUAL' if manual else 'AUTO')
  m=Manager(self.store,None,None);m.stops[self.id]=threading.Event();work=self.root/'work'
  def judge(*args):return dict(status=visual if args[5]=='review_final' else 'PASS',issues=[] if visual=='PASS' else ['TEXT_GHOSTING'],reason='Fixture QA',**{k:True for k in ['title','subtitle','cta','overlay','source_text','watermark']})
  with patch('autocut.pipeline.preflight_job',return_value=future(dict(output=str(preview),output_sha256=sha(preview),contact_sheet='fixture.jpg'))),patch('autocut.pipeline.preflight_review_context',return_value={}),patch('autocut.pipeline.approve_preflight'),patch('autocut.pipeline.identity',return_value='same-render'),patch('autocut.pipeline.final_job',return_value=future(dict(output=output,output_sha256=sha(output)))),patch('autocut.pipeline.qa_job',return_value=future(dict(status=technical,issues=[] if technical=='PASS' else ['timing'],output=output,output_sha256=sha(output)))),patch('autocut.pipeline.batch_sheets'),patch.object(m,'judge',side_effect=judge):
   m.finish_plans(self.id,MagicMock(),self.store.get(self.id),[plan],work)
  m.queue.shutdown()
 def test_29_auto_final_pass_after_qa_only(self):
  self.finish_fixture();self.assertEqual(self.raw()['status'],'PASS');self.assertEqual(self.state()['pipeline_status'],'COMPLETED')
 def test_30_visual_qa_requires_user_after_render(self):
  self.finish_fixture(visual='REVIEW_REQUIRED');self.assertEqual(self.state()['user_action_status'],'REVIEW_FINAL_VIDEO');self.assertIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_31_manual_final_mode(self):
  self.finish_fixture(manual=True);self.assertEqual(self.raw()['status'],'WAITING_FINAL_REVIEW');self.assertEqual(self.state()['confirmation_object'],'最终成片')
 def test_32_technical_check_completed_reviewable(self):
  self.finish_fixture(technical='FAIL');self.assertEqual(self.state()['qa_done'],True);self.assertEqual(self.state()['user_action_status'],'REVIEW_FINAL_VIDEO')
 def test_33_subtitle_missing_blocks_final(self):
  self.final_fixture();p=Path(self.raw()['plan']);data=json.loads(p.read_text());data['visual']['subtitle']['captions']=[];write(p,data);self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_34_script_unconfirmed_cannot_final(self):
  self.final_fixture();self.store.item(self.id,1,script_status='WAITING_APPROVAL');self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_35_completed_old_job_evidence_is_compatible(self):
  self.final_fixture();self.post('approve-final');before=self.raw();r=self.c.get('/jobs/'+self.id).json();self.assertEqual(r['ownership_counts']['completed'],1);self.assertEqual(before,self.raw())

 def test_36_restart_inflight_copy_becomes_actionable(self):
  self.store.item(self.id,1,status='SCRIPTING',operation_inflight=True);again=Store(self.store.home);j=again.get(self.id);state=item_state(again,j,j['items'][0]);self.assertEqual(state['current_owner'],'USER');self.assertFalse(j['items'][0]['operation_inflight'])
 def test_37_job_failure_cannot_look_permanently_busy(self):
  self.store.item(self.id,1,status='VOICE');self.store.update(self.id,state='REVIEW_REQUIRED',issues=['ELEVENLABS_NOT_CONFIGURED']);state=self.state();self.assertEqual(state['current_owner'],'USER');self.assertIn('RETRY_PROCESSING',state['allowed_actions'])
 def test_38_restart_queued_item_becomes_actionable(self):
  self.store.item(self.id,1,status='QUEUED');self.store.update(self.id,state='RUNNING');again=Store(self.store.home);j=again.get(self.id);self.assertEqual(item_state(again,j,j['items'][0])['current_owner'],'USER')

 def test_39_resume_queue_preserves_completed_analysis(self):
  self.store.done(self.id,'VIDEO_ANALYSIS',1,'source',{});self.store.done(self.id,'SCRIPT_APPROVED',1,'copy',{});self.store.item(self.id,1,status='QUEUED',resume_phase='SCRIPT')
  state=self.state();self.assertEqual(state['stages'][0]['status'],'DONE');self.assertEqual(state['current_stage'],'文案');self.assertEqual(state['allowed_actions'],[])

if __name__=='__main__':unittest.main()
