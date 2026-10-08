"""0.1.15 focused tests: actual HTTP/SQLite/worker resume; paid/media edges mocked."""
import copy,json,shutil,threading,time,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
import test_batch_workspace as batch_fixture
import test_ownership as owner_fixture
from test_review_flow import future,DEC
from autocut.pipeline import Manager
from autocut.storage import Store
from autocut.server import create_app
from autocut.ownership import item_state,checkpoints,final_ready
from autocut.duration_fit import solve
from autocut.production_policy import classify,VERSION
from autocut.batch_confirmation import batch_view
from autocut.script_language import check
from app.s3.headless_runtime import write,sha
from fastapi.testclient import TestClient

class AutoTests(unittest.TestCase):
 setUp=batch_fixture.BatchTests.setUp
 tearDown=batch_fixture.BatchTests.tearDown
 raw=batch_fixture.BatchTests.raw;state=batch_fixture.BatchTests.state;post=batch_fixture.BatchTests.post;public=batch_fixture.BatchTests.public;view=batch_fixture.BatchTests.view;payload=batch_fixture.BatchTests.payload;submit=batch_fixture.BatchTests.submit;scripts=batch_fixture.BatchTests.scripts;rows=batch_fixture.BatchTests.rows
 def manager(self):
  self.m=Manager(self.store,MagicMock(),MagicMock());self.addCleanup(self.m.queue.shutdown,wait=True)
  self.app.state.batch.manager=self.m
  return self.m
 def released(self):self.scripts();self.assertEqual(self.submit('titles').status_code,200)
 def test_01_ten_scripts_transition_and_snapshot(self):
  for n in range(4,11):
   x=copy.deepcopy(self.raw());x.pop('rank');self.store.item(self.id,n,**x)
  self.store.update(self.id,source_count=10);r=self.scripts().json();self.assertEqual(r['batch_state'],'TITLES');self.assertEqual(r['job_status'],'待确认标题');self.assertEqual(len(r['items']),10);self.assertTrue(all(x['script_status']=='USER_APPROVED' for x in self.rows()));self.assertEqual(self.queue.enqueued,[])
 def test_02_invalid_script_atomic(self):
  self.store.item(self.id,3,final_script='This is English text for your product',final_script_language='en');before=self.rows();self.assertEqual(self.submit('scripts').status_code,400);self.assertEqual(before,self.rows())
 def test_03_missing_title_atomic(self):
  self.scripts();before=self.rows();d=self.payload('titles');d['selections'].pop('03');self.assertEqual(self.submit('titles',d).status_code,400);self.assertEqual(before,self.rows())
 def test_04_stale_after_script_edit(self):
  d=self.payload('scripts');self.store.item(self.id,2,action_revision=900);self.assertEqual(self.submit('scripts',d).json()['error'],'STALE_ACTION')
 def test_05_collision_does_not_reopen_confirmed_script_or_title(self):
  self.released()
  for code in ['CTA_SUBTITLE_COLLISION','CTA_OUT_OF_CANVAS','TEXT_GHOSTING']:
   self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=[code]);s=self.state();self.assertNotIn('APPROVE_CONTENT',s['allowed_actions']);self.assertNotIn('APPROVE_SCRIPT',s['allowed_actions']);self.assertNotIn('APPROVE_TITLE',s['allowed_actions']);self.assertEqual(s['pipeline_status'],'FAILED')
 def test_06_actual_worker_tts_resume_and_duplicate_no_second_call(self):
  m=self.manager();self.scripts();data=self.payload('titles');entered=threading.Event();release=threading.Event();calls=[]
  def tts(h,payload,**kw):
   calls.append(payload['tts_text']);entered.set();release.wait(4);raise ValueError('FIXTURE_PROVIDER_FAILURE')
  with patch('autocut.item_pipeline.tts_payload',side_effect=lambda text,*a:({'tts_text':text},None)),patch.object(m,'catalog',return_value={'voices':[{'id':'v','name':'Carla'}],'scope':'fixture'}),patch('autocut.item_pipeline.tts_job',side_effect=tts):
   r=self.submit('titles',data);self.assertEqual(r.status_code,200,r.text);self.assertTrue(entered.wait(3));self.assertEqual(r.json()['batch_state'],'RUNNING');self.assertEqual(r.json()['job_status'],'剪辑中');self.assertTrue(all(x['status'] not in {'PASS','WAITING_TITLE_APPROVAL','WAITING_SCRIPT_APPROVAL'} for x in self.rows()));self.assertEqual(self.submit('titles',data).json()['batch_result']['replayed'],True);release.set();m.active[self.id].result(8)
  self.assertEqual(len(calls),3);self.assertEqual(set(calls),{x['final_script'] for x in self.rows()});self.assertTrue(all(x['status']=='FAILED' for x in self.rows()))
 def test_07_pending_commit_survives_restart_and_once_dispatch(self):
  self.released();s=Store(self.store.home);m=Manager(s,None,MagicMock());self.addCleanup(m.queue.shutdown,wait=True);seen=[];hold=threading.Event()
  def worker(manager,id,rank,phase):seen.append(rank);hold.wait(3);manager.store.item(id,rank,status='FAILED',issues=['FIXTURE'])
  with patch('autocut.item_pipeline.resume_item',side_effect=worker):
   m.recover_jobs();m.recover_jobs();hold.set();m.active[self.id].result(5);m.recover_jobs()
  self.assertCountEqual(seen,['01','02','03']);self.assertEqual(batch_view(s,s.get(self.id))['phase'],'RUNNING')
  with s.connect() as db:self.assertEqual(db.execute('select max(version) from migrations').fetchone()[0],3)
 def test_08_legacy_warning_resumes_once_hard_failure_preserved(self):
  self.released()
  for n,c in [(1,'TEXT_GHOSTING'),(2,'SOURCE_HASH_CHANGED'),(3,'VISUAL_DURATION_INSUFFICIENT')]:self.store.item(self.id,n,status='REVIEW_REQUIRED' if n!=3 else 'WAITING_SUPPLEMENT_MEDIA',issues=[c],production_policy=None,resume_pending=False)
  m=self.manager();seen=[]
  def work(manager,id,rank,phase):seen.append(rank);manager.store.item(id,rank,status='FAILED',issues=['FIXTURE'])
  with patch('autocut.item_pipeline.resume_item',side_effect=work):m.recover_jobs();m.active[self.id].result(5);m.recover_jobs()
  self.assertCountEqual(seen,['01','03']);self.assertEqual(self.rows()[1]['issues'],['SOURCE_HASH_CHANGED'])
 def test_09_all_ordinary_warnings_nonblocking(self):
  for code in ['BACKGROUND_BLUR','TEXT_GHOSTING','OVERLAY_BOUNDARY','CARD_STYLE','TEMPORAL_ARTIFACT']:
   self.assertEqual(classify(dict(status='REVIEW_REQUIRED',issues=[code]))['status'],'WARNING')
 def test_10_severe_and_corrupt_fail(self):
  for code in ['SUBJECT_DAMAGE','PRODUCT_OBSCURED','BLACK_SCREEN','NO_AUDIO','OUTPUT_CORRUPT']:
   self.assertEqual(classify(dict(status='REVIEW_REQUIRED',issues=[code]))['status'],'FAIL')
  self.assertEqual(classify(dict(status='FAIL',issues=['decode_failed']),True)['status'],'FAIL')
 def test_11_final_gate_rejects_bad_qa(self):
  owner_fixture.OwnershipTests.final_fixture(self);self.assertFalse(final_ready(self.store,self.store.get(self.id),self.raw()))
 def test_12_30_31_fit(self):self.assertFit(30,31)
 def test_13_30_36_joint_fit(self):
  p=self.assertFit(30,36);self.assertGreater(p['video_speed'],1);self.assertGreater(p['voice_speed'],1)
 def test_14_30_42_fit(self):self.assertFit(30,42)
 def test_15_hard_limit_unfit(self):self.assertEqual(solve(23,42)['fit_result'],'DURATION_FIT_FAILED')
 def test_16_equal_max_cannot_fix_ratio(self):self.assertFalse(solve(30,46)['fits'])
 def test_17_no_slowmo_or_voice_slowdown_surplus(self):
  p=self.assertFit(60,30);self.assertLess(p['target_duration'],60);self.assertEqual(p['voice_speed'],1)
 def test_18_music_exempt(self):self.assertTrue(solve(10,60,music_only=True)['fits'])
 def test_19_conservative_face_motion(self):self.assertEqual(solve(30,36,{'conservative_motion':True})['video_speed'],1)
 def test_20_no_confirmed_text_mutation(self):
  before=copy.deepcopy(self.rows());[solve(v,a) for v,a in [(30,31),(30,42),(23,42)]];self.assertEqual(before,self.rows())
 def test_21_duplicate_script_receipt_no_work(self):
  d=self.payload('scripts');self.submit('scripts',d);before=self.rows();self.assertTrue(self.submit('scripts',d).json()['batch_result']['replayed']);self.assertEqual(before,self.rows());self.assertEqual(self.queue.enqueued,[])
 def test_22_system_speed_state_no_actions(self):
  self.released();self.store.update(self.id,state='RUNNING');self.store.item(self.id,1,status='DURATION_FIT');s=self.state();self.assertEqual(s['current_owner'],'SYSTEM');self.assertEqual(s['allowed_actions'],[]);self.assertEqual(s['state_message'],'正在自动适配时长')
 def test_23_sql_failure_no_half_commit(self):
  before=self.rows()
  with self.store.connect() as d:d.execute("CREATE TRIGGER reject_last BEFORE UPDATE ON job_items WHEN NEW.rank='03' BEGIN SELECT RAISE(ABORT,'fixture'); END")
  with self.assertRaises(Exception):self.app.state.batch.scripts(self.id,self.payload('scripts'))
  self.assertEqual(before,self.rows());self.assertEqual(self.view()['phase'],'SCRIPTS')
 def test_24_frontend_no_generic_retry(self):
  text=Path('web/review.js').read_text();self.assertNotIn('重新处理',text);self.assertNotIn('接受当前画面',text);self.assertNotIn('接受当前结果',text)
 def test_25_no_early_pass(self):
  self.store.item(self.id,1,status='ANALYZING',copy=None,final_script=None);self.store.update(self.id,state='RUNNING');self.assertEqual(self.post('review/accept').status_code,409);self.assertEqual(self.raw()['status'],'ANALYZING')
 def assertFit(self,v,a):
  p=solve(v,a);self.assertTrue(p['fits']);self.assertLessEqual(p['voice_speed'],1.5);self.assertLessEqual(p['video_speed'],1.5);self.assertGreaterEqual(p['video_speed'],1);self.assertGreaterEqual(p['voice_speed'],1);self.assertLessEqual(p['final_voice_duration'],p['final_visual_duration']+.001);self.assertLessEqual(p['target_duration']*p['video_speed'],v+.001);return p
 def finish_case(self,visual='PASS',technical='PASS',render_errors=None):
  self.released();owner_fixture.OwnershipTests.final_fixture(self)
  p=json.loads(Path(self.raw()['plan']).read_text());p.update(rank='01',target_duration=6,title=self.copy['titles'][0]['text'],final_title_language='pt-BR',source=self.raw()['source_snapshot']);write(self.raw()['plan'],p)
  self.store.item(self.id,1,status='EDITING');self.store.update(self.id,final_review_mode='MANUAL');m=self.manager();m.stops[self.id]=threading.Event();work=self.root/'work';work.mkdir(exist_ok=True)
  source_video=Path('build/review019/smoke/music_supplement_smoke.mp4');output=Path(p['output']);errors=list(render_errors or []);calls=[]
  def render(*args,**kwargs):
   calls.append(kwargs)
   if errors:
    code=errors.pop(0)
    if code:return future(dict(error=code))
   shutil.copy2(source_video,output);return future(dict(output=str(output),output_sha256=sha(output)))
  def judge(*args):return dict(status=visual,issues=[] if visual=='PASS' else ['TEXT_GHOSTING'],reason='mock visual evidence',product_preserved=True,**{k:True for k in ['title','subtitle','cta','overlay','source_text','watermark']})
  with patch('autocut.automatic_finish.preflight_job',return_value=future(dict(output=str(output),output_sha256=sha(output),contact_sheet='fixture.jpg',technical_status='PASS'))),patch('autocut.pipeline.preflight_review_context',return_value={}),patch('autocut.automatic_finish.approve_preflight'),patch('autocut.automatic_finish.identity',return_value='same-render'),patch('autocut.automatic_finish.final_job',side_effect=render),patch('autocut.automatic_finish.qa_job',side_effect=lambda *a:future(dict(status=technical,issues=[] if technical=='PASS' else ['decode_failed'],output=str(output),output_sha256=sha(output)))),patch('autocut.automatic_finish.batch_sheets'),patch.object(m,'judge',side_effect=judge):m.finish_plans(self.id,MagicMock(),self.store.get(self.id),[p],work)
  return calls
 def test_26_normal_final_auto_pass_even_legacy_manual_setting(self):
  self.finish_case();self.assertEqual(self.raw()['status'],'PASS');self.assertNotIn('APPROVE_FINAL',self.state()['allowed_actions'])
 def test_27_visual_warning_completes_with_note(self):
  self.finish_case(visual='REVIEW_REQUIRED');self.assertEqual(self.raw()['status'],'PASS');self.assertEqual(self.raw()['qa_status'],'WARNING');self.assertIn('TEXT_GHOSTING',self.raw()['qa_warnings'])
 def test_28_render_retry_once_success(self):
  calls=self.finish_case(render_errors=['FFMPEG_RENDER_FAILED',None]);self.assertEqual(len(calls),2);self.assertEqual(self.raw()['status'],'PASS');self.assertTrue(self.raw()['auto_repair_used'])
 def test_29_render_retry_once_failure(self):
  calls=self.finish_case(render_errors=['FFMPEG_RENDER_FAILED','FFMPEG_RENDER_FAILED']);self.assertEqual(len(calls),2);self.assertEqual(self.raw()['status'],'FAILED')
 def test_30_corrupt_output_fails_after_one_repair(self):
  calls=self.finish_case(technical='FAIL');self.assertEqual(len(calls),2);self.assertEqual(self.raw()['status'],'FAILED');self.assertFalse(final_ready(self.store,self.store.get(self.id),self.raw()))
 def test_31_ten_workers_one_failure_nine_real_final_gates(self):
  self.released();owner_fixture.OwnershipTests.final_fixture(self);base=copy.deepcopy(self.raw());base.update(status='QUEUED',resume_pending=True,script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True);base.pop('rank')
  for n in range(1,11):self.store.item(self.id,n,**base)
  self.store.update(self.id,batch_review=dict(phase='RUNNING',ranks=[f'{n:02}' for n in range(1,11)],receipts={}),state='QUEUED')
  m=self.manager();entered=threading.Barrier(2);seen=[]
  def work(manager,id,rank,phase):
   if rank in ['01','02']:entered.wait(3)
   seen.append(rank)
   if rank=='05':manager.store.item(id,rank,status='FAILED',issues=['DURATION_FIT_FAILED']);return
   x=next(x for x in manager.store.get(id)['items'] if x['rank']==rank)
   for stage,data in [('TTS_DONE',x['audio_result']),('FINAL_RENDERED',dict(output=x['output'],output_sha256=sha(x['output']))),('QA_DONE',dict(status='PASS',technical={'status':'PASS'}))]:manager.store.done(id,stage,rank,'same-render',data)
   manager.store.item(id,rank,status='CHECKING');manager.store.item(id,rank,status='PASS')
  with patch('autocut.item_pipeline.resume_item',side_effect=work):m.resume_job_after_user_action(self.id);m.active[self.id].result(6)
  self.assertEqual(len(seen),10);self.assertEqual(sum(x['status']=='PASS' for x in self.rows()),9);self.assertEqual(self.store.get(self.id)['state'],'PARTIAL_COMPLETED');self.assertEqual(self.view()['label'],'需要处理 1')
 def test_32_reopen_script_minimal_still_works(self):
  self.scripts();b=self.view();r=self.c.post('/jobs/'+self.id+'/batch/reopen-script',json=dict(rank='01',action_token=b['action_token']));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.view()['phase'],'SCRIPTS');self.assertEqual(self.rows()[1]['script_status'],'USER_APPROVED')
 def test_33_missing_titles_prepared_automatically_without_tts(self):
  self.store.item(self.id,1,title_candidates=[],copy=dict(self.copy,titles=[]));m=self.manager()
  with patch('autocut.content_confirmation.ContentConfirmation.title_request',return_value=self.copy['titles']) as titles,patch('autocut.item_pipeline.tts_job') as tts:
   self.scripts();m.active[self.id].result(5);titles.assert_called_once();tts.assert_not_called()
  self.assertTrue(self.view()['all_titles_ready']);self.assertEqual(self.view()['label'],'待确认标题')
 def test_34_checkpoint_rank_alias_does_not_shadow_latest(self):
  with self.store.connect() as d:
   for rank,status in [('1','REVIEW_REQUIRED'),('01','PASS')]:d.execute('insert or replace into checkpoints values(?,?,?,?,?)',(self.id,'QA_DONE',rank,'same',json.dumps({'status':status})))
  self.assertEqual(checkpoints(self.store,self.id,1)['QA_DONE']['data']['status'],'PASS');self.store.done(self.id,'QA_DONE',1,'new',{'status':'WARNING'});self.assertEqual(checkpoints(self.store,self.id,1)['QA_DONE']['data']['status'],'WARNING')
 def test_35_completed_job_not_reexecuted(self):
  self.finish_case();before=self.raw();m=self.m
  with patch.object(m,'enqueue_batch') as enqueue:
   m.resume_job_after_user_action(self.id,True);self.assertNotIn('01',[rank for call in enqueue.call_args_list for rank,phase in call.args[1]])
  self.assertEqual(before,self.raw())
 def test_36_unknown_legacy_failure_not_waived(self):
  self.released();self.store.item(self.id,1,status='REVIEW_REQUIRED',issues=['UNKNOWN_STATE_DAMAGE'],resume_pending=False,production_policy=None);m=self.manager()
  with patch.object(m,'enqueue_batch') as enqueue:
   for n in [2,3]:self.store.item(self.id,n,resume_pending=False)
   m.resume_job_after_user_action(self.id,True);enqueue.assert_not_called()
  self.assertEqual(self.raw()['issues'],['UNKNOWN_STATE_DAMAGE'])
 def test_37_old_completed_warning_with_explicit_final_approval_stays_complete(self):
  self.released();owner_fixture.OwnershipTests.final_fixture(self);x=self.raw()
  self.store.done(self.id,'QA_DONE',1,'same-render',dict(status='REVIEW_REQUIRED',technical={'status':'PASS'},visual={'status':'REVIEW_REQUIRED','issues':['TEXT_GHOSTING']}));self.store.done(self.id,'FINAL_APPROVED',1,'same-render',{'status':'PASS'})
  # Historical terminal record: fixture only, normal writes still use final gate.
  with self.store.connect() as d:
   x.update(status='USER_APPROVED');d.execute('update job_items set data=? where job_id=? and rank=?',(json.dumps(x),self.id,'01'))
  self.assertTrue(final_ready(self.store,self.store.get(self.id),self.raw()));self.assertEqual(self.state()['pipeline_status'],'COMPLETED')
 def test_38_backend_progress_comes_from_live_item_states(self):
  self.released();self.store.update(self.id,state='RUNNING',progress=30);self.store.item(self.id,1,status='DURATION_FIT');self.store.item(self.id,2,status='RENDERING');self.store.item(self.id,3,status='CHECKING');j=self.public();self.assertEqual([x['ui_progress'] for x in j['items']],[52,88,92]);self.assertEqual(j['progress'],77)

 def test_39_exact_hard_limit_fits_without_truncation(self):
  p=self.assertFit(30,45);self.assertEqual(p['voice_speed'],1.5);self.assertEqual(p['video_speed'],1);self.assertEqual(p['target_duration'],30)
 def test_40_existing_tts_reused_for_speed_without_provider_or_rewrite(self):
  self.released();a=dict(path=str(self.source),sha256=sha(self.source),duration=28,technical_status='PASS');self.store.item(self.id,1,audio_result=a);before=self.raw()['final_script'];m=self.manager();m.stops[self.id]=threading.Event()
  from autocut.item_pipeline import resume_item
  with patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.item_review.ItemReview.copy_request') as copy_request,patch('autocut.pipeline.make_plan',side_effect=ValueError('FIXTURE_AFTER_FIT')):
   resume_item(m,self.id,1,'TITLE');tts.assert_not_called();copy_request.assert_not_called()
  self.assertTrue(self.raw()['duration_match']['fits']);self.assertEqual(before,self.raw()['final_script']);self.assertEqual(sha(self.source),a['sha256'])
