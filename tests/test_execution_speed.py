"""Real queues/SQLite/cache, deterministic visual driver; no paid requests."""
import copy,json,sqlite3,tempfile,threading,time,unittest
from concurrent.futures import Future,ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from autocut import context
from autocut.storage import Store
from autocut.pipeline import Manager
from autocut.source_analysis import analyze_sources
from autocut.drivers import AIDriver,Analysis,Health
from app.s3.headless_runtime import Harness,sha


class LocalDriver(AIDriver):
 name='fixture'
 def __init__(self):
  self.model='fixture-model';self._request_local=threading.local();self.lock=threading.Lock();self.active=0;self.peak=0;self.calls=0;self.started=threading.Event();self.release=None
 def health(self):return Health(driver=self.name,status='READY',detail='fixture')
 def _request(self,task,inputs,images,schema):
  with self.lock:self.calls+=1;self.active+=1;self.peak=max(self.peak,self.active);self.started.set()
  try:
   if self.release:self.release.wait(3)
   time.sleep(.07)
   self.record_request(self.model,images)
   return Analysis(source_language='en',clean_script='A compact product',voice_quality='GOOD',speaker='unknown',context='A real product fixture',selling_points=['compact'],allowed_claims=['compact'],promotion_context='',group_key='same',existing_arrow=False,overlays_need_review=False,overlay_notes='',people_present=False,product_description='A compact product',main_actions=['demonstration'],protected_regions=[dict(kind='product',bbox=[.2,.2,.8,.8],start=0,end=1,description='product')])
  finally:
   with self.lock:self.active-=1


class SpeedTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.store=Store(self.root/'data');self.driver=LocalDriver()
  class Router:
   def __init__(self,d):self.drivers={d.name:d};self.driver=d
   def resolve(self,*args):return self.driver
  self.m=Manager(self.store,None,Router(self.driver));self.addCleanup(lambda:self.m.queue.shutdown(wait=True))
  self.image=self.root/'frames.jpg';self.image.write_bytes(b'exact frame evidence')
  self.sources=[]
  for rank in range(1,4):
   p=self.root/f'{rank}.mp4';p.write_bytes(str(rank).encode());self.sources.append(dict(rank=rank,path=str(p),filename=p.name,sha256=sha(p),duration=2.,width=1080,height=1920,status='VALID'))
 def job(self,n):
  j,_=self.store.create(dict(request_id='speed-'+str(n),input_path=str(self.root),product_name='cache fixture',market='Brazil',mode='Remix',watermark_mode='handled',source_count=3))
  return j
 def inputs(self):return dict(source={k:self.sources[0][k] for k in ['sha256','duration','width','height']},asr={'text':'A compact product'},product={'product_name':'cache fixture'})
 def batch(self,j):
  asr=[]
  for source in self.sources:
   f=Future();f.set_result(dict(text='A compact product',language='en'));asr.append((source,f))
  with Harness(self.root/'work') as h,patch('autocut.pipeline.source_sheet',return_value=str(self.image)):
   return analyze_sources(self.m,j['id'],h,j,dict(product_name='cache fixture'),asr,self.root/'work',True)
 def wait(self,predicate):
  until=time.monotonic()+4
  while not predicate():
   if time.monotonic()>until:raise AssertionError('queue checkpoint timeout')
   time.sleep(.01)
 def test_two_sources_overlap_and_keep_natural_order_and_all_history(self):
  j=self.job(1);started=time.monotonic();rows=self.batch(j);wall=time.monotonic()-started
  self.assertEqual(self.driver.peak,2);self.assertEqual(self.driver.calls,3);self.assertEqual([r['source']['rank'] for r in rows],[1,2,3])
  self.assertEqual(self.store.get(j['id'])['analyzed_count'],3);self.assertEqual(len(self.store.get(j['id'])['driver_history']),3)
  with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from checkpoints where job_id=? and stage=?',(j['id'],'VIDEO_ANALYSIS')).fetchone()[0],3)
  # Different task, identical sources/context: zero additional driver requests.
  self.batch(self.job(2));self.assertEqual(self.driver.calls,3)
  print('source concurrency peak=2; cold fixture wall=%.3fs; next task adds 0 requests'%wall)
 def test_same_evidence_single_flight_and_image_change_invalidates(self):
  a,b=self.job(1),self.job(2);inputs=self.inputs()
  with Harness(self.root/'work') as h,ThreadPoolExecutor(2) as pool:
   calls=[pool.submit(self.m.judge,j['id'],h,'VIDEO_ANALYSIS',1,inputs,'analyze',[self.image]) for j in [a,b]]
   self.assertEqual([f.result()['product_description'] for f in calls],['A compact product']*2)
   self.assertEqual(self.driver.calls,1)
   self.image.write_bytes(b'changed frame evidence')
   self.m.judge(a['id'],h,'VIDEO_ANALYSIS',1,inputs,'analyze',[self.image]);self.assertEqual(self.driver.calls,2)
   changed=copy.deepcopy(inputs);changed['source']['sha256']='changed-source'
   self.m.judge(a['id'],h,'VIDEO_ANALYSIS',1,changed,'analyze',[self.image]);self.assertEqual(self.driver.calls,3)
 def test_corrupt_cache_never_becomes_a_pass(self):
  a,b=self.job(1),self.job(2);inputs=self.inputs()
  with Harness(self.root/'work') as h:
   self.m.judge(a['id'],h,'VIDEO_ANALYSIS',1,inputs,'analyze',[self.image])
   p=next(self.m.analysis_cache.root.glob('*.json'));data=json.loads(p.read_text());data['result']['product_description']='';p.write_text(json.dumps(data))
   self.m.judge(b['id'],h,'VIDEO_ANALYSIS',1,inputs,'analyze',[self.image]);self.assertEqual(self.driver.calls,2)
 def test_database_connections_close_and_failed_writes_roll_back(self):
  from autocut.feishu_tasks.storage import Journal
  for owner in [self.store,Journal(self.store.home)]:
   with owner.connect() as db:db.execute('SELECT 1').fetchone()
   with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
  with self.assertRaises(ValueError):
   with self.store.connect() as db:
    db.execute("INSERT INTO app_settings VALUES('rollback-fixture','1')");raise ValueError('rollback')
  self.assertNotIn('rollback-fixture',self.store.settings())
 def test_pause_parks_parent_and_queued_sources_without_new_requests(self):
  j=self.job(1);self.driver.release=threading.Event();done=threading.Event()
  def run(id):
   self.store.update(id,state='RUNNING');self.batch(self.store.get(id));done.set()
  self.m.run=run;self.m.enqueue(j['id']);self.assertTrue(self.driver.started.wait(2));self.wait(lambda:self.driver.active==2)
  self.m.control.request(j['id'],'PAUSE');self.driver.release.set();self.wait(lambda:self.store.get(j['id'])['state']=='PAUSED')
  self.assertEqual(self.driver.calls,2);self.assertFalse(done.is_set())
  self.m.control.resume(j['id']);self.assertTrue(done.wait(3));self.assertEqual(self.driver.calls,3)
 def test_restart_resumes_only_durable_initial_worker(self):
  j=self.job(1);self.store.update(j['id'],state='RUNNING',control_resume=[dict(kind='job')]);reopened=Store(self.store.home)
  other=Manager(reopened,None,self.m.router);started=threading.Event();other.run=lambda id:started.set()
  try:
   other.recover_jobs();self.assertTrue(started.wait(2));self.assertIn(j['id'],other.active)
  finally:other.queue.shutdown(wait=True)
 def test_web_confirmation_barrier_defers_all_edit_plan_requests(self):
  from test_copy_presentation import response
  from unittest.mock import MagicMock
  j=self.job(1);self.store.update(j['id'],mode='Transport',batch_review_required=True,content_confirmation_required=True,title_approval_mode='manual',product_profile=dict(product_name='cache fixture'))
  j=self.store.get(j['id']);work=self.store.home/'jobs'/j['id']/'work';work.mkdir(parents=True)
  (work/'manifest.json').write_text(json.dumps(dict(sources=self.sources)))
  analysis=Analysis(source_language='en',clean_script='A compact product',voice_quality='GOOD',speaker='unknown',context='A product demonstration',selling_points=['compact'],allowed_claims=['compact'],promotion_context='',group_key='same',existing_arrow=False,overlays_need_review=False,overlay_notes='',product_description='A compact product',main_actions=['demonstration'],protected_regions=[dict(kind='product',bbox=[.2,.2,.8,.8],start=0,end=1,description='product')]).model_dump()
  prepared=[dict(source=s,asr_result={},transcript_path=None,transcript=dict(text='A compact product',language='en'),sheet=str(self.image),task={},analysis=analysis) for s in self.sources]
  future=Future();future.set_result({});self.m.judge=MagicMock(side_effect=AssertionError('No edit plan before confirmation'))
  self.m.prepare_copies=lambda contexts,work,model:{c['script_group_id']:dict(response=response()) for c in contexts}
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.preserve_job_sources',return_value=j),patch('autocut.pipeline.asr_job',return_value=future),patch('autocut.pipeline.analyze_sources',return_value=prepared):self.m.run(j['id'])
  out=self.store.get(j['id']);self.assertEqual(out['state'],'WAITING_SCRIPT_APPROVAL');self.assertEqual(len(out['items']),3)
  self.assertTrue(all(x['decision'] is None and len(x['title_candidates'])==3 for x in out['items']));self.m.judge.assert_not_called()

if __name__=='__main__':unittest.main()
