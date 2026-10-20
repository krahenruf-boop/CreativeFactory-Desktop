import copy,json,subprocess,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from autocut import context
from autocut.ai_recovery import request,resume_final,final_evidence,resume_render
from autocut.drivers import CodexDriver,DriverError,PlanDecision
from app.s3.headless_runtime import sha,write

class AIRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.work=self.root/'jobs/job/work'
  self.media=self.root/'source';self.media.write_bytes(b'source unchanged');self.out=self.root/'final.mp4';self.out.write_bytes(b'existing export')
  spoken={'text':'Este kit deixa seu carro muito mais claro.','language':'pt-BR'};title={'text':'Seu carro mais claro','language':'pt-BR'}
  self.item=dict(rank='07',status='FAILED',issues=['AI_DRIVER_EXEC_FAILED'],script_status='USER_APPROVED',title_status='USER_APPROVED',content_confirmed=True,final_script=spoken,final_title=title,output=str(self.out),plan=str(self.work/'plans/07.json'))
  self.other=dict(self.item,rank='09',output=None,plan=None,source_snapshot=dict(path=str(self.media),sha256=sha(self.media)),analysis={'context':'lamp'},copy={'titles':[title]})
  self.job=dict(id='job',market='Brazil',batch_review={'phase':'RUNNING'},items=[self.item,self.other,dict(rank='01',status='PASS')])
  self.plan=dict(rank='07',output=str(self.out),final_script=spoken['text'],title=title['text']);write(self.item['plan'],self.plan)
  self.technical=dict(status='PASS',full_decode_count=1,output_sha256=sha(self.out),issues=[])
  self.arrow=dict(status='PASS',output_sha256=sha(self.out));self.rendered=dict(input_sha='render-identity',data=dict(output_sha256=sha(self.out),cta_verification=self.arrow))
  write(self.work/'qa/07/qa.json',self.technical);(self.work/'qa/07/contact.jpg').write_bytes(b'visual evidence')
  self.manager=MagicMock();self.manager.lock=threading.RLock();self.manager.item_pending=set()
  self.store=self.manager.store;self.store.home=self.root;self.store.lock=threading.RLock();self.store.get.return_value=self.job
  self.store.item.side_effect=lambda id,rank,**data: next(i for i in self.job['items'] if i['rank']==rank).update(data)
  self.cp=patch('autocut.ai_recovery.checkpoints',side_effect=lambda s,j,r:{'FINAL_RENDERED':self.rendered} if r=='07' else {})
  self.cp.start();self.addCleanup(self.cp.stop)

 def test_queue_uses_final_qa_and_missing_plan_only(self):
  request(self.manager,'job',['07','09'])
  self.manager.enqueue_batch.assert_called_once_with('job',[('07','AI_FINAL_QA'),('09','SCRIPT')])
  self.manager.item_pending.update([('job','07'),('job','09')]);self.manager.enqueue_batch.reset_mock()
  request(self.manager,'job',['07','09']);self.manager.enqueue_batch.assert_not_called()
  self.assertEqual(self.job['items'][2]['status'],'PASS')

 def test_changed_export_and_unrelated_failure_rejected_before_queue(self):
  self.out.write_bytes(b'changed')
  with self.assertRaisesRegex(ValueError,'FINAL_OUTPUT_CHANGED'):request(self.manager,'job',['07','09'])
  self.store.item.assert_not_called();self.manager.enqueue_batch.assert_not_called()
  self.other['issues']=['DURATION_FIT_FAILED']
  with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):request(self.manager,'job',['09'])

 def test_final_qa_reuses_bytes_without_render_tts_or_plan_changes(self):
  before=self.out.read_bytes();self.manager.judge.return_value=dict(status='PASS',severity='NONE',issues=[])
  with patch('autocut.ai_recovery.Harness'),patch('autocut.ai_recovery.review_issues',return_value=[]),patch('autocut.ai_recovery.final_ready',return_value=True),patch('autocut.ai_recovery.audit'),patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.automatic_finish.final_job') as render:
   resume_final(self.manager,'job','07')
   self.assertEqual(self.item['status'],'PASS');tts.assert_not_called();render.assert_not_called()
  self.assertEqual(self.out.read_bytes(),before);self.assertEqual(json.loads(Path(self.item['plan']).read_text()),self.plan)
  self.assertEqual(self.store.done.call_args.args[3],'render-identity')

 def test_failed_judgment_does_not_mark_complete(self):
  self.manager.judge.side_effect=DriverError('AI_DRIVER_EXEC_FAILED','codex')
  with patch('autocut.ai_recovery.Harness'),patch('autocut.ai_recovery.audit'):
   resume_final(self.manager,'job','07')
  self.assertEqual(self.item['status'],'FAILED');self.store.done.assert_not_called()

 def test_driver_failure_keeps_category_without_prompt_or_credentials(self):
  def runner(args,**kwargs):
   if args[1:]==['login','status']:return subprocess.CompletedProcess(args,0,'Logged in','')
   return subprocess.CompletedProcess(args,1,'private output','ERROR: You have hit your usage limit. secret-should-not-be-saved')
  driver=CodexDriver(self.root/'driver',binary='/bin/echo',runner=runner)
  with self.assertRaisesRegex(DriverError,'AI_DRIVER_EXEC_FAILED'):driver._request('private prompt',{},[],PlanDecision)
  p=next((self.root/'driver/failures').glob('*.json'));data=json.loads(p.read_text())
  self.assertEqual(data['category'],'USAGE_LIMIT');self.assertEqual(data['returncode'],1)
  self.assertNotIn('private',p.read_text());self.assertNotIn('secret-',p.read_text());self.assertEqual(p.stat().st_mode&0o777,0o600)

 def test_driver_classifies_stdout_failures_without_saving_messages(self):
  for number,(message,category) in enumerate([
   ('ERROR: stream disconnected while reconnecting','NETWORK'),
   ('ERROR: unexpected status 503 Service Unavailable','SERVICE_UNAVAILABLE'),
   ('ERROR: invalid_request_error: invalid schema','INVALID_REQUEST'),
   ('ERROR: unknown failure','UNKNOWN'),
  ]):
   with self.subTest(category=category):
    def runner(args,**kwargs):
     if args[1:]==['login','status']:return subprocess.CompletedProcess(args,0,'Logged in','')
     return subprocess.CompletedProcess(args,1,message+' private-prompt secret-credential','')
    folder=self.root/f'stdout-driver-{number}'
    driver=CodexDriver(folder,binary='/bin/echo',runner=runner)
    with self.assertRaisesRegex(DriverError,'AI_DRIVER_EXEC_FAILED'):driver._request('private prompt',{},[],PlanDecision)
    path=next((folder/'failures').glob('*.json'));raw=path.read_text();data=json.loads(raw)
    self.assertEqual(data['category'],category);self.assertEqual(data['error_streams'],['stdout'])
    self.assertGreater(data['stdout_bytes'],0);self.assertEqual(data['stderr_bytes'],0)
    self.assertNotIn('private',raw);self.assertNotIn('secret-',raw);self.assertNotIn(message,raw)

 def saved_render(self):
  p=dict(self.plan,rank='09',output=str(self.root/'next.mp4'),source=dict(path=str(self.media),sha256=sha(self.media)),audio=dict(audio_path=str(self.media),sha256=sha(self.media)),montage_segments=[dict(path=str(self.media),sha256=sha(self.media))])
  self.other['plan']=str(self.work/'plans/09.json');write(self.other['plan'],p);return p

 def test_preflight_failure_resumes_frozen_plan_including_supplement(self):
  p=self.saved_render();request(self.manager,'job',['09'])
  self.manager.enqueue_batch.assert_called_once_with('job',[('09','AI_RENDER_QA')])
  with patch('autocut.ai_recovery.Harness') as harness,patch('autocut.ai_recovery.audit'),patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.pipeline.make_plan') as make:
   resume_render(self.manager,'job','09')
   self.manager.finish_plans.assert_called_once_with('job',harness.return_value.__enter__.return_value,self.job,[p],self.work)
   tts.assert_not_called();make.assert_not_called()

 def test_changed_supplement_blocks_recovery(self):
  p=self.saved_render();p['montage_segments'][0]['sha256']='changed';write(self.other['plan'],p)
  with self.assertRaisesRegex(ValueError,'RECOVERY_CACHE_INVALID'):request(self.manager,'job',['09'])
  self.store.item.assert_not_called();self.manager.enqueue_batch.assert_not_called()

