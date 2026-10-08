"""Real-production regression: partial overlap must not imply severe damage."""
import json,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from autocut.drivers import CodexDriver,Review,DriverError
from autocut.production_policy import classify
from autocut.pipeline import Manager
from autocut.storage import Store
from autocut.production_resume import recheck_visual_severity,resume
from app.s3.headless_runtime import fingerprint

def review(**kw):
 return dict(status='REVIEW_REQUIRED',title=True,subtitle=False,cta=True,overlay=True,source_text=True,watermark=True,product_preserved=False,issues=['Captions partially overlap a hand; product remains recognizable.'],reason='Minor partial overlap',**kw)

class SeverityTests(unittest.TestCase):
 def test_01_partial_overlap_is_warning(self):
  self.assertEqual(classify(review(severity='WARNING'))['status'],'WARNING')
 def test_02_legacy_false_flag_not_silently_waived(self):
  self.assertEqual(classify(review())['status'],'FAIL')
 def test_03_severe_damage_stays_blocking(self):
  self.assertEqual(classify(review(severity='SEVERE'))['status'],'FAIL')
 def test_04_known_hard_error_cannot_be_downgraded(self):
  r=review(severity='WARNING');r['issues']=['PRODUCT_OBSCURED']
  self.assertEqual(classify(r)['status'],'FAIL')
  self.assertEqual(classify({'status':'FAIL','issues':[],'severity':'WARNING'},True)['status'],'FAIL')
 def test_05_fresh_review_requires_severity(self):
  with tempfile.TemporaryDirectory() as d:
   driver=CodexDriver(d)
   with self.assertRaises(DriverError):driver.decode(json.dumps(review()),Review)
   self.assertEqual(driver.decode(json.dumps(review(severity='WARNING')),Review).severity,'WARNING')
 def test_06_warning_is_not_claimed_as_clean_pass(self):
  with tempfile.TemporaryDirectory() as d:
   r=review(severity='WARNING');r['status']='PASS'
   with self.assertRaises(DriverError):CodexDriver(d).decode(json.dumps(r),Review)
 def test_07_contract_explains_visual_severity(self):
  with tempfile.TemporaryDirectory() as d:
   driver=CodexDriver(d);driver._request=MagicMock();driver.review_overlay({},['frame'])
   self.assertIn('substantially obscured',driver._request.call_args.args[0])
   self.assertIn('severity',Review.model_json_schema()['required'])
 def test_08_new_review_contract_does_not_repeat_paid_stages(self):
  with tempfile.TemporaryDirectory() as d:
   store=Store(Path(d)/'db');j,_=store.create(dict(request_id='severity',product_name='belt'));id=j['id'];inputs={'plan':'same'}
   store.done(id,'VISUAL_QA','preflight-03',fingerprint(inputs),review())
   for stage in ['ASR_DONE','COPY_DONE','TTS_DONE']:store.done(id,stage,'03','paid',{'unchanged':True})
   driver=CodexDriver(d);driver.review_overlay=MagicMock(return_value=Review(**review(severity='WARNING')))
   manager=Manager(store,MagicMock(),SimpleNamespace(resolve=lambda _:driver));self.addCleanup(manager.queue.shutdown);manager.stops[id]=threading.Event()
   fresh=manager.judge(id,MagicMock(),'VISUAL_QA','preflight-03',inputs,'review_overlay',[])
   self.assertEqual(classify(fresh)['status'],'WARNING')
   manager.judge(id,MagicMock(),'VISUAL_QA','preflight-03',inputs,'review_overlay',[]);self.assertEqual(driver.review_overlay.call_count,1)
   for stage in ['ASR_DONE','COPY_DONE','TTS_DONE']:self.assertEqual(store.checkpoint(id,stage,'03','paid'),{'unchanged':True})
 def recovery(self):
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);s=Store(temp.name);j,_=s.create(dict(request_id='onsite-recheck',product_name='belt',market='Brazil',target_language='pt-BR',batch_review_required=True,batch_review={'phase':'RUNNING','ranks':['03','04','05']},scripts_approved=True,titles_approved=True));id=j['id']
  for rank in ['03','04','05']:
   s.item(id,rank,status='FAILED',issues=['SEVERE_VISUAL_QA'] if rank!='05' else ['DURATION_FIT_FAILED'],script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,final_script='Você só encaixa e puxa para ajustar a calça na cintura.',final_script_language='pt-BR',final_title={'text':'Ajuste sua calça sem complicação','language':'pt-BR'})
   s.done(id,'VISUAL_QA','preflight-'+rank,'old',review())
  s.update(id,state='PARTIAL_COMPLETED');m=MagicMock();m.store=s;m.item_pending=set();m.resume_job_after_user_action.side_effect=lambda i:resume(m,i)
  return s,id,m
 def test_09_targeted_recovery_once_preserves_duration_failure(self):
  s,id,m=self.recovery();recheck_visual_severity(m,id,['03','04']);m.enqueue_batch.assert_called_once();self.assertEqual([r for r,p in m.enqueue_batch.call_args.args[1]],['03','04'])
  recheck_visual_severity(m,id,['03','04']);self.assertEqual(m.enqueue_batch.call_count,2) # Outbox stays pending until the worker claims it.
  m.item_pending={(id,'03'),(id,'04')};recheck_visual_severity(m,id,['03','04']);self.assertEqual(m.enqueue_batch.call_count,2)
  self.assertEqual(next(x for x in s.get(id)['items'] if x['rank']=='05')['issues'],['DURATION_FIT_FAILED'])
 def test_10_invalid_set_has_no_half_mutation(self):
  s,id,m=self.recovery()
  with self.assertRaises(ValueError):recheck_visual_severity(m,id,['03','05'])
  self.assertFalse(next(x for x in s.get(id)['items'] if x['rank']=='03').get('resume_pending'));m.enqueue_batch.assert_not_called()
 def test_11_new_severe_verdict_cannot_be_retried_as_legacy(self):
  s,id,m=self.recovery();s.done(id,'VISUAL_QA','preflight-03','new',review(severity='SEVERE'))
  with self.assertRaises(ValueError):recheck_visual_severity(m,id,['03'])
  m.enqueue_batch.assert_not_called()
 def test_12_recovery_never_approves_content(self):
  s,id,m=self.recovery();s.item(id,'03',script_status='WAITING_APPROVAL',content_confirmed=False)
  with self.assertRaises(ValueError):recheck_visual_severity(m,id,['03'])
  m.enqueue_batch.assert_not_called()

if __name__=='__main__':unittest.main()
