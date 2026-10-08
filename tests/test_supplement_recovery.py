# coding: utf-8
"""0.1.17 targeted regression. Real ffprobe; isolated DB; no paid providers."""
import autocut.context
import copy,json,tempfile,threading,subprocess,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from autocut.storage import Store
from autocut.pipeline import Manager
from autocut.item_review import ItemReview,get_item
from autocut.supplement_recovery import *
from autocut.review_media import import_media
from autocut.ownership import item_state
from autocut.item_pipeline import resume_item
from app.s3.headless_runtime import sha,write
from test_copy_presentation import response,BR

class Recovery(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.files=tempfile.TemporaryDirectory();cls.video=Path(cls.files.name)/'supp.mp4'
  subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=160x284:r=30:d=4','-c:v','libx264','-pix_fmt','yuv420p',str(cls.video)],check=True)
 @classmethod
 def tearDownClass(cls):cls.files.cleanup()
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data')
  self.id=self.store.create(dict(request_id='fixture',input_path=str(self.root),product_name='Buckle',market='Brazil',mode='Transport',product_profile_id='belt'))[0]['id'];self.store.update(self.id,state='PARTIAL_COMPLETED',batch_review={'phase':'RUNNING','ranks':['05']})
  self.m=Manager(self.store,None,MagicMock());self.service=ItemReview(self.store,self.m,self.m.router,MagicMock());self.source=self.root/'original.mp4';self.source.write_bytes(b'original read only fixture');audio=self.root/'voice.wav';audio.write_bytes(b'existing paid audio')
  self.src=dict(path=str(self.source),sha256=sha(self.source),rank=5,duration=20,width=160,height=284,fps=30)
  self.media=import_media(self.store.home,self.id,self.video);self.media['id']='supp'
  self.store.update(self.id,supplement_media=[self.media]);r=response()
  self.store.item(self.id,5,status='FAILED',issues=['DURATION_FIT_FAILED'],source_snapshot=self.src,source_path=str(self.source),analysis={'context':'same buckle'},source_sheet='cached.jpg',decision={'selected_title_id':1},copy=r,final_script=r['localized_script']['text'],final_script_language='pt-BR',final_title=r['titles'][0],script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,audio_result=dict(path=str(audio),sha256=sha(audio),duration=36),supplement_downloads=[self.media])
  self.plan=dict(status='MATCH',reason='Same buckle closeup',clips=[dict(supplement_source='supp',source_interval=[0,4],target_duration=4,insert_position=10,matched_script_segment=BR[:10],reason='Same buckle')])
  self.m.enqueue_batch=MagicMock();self.reload()
 def reload(self):
  self.j,self.x=get_item(self.store,self.id,5);self.rows=validate(self.j,self.x,bound_media(self.j,self.x));self.ctx=context(self.j,self.x,self.src,self.x['analysis'],self.rows)
 def apply(self):return apply_plan(self.service,self.id,5,self.plan,self.rows,self.ctx)
 def tearDown(self):self.m.queue.shutdown();self.tmp.cleanup()
 def test_01_probe_and_hash(self):
  self.assertEqual(self.rows[0]['validation_status'],'PASS');self.assertEqual(self.rows[0]['duration'],4);self.assertEqual(self.rows[0]['sha256'],sha(self.video))
 def test_02_binding_and_legacy(self):
  self.assertEqual(self.rows[0]['item_id'],'05');self.assertEqual(self.rows[0]['job_id'],self.id)
  self.assertEqual(bound_media(self.j,{'rank':'01'}),[])
 def test_03_plan_and_duration_updated(self):
  fit=self.apply();self.assertTrue(fit['fits']);self.assertEqual(fit['original_visual_duration'],24)
  x=get_item(self.store,self.id,5)[1];self.assertEqual(x['supplement_plan'],self.plan);self.assertTrue(Path(x['supplement_manifest_path']).exists())
 def test_04_clip_references_and_insert(self):
  c=resolve_supplements(self.src,{}, {},self.plan,self.rows,BR);self.assertEqual([r['path'] for r in c],[str(self.source),self.media['path'],str(self.source)])
 def test_05_minimum_and_preferred(self):
  self.assertEqual(self.ctx['minimum_additional_seconds'],4);self.assertAlmostEqual(self.ctx['preferred_additional_seconds'],8.8)
 def test_06_failed_resume_clears_owner(self):
  self.apply();x=get_item(self.store,self.id,5)[1];self.assertEqual(x['status'],'QUEUED');self.assertEqual(x['current_owner'],'SYSTEM');self.assertEqual(x['issues'],[])
  self.m.enqueue_batch.assert_called_once_with(self.id,[('05','SUPPLEMENT')])
 def test_07_invalidate_only_downstream(self):
  for stage in ['ASR_DONE','VIDEO_ANALYSIS','EDIT_PLAN','COPY_DONE','SCRIPT_APPROVED','TITLE_APPROVED','TTS_DONE','SUBTITLE','FINAL_RENDERED','QA_DONE']:self.store.done(self.id,stage,5,'old',{'existing':True})
  self.apply()
  for stage in ['ASR_DONE','VIDEO_ANALYSIS','EDIT_PLAN','COPY_DONE','SCRIPT_APPROVED','TITLE_APPROVED','TTS_DONE']:self.assertTrue(self.store.checkpoint(self.id,stage,5,'old'))
  for stage in ['SUBTITLE','FINAL_RENDERED','QA_DONE']:self.assertIsNone(self.store.checkpoint(self.id,stage,5,'old'))
 def test_08_invalid_plan_no_partial_commit(self):
  before=get_item(self.store,self.id,5)[1];self.plan['clips'][0]['source_interval']=[0,99]
  with self.assertRaises(ValueError):self.apply()
  self.assertEqual(get_item(self.store,self.id,5)[1],before)
 def test_09_insufficient_gap_and_action(self):
  self.plan['clips'][0].update(source_interval=[0,1],target_duration=1);fit=self.apply();self.assertFalse(fit['fits']);self.assertEqual(fit['missing_duration'],3)
  j,x=get_item(self.store,self.id,5);state=item_state(self.store,j,x);self.assertIn('3.00',state['state_message']);self.assertIn('ADD_SUPPLEMENT_MEDIA',state['allowed_actions']);self.m.enqueue_batch.assert_not_called()
 def test_10_render_error_not_duration(self):
  self.store.item(self.id,5,status='FAILED',issues=['FFMPEG_RENDER_FAILED']);j,x=get_item(self.store,self.id,5);self.assertNotIn('ADD_SUPPLEMENT_MEDIA',item_state(self.store,j,x)['allowed_actions'])
 def test_11_duplicate_local_intake(self):
  a=import_media(self.store.home,self.id,self.video);b=import_media(self.store.home,self.id,self.video);self.assertEqual(a['id'],b['id']);self.assertEqual(a['path'],b['path'])
 def test_12_duplicate_reuse_no_provider(self):
  self.apply();self.m.item_pending.add((self.id,'05'));self.service.reuse_media(self.id,5,'stale');self.service.reuse_media(self.id,5,'stale');self.m.router.resolve.assert_not_called();self.m.enqueue_batch.assert_called_once()
 def test_13_restart_durable_outbox(self):
  self.apply();again=Store(self.store.home);m=Manager(again,None,MagicMock());m.enqueue_batch=MagicMock();m.resume_job_after_user_action(self.id,recover=True);m.enqueue_batch.assert_called_once_with(self.id,[('05','SUPPLEMENT')]);m.queue.shutdown()
 def test_14_other_items_unchanged(self):
  for n in range(1,5):self.store.item(self.id,n,status='FAILED',issues=['fixture unchanged'])
  before=self.store.get(self.id)['items'][:4];self.apply();self.assertEqual(self.store.get(self.id)['items'][:4],before)
 def test_15_approved_content_preserved(self):
  before=copy.deepcopy(self.x);self.apply();x=get_item(self.store,self.id,5)[1]
  for k in ['copy','final_script','final_title','script_status','title_status','audio_result']:self.assertEqual(x[k],before[k])
 def test_16_cached_plan_no_analysis_charge(self):
  write(cache_path(self.store.home,self.id,self.ctx),self.plan);self.service.plan_media(self.id,5,internal=True);self.m.router.resolve.assert_not_called();self.assertTrue(get_item(self.store,self.id,5)[1]['duration_match']['fits'])
 def test_17_real_worker_single_launch(self):
  self.apply();self.m.enqueue_batch=Manager.enqueue_batch.__get__(self.m);entered=threading.Event();release=threading.Event();calls=[]
  def worker(*a):calls.append(a);entered.set();release.wait(5)
  with patch('autocut.item_pipeline.resume_item',side_effect=worker):
   self.m.resume_job_after_user_action(self.id);self.assertTrue(entered.wait(5));self.m.resume_job_after_user_action(self.id);release.set();self.m.active[self.id].result(10)
  self.assertEqual(len(calls),1);self.assertEqual(calls[0][-1],'SUPPLEMENT')
 def test_18_cached_audio_and_real_plan_builder_boundary(self):
  self.apply();self.m.stops[self.id]=threading.Event();before=get_item(self.store,self.id,5)[1]
  with patch('autocut.item_pipeline.Harness') as harness,patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.pipeline.make_plan',return_value={'rank':'05','final_script':BR}) as make,patch.object(self.m,'finish_plans'):
   resume_item(self.m,self.id,'05','SUPPLEMENT');tts.assert_not_called();make.assert_called_once();self.assertEqual(len(make.call_args.args[0]['remix_clips']),3)
  after=get_item(self.store,self.id,5)[1];self.assertEqual(after['final_title'],before['final_title']);self.m.router.resolve.assert_not_called()
 def test_19_missing_audio_never_pays(self):
  self.apply();Path(self.x['audio_result']['path']).unlink();self.m.stops[self.id]=threading.Event()
  with patch('autocut.item_pipeline.tts_job') as tts:resume_item(self.m,self.id,'05','SUPPLEMENT');tts.assert_not_called()
  self.assertEqual(get_item(self.store,self.id,5)[1]['issues'],['TTS_CACHE_MISSING'])
 def test_20_corrupt_media_rejected(self):
  Path(self.media['path']).write_bytes(b'not a video')
  with self.assertRaisesRegex(ValueError,'SUPPLEMENT_MEDIA_INVALID'):validate(self.j,self.x,[self.media])
 def test_21_cta_default_unchanged(self):
  from autocut.cta_policy import required
  self.assertTrue(required({'task_overrides':{}}))
 def test_22_schema_unchanged(self):
  self.apply()
  with self.store.connect() as db:self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)

if __name__=='__main__':unittest.main()
