import copy,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from autocut import context
from autocut.production_resume import recheck_cta_layers
from autocut.item_pipeline import resume_item
from app.s3.headless_runtime import sha


class LayerResumeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  media=self.root/'cached';media.write_bytes(b'unchanged')
  self.plan=dict(rank='01',final_script='Texto aprovado',title='Título aprovado',source=dict(path=str(media),sha256=sha(media)),audio=dict(path=str(media),sha256=sha(media)),output=str(self.root/'new.mp4'))
  path=self.root/'plan.json';path.write_text(json.dumps(self.plan))
  self.item=dict(rank='01',status='FAILED',issues=['CTA_SUBTITLE_COLLISION'],plan=str(path),final_script=self.plan['final_script'],final_title=dict(text=self.plan['title']),script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True)
  self.job=dict(id='job',batch_review={'phase':'RUNNING'},items=[self.item,dict(rank='02',status='PASS')])
  self.m=MagicMock();self.m.lock=threading.RLock();self.m.item_pending=set();self.m.store.lock=threading.RLock();self.m.store.home=self.root;self.m.store.get.return_value=self.job

 def test_only_requested_failure_queues_and_pending_is_idempotent(self):
  with patch('autocut.ownership.invalidate_final') as invalidate:
   recheck_cta_layers(self.m,'job',['01'])
   self.m.enqueue_batch.assert_called_once_with('job',[('01','CTA_TEXT_LAYERS')]);invalidate.assert_called_once_with(self.m.store,'job','01')
   self.m.item_pending.add(('job','01'));self.m.enqueue_batch.reset_mock()
   recheck_cta_layers(self.m,'job',['01']);self.m.enqueue_batch.assert_not_called()

 def test_other_error_and_existing_output_do_not_mutate(self):
  for issue in ['OUTPUT_QA_FAILED','DURATION_FIT_FAILED']:
   self.item['issues']=[issue]
   with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):recheck_cta_layers(self.m,'job',['01'])
  self.item['issues']=['CTA_SUBTITLE_COLLISION'];Path(self.plan['output']).write_bytes(b'keep')
  with self.assertRaisesRegex(ValueError,'FINAL_OUTPUT_ALREADY_EXISTS'):recheck_cta_layers(self.m,'job',['01'])
  self.m.store.item.assert_not_called();self.m.enqueue_batch.assert_not_called()

 def test_corrupt_cache_and_changed_approval_do_not_mutate(self):
  self.item['final_script']='changed'
  with self.assertRaisesRegex(ValueError,'APPROVED_CONTENT_CHANGED'):recheck_cta_layers(self.m,'job',['01'])
  self.item['final_script']=self.plan['final_script'];Path(self.plan['audio']['path']).write_bytes(b'corrupt')
  with self.assertRaisesRegex(ValueError,'RECOVERY_CACHE_INVALID'):recheck_cta_layers(self.m,'job',['01'])
  self.m.store.item.assert_not_called()

 def test_worker_reuses_exact_plan_without_upstream(self):
  before=copy.deepcopy(self.plan)
  with patch('autocut.item_pipeline.Harness') as harness,patch('autocut.item_pipeline.audit'),patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.pipeline.make_plan') as make:
   resume_item(self.m,'job','01','CTA_TEXT_LAYERS')
   self.m.finish_plans.assert_called_once_with('job',harness.return_value.__enter__.return_value,self.job,[before],self.root/'jobs/job/work')
   tts.assert_not_called();make.assert_not_called();self.m.judge.assert_not_called()

if __name__=='__main__':unittest.main()
