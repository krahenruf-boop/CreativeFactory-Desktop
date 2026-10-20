"""Cooperative job control: stop at stage boundaries, never kill file writers."""
import threading
from contextlib import contextmanager
from concurrent.futures import wait


class Stopped(Exception):
 pass


HELD={'PAUSE','STOP'}
LABELS={'PAUSING':'正在暂停','PAUSED':'已暂停','STOPPING':'正在停止','STOPPED':'已停止'}


class JobControl:
 def __init__(self,manager):
  self.manager=manager;self.store=manager.store;self.workers={};self.parked={};self.wake={};self.stages={};self.deletion=None
 def event(self,id):return self.wake.setdefault(id,threading.Event())
 def mode(self,id):return self.store.get(id).get('control_request')
 def held(self,id):return self.mode(id) in HELD or bool(self.store.get(id).get('deleted_at'))
 def busy(self,id):
  f=self.manager.active.get(id)
  return bool(f and not f.done()) or any(j==id for j,_ in self.manager.item_pending) or bool(self.workers.get(id)) or any(not f.done() for f in self.stages.get(id,()))
 def view(self,j):
  busy=self.busy(j['id']);mode=j.get('control_request');state=j['state'];deleted=bool(j.get('deleted_at'))
  if mode=='PAUSE':state='PAUSED' if state=='PAUSED' else 'PAUSING'
  if mode=='STOP':state='STOPPING' if busy or self.editing(j['id']) else 'STOPPED'
  actions=[] if deleted else (['RESUME','DELETE'] if state=='PAUSED' else ['STOP','DELETE'] if state=='PAUSING' else [] if state=='STOPPING' else ['PAUSE','STOP','DELETE'] if busy else ['DELETE'])
  return dict(state=state,label=LABELS.get(state),worker_active=busy,worker_paused=bool(self.parked.get(j['id'])),actions=actions,delete_pending=bool(j.get('delete_pending')))
 def editing(self,id):return bool(self.deletion and self.deletion.edits.get(id))
 def stage(self,id,future):
  # Already dispatched ASR/TTS work must finish before declaring PAUSED.
  with self.store.lock,self.manager.lock:self.stages.setdefault(id,set()).add(future)
  def finished(_):
   with self.store.lock,self.manager.lock:
    self.stages[id].discard(future);self.settle(id)
  future.add_done_callback(finished)
  return future
 def register(self,id,spec):
  j=self.store.get(id)
  if j.get('deleted_at'):raise ValueError('JOB_DELETED')
  if self.held(id):raise ValueError('JOB_CONTROL_PENDING')
  specs=j.get('control_resume') or []
  if spec not in specs:self.store.update(id,control_resume=specs+[spec])
 def track(self,id,future,spec):
  def done(_):
   with self.store.lock,self.manager.lock:
    j=self.store.get(id);specs=j.get('control_resume') or []
    if spec in specs:specs.remove(spec)
    self.store.update(id,control_resume=specs)
    self.settle(id)
  future.add_done_callback(done)
  return future
 @contextmanager
 def worker(self,id):
  tid=threading.get_ident()
  with self.store.lock,self.manager.lock:self.workers.setdefault(id,set()).add(tid)
  try:
   self.check(id)
   yield
  finally:
   with self.store.lock,self.manager.lock:
    self.workers.setdefault(id,set()).discard(tid);self.parked.setdefault(id,set()).discard(tid)
    # A finishing child can leave only parked peers; no peer will run another
    # check until resumed. Settle PAUSED here as well as at the check boundary.
    if self.mode(id)=='PAUSE' and self.workers[id]<=self.parked[id] and not any(not f.done() for f in self.stages.get(id,())):
     self.store.update(id,state='PAUSED')
 def run(self,id,fn,*args):
  try:
   with self.worker(id):return fn(*args)
  except Stopped:return None
 def check(self,id):
  tid=threading.get_ident()
  while True:
   with self.store.lock,self.manager.lock:
    j=self.store.get(id);mode=j.get('control_request')
    if j.get('deleted_at') or mode=='STOP' or self.manager.stops.get(id,threading.Event()).is_set():raise Stopped()
    if mode!='PAUSE':
     self.parked.setdefault(id,set()).discard(tid);return
    self.parked.setdefault(id,set()).add(tid)
    pending=[f for f in self.stages.get(id,()) if not f.done()]
    if not pending and self.workers.get(id) and self.workers[id]<=self.parked[id]:self.store.update(id,state='PAUSED')
    event=self.event(id)
   if pending:
    wait(pending);continue
   event.wait() # Resume/stop wakes the same stack: no ASR, script or TTS replay.
 def request(self,id,mode,delete=False):
  with self.store.lock,self.manager.lock:
   j=self.store.get(id)
   if j.get('deleted_at'):return j
   if mode=='PAUSE' and j.get('control_request')=='PAUSE':return j
   if mode=='PAUSE' and j.get('control_request')=='STOP':raise ValueError('JOB_CONTROL_PENDING')
   if mode=='PAUSE' and not self.busy(id) and j.get('control_request')!='PAUSE':raise ValueError('JOB_NOT_RUNNING')
   saved=j.get('control_return_state') if j.get('control_request') else j['state']
   self.store.update(id,state='PAUSING' if mode=='PAUSE' else 'STOPPING',control_request=mode,control_return_state=saved,delete_pending=delete or bool(j.get('delete_pending')))
   if mode=='PAUSE':self.event(id).clear()
   else:self.manager.stops.setdefault(id,threading.Event()).set();self.event(id).set()
   self.settle(id)
   return self.store.get(id)
 def settle(self,id):
  # Called only after the Future is done and all deferred/edit leases are gone.
  if self.busy(id) or self.editing(id):return
  j=self.store.get(id);mode=j.get('control_request')
  if mode=='STOP':
   self.store.update(id,state='STOPPED')
   if j.get('delete_pending') and self.deletion:
    self.deletion.soft_delete(id);self.store.update(id,delete_pending=False)
  elif mode=='PAUSE':
   if j['state'] not in LABELS:self.store.update(id,control_return_state=j['state'])
   self.store.update(id,state='PAUSED')
 def resume(self,id):
  with self.store.lock,self.manager.lock:
   j=self.store.get(id)
   if j.get('deleted_at'):raise ValueError('JOB_DELETED')
   if not j.get('control_request') and self.busy(id):return j # duplicate click
   if j.get('control_request')!='PAUSE' or j['state']!='PAUSED':raise ValueError('JOB_NOT_PAUSED')
   busy=self.busy(id);specs=j.get('control_resume') or []
   self.store.update(id,state='RUNNING' if busy or specs else j.get('control_return_state','INTERRUPTED'),control_request=None,control_resume=specs if busy else [],delete_pending=False)
   self.event(id).set()
   if not busy:
    # Restart recovery uses existing durable caches; never recreate Items.
    for spec in specs:
     if spec['kind']=='job':self.manager.enqueue(id)
     elif spec['kind']=='titles':self.manager.prepare_batch_titles(id,spec['ranks'])
     else:self.manager.enqueue_batch(id,spec['rows'])
   return self.store.get(id)
 def recover(self):
  for j in self.store.jobs():
   if j.get('control_request')=='PAUSE':self.store.update(j['id'],state='PAUSED')
   elif j.get('control_request')=='STOP':self.settle(j['id'])
