"""Targeted control smoke with real thread workers; no media or provider calls."""
import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.pipeline import Manager
from autocut.server import create_app

class FixtureManager(Manager):
 def __init__(self,store):
  super().__init__(store,None,None);self.started=threading.Event();self.release=threading.Event();self.ticks={}
 def run(self,id):
  self.store.update(id,state='RUNNING');self.started.set();self.release.wait(5)
  for i in range(500):
   self.check(id);self.ticks[id]=self.ticks.get(id,0)+1
   self.store.done(id,'FIXTURE',1,'same',dict(count=self.ticks[id]))
   time.sleep(.015)
 def wait(self,predicate):
  end=time.monotonic()+4
  while not predicate():
   if time.monotonic()>end:raise AssertionError('worker state timeout')
   time.sleep(.01)

class ControlSmoke(unittest.TestCase):
 def test_inflight_stage_drains_before_paused(self):
  from concurrent.futures import ThreadPoolExecutor
  with tempfile.TemporaryDirectory() as temp:
   store=Store(Path(temp)/'data');m=FixtureManager(store);gate=threading.Event();ready=threading.Event();stage_pool=ThreadPoolExecutor(1)
   j,_=store.create(dict(request_id='external-stage',product_name='阶段隔离验收',market='Brazil',mode='Transport',input_path=temp,product_profile_id='general'));id=j['id']
   stage=m.control.stage(id,stage_pool.submit(gate.wait))
   def work(id):
    ready.set()
    while True:m.check(id);time.sleep(.01)
   m.run=work;m.enqueue(id);self.assertTrue(ready.wait(2))
   try:
    m.control.request(id,'PAUSE');time.sleep(.04);self.assertEqual(store.get(id)['state'],'PAUSING');self.assertFalse(stage.done())
    gate.set();m.wait(lambda:store.get(id)['state']=='PAUSED');self.assertTrue(stage.done())
   finally:
    gate.set();m.stop(id);m.queue.shutdown(wait=True);stage_pool.shutdown(wait=True)
 def test_stop_delete_waits_for_dispatched_stage(self):
  from concurrent.futures import ThreadPoolExecutor
  from autocut.job_deletion import JobDeletion
  with tempfile.TemporaryDirectory() as temp:
   store=Store(Path(temp)/'data');m=FixtureManager(store);gate=threading.Event();ready=threading.Event();pool=ThreadPoolExecutor(1);JobDeletion(store,m)
   j,_=store.create(dict(request_id='stop-external-stage',product_name='停止阶段验收',market='Brazil',mode='Transport',input_path=temp,product_profile_id='general'));id=j['id']
   stage=m.control.stage(id,pool.submit(gate.wait))
   def work(id):
    ready.set()
    while True:m.check(id);time.sleep(.01)
   m.run=work;m.enqueue(id);self.assertTrue(ready.wait(2))
   try:
    m.control.request(id,'STOP',delete=True);m.wait(lambda:m.active[id].done());self.assertIsNone(store.get(id)['deleted_at']);self.assertTrue(m.control.busy(id))
    gate.set();m.wait(lambda:bool(store.get(id)['deleted_at']));self.assertEqual(store.get(id)['state'],'STOPPED')
   finally:gate.set();m.stop(id);m.queue.shutdown(wait=True);pool.shutdown(wait=True)
 def test_control_smoke(self):
  with tempfile.TemporaryDirectory() as temp:
   store=Store(Path(temp)/'data');m=FixtureManager(store)
   app=create_app(store,None,None,m,'fixture',recover_on_startup=False)
   c=TestClient(app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer fixture'})
   source=Path(temp)/'source';output=Path(temp)/'final.mp4';source.write_bytes(b'readonly fixture');output.write_bytes(b'completed fixture')
   made=[]
   def make(state='FAILED'):
    j,_=store.create(dict(request_id='fixture-'+str(len(made)),product_name='控制隔离验收',market='Brazil',mode='Transport',input_path=temp,product_profile_id='general'))
    store.update(j['id'],state=state);store.item(j['id'],1,status='FAILED',source_path=str(source),output=str(output),issues=[]);made.append(j['id']);return j['id']
   def post(id,op):
    r=c.post('/jobs/'+id+'/'+op,json={});self.assertEqual(r.status_code,200,r.text);return r.json()
   try:
    a=make('RUNNING');m.enqueue(a);self.assertTrue(m.started.wait(2));f=m.active[a]
    self.assertEqual(post(a,'pause')['state'],'PAUSING');self.assertFalse(f.done())
    m.release.set();m.wait(lambda:store.get(a)['state']=='PAUSED')
    n=m.ticks.get(a,0);time.sleep(.06);self.assertEqual(m.ticks.get(a,0),n)
    self.assertTrue(m.control.parked[a]);post(a,'pause') # idempotent pause
    self.assertEqual(post(a,'resume')['state'],'RUNNING');m.wait(lambda:m.ticks.get(a,0)>n);self.assertIs(f,m.active[a])
    post(a,'stop');m.wait(lambda:store.get(a)['state']=='STOPPED');self.assertTrue(f.done());self.assertFalse(m.control.busy(a))
    self.assertTrue(post(a,'delete')['deleted_at']);self.assertIsNone(post(a,'restore')['deleted_at'])
    # Historical state alone is never evidence of a worker.
    for state in ['FAILED','WAITING_SUPPLEMENT_MEDIA','REVIEW_REQUIRED','PARTIAL_COMPLETED','RUNNING','PAUSED','STOPPED']:
     id=make(state);self.assertTrue(post(id,'delete')['deleted_at']);post(id,'restore')
    b=make('RUNNING');m.release.clear();m.started.clear();m.enqueue(b);self.assertTrue(m.started.wait(2))
    r=post(b,'stop-delete');self.assertEqual(r['state'],'STOPPING');self.assertFalse(r['deleted_at'])
    self.assertFalse(m.active[b].done());m.release.set();m.wait(lambda:bool(store.get(b)['deleted_at']));self.assertTrue(m.active[b].done())
    post(b,'restore');self.assertIsNone(store.get(b)['deleted_at'])
    # Batch workers must all park, stop and drain; no later item may start work.
    batch=make('RUNNING');entered=set()
    def item(manager,id,rank,phase):
     entered.add(rank)
     for _ in range(500):manager.check(id);time.sleep(.01)
    with patch('autocut.item_pipeline.resume_item',item):
     m.enqueue_batch(batch,[('01','TITLE'),('02','TITLE'),('03','TITLE')]);m.wait(lambda:len(entered)==2)
     post(batch,'pause');m.wait(lambda:store.get(batch)['state']=='PAUSED');self.assertEqual(len(m.control.parked[batch]),2)
     post(batch,'stop-delete');m.wait(lambda:bool(store.get(batch)['deleted_at']));self.assertNotIn('03',entered)
    # Restart preserves pause and does not auto-enqueue a paid pipeline.
    paused=make('PAUSED');store.update(paused,control_request='PAUSE',control_resume=[{'kind':'job'}])
    reopened=Store(store.home);other=Manager(reopened,None,None);other.control.recover();other.recover_jobs();self.assertEqual(reopened.get(paused)['state'],'PAUSED');self.assertFalse(other.active);other.queue.shutdown(wait=True)
    self.assertEqual(source.read_bytes(),b'readonly fixture');self.assertEqual(output.read_bytes(),b'completed fixture')
    self.assertIsNone(reopened.get(a)['deleted_at']);self.assertTrue(reopened.get(batch)['deleted_at'])
   finally:
    m.release.set()
    for id in made:
     if m.control.busy(id):m.stop(id)
    m.queue.shutdown(wait=True);c.close()

if __name__=='__main__':unittest.main()