class TransientVisualRetryTests(unittest.TestCase):
 def setUp(self):
  from autocut.pipeline import Manager
  self.judge=Manager.judge;self.m=MagicMock();self.m.store.checkpoint.return_value=None;self.m.store.settings.return_value={'ai_driver':'codex'};self.m.store.get.return_value={'driver_history':[]}
  self.m._judge_request=lambda *args,**kwargs:Manager._judge_request(self.m,*args,**kwargs)
  self.driver=self.m.router.resolve.return_value;self.driver.name='codex';self.driver.request_evidence.return_value={}
  self.ok=MagicMock();self.ok.model_dump.return_value={'status':'PASS','issues':[]}

 def test_transient_error_retries_only_judgment_once(self):
  self.driver.review_final.side_effect=[DriverError('AI_DRIVER_EXEC_FAILED','codex',True),self.ok]
  with patch('autocut.pipeline.time.sleep'),patch('autocut.item_review.audit') as audit:
   result=self.judge(self.m,'job',MagicMock(),'VISUAL_QA','final-09',{},'review_final',['same-evidence.jpg'])
  self.assertEqual(result['status'],'PASS');self.assertEqual(self.driver.review_final.call_count,2)
  self.assertEqual(self.driver.review_final.call_args_list[0],self.driver.review_final.call_args_list[1]);audit.assert_called_once();self.m.store.done.assert_called_once()

 def test_repeat_failure_is_bounded_and_never_saved_as_success(self):
  self.driver.review_final.side_effect=DriverError('AI_DRIVER_TIMEOUT','codex',True)
  with patch('autocut.pipeline.time.sleep'),patch('autocut.item_review.audit'):
   with self.assertRaises(DriverError):self.judge(self.m,'job',MagicMock(),'VISUAL_QA','final-09',{},'review_final',[])
  self.assertEqual(self.driver.review_final.call_count,2);self.m.store.done.assert_not_called()

 def test_non_transient_and_upstream_calls_do_not_retry(self):
  for stage,retryable in [('VISUAL_QA',False),('EDIT_PLAN',True)]:
   self.driver.review_final.reset_mock();self.driver.review_final.side_effect=DriverError('AI_DRIVER_EXEC_FAILED','codex',retryable)
   with self.assertRaises(DriverError):self.judge(self.m,'job',MagicMock(),stage,'09',{},'review_final',[])
   self.driver.review_final.assert_called_once();self.m.store.done.assert_not_called()

if __name__=='__main__':unittest.main()
