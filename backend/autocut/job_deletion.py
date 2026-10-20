"""Job management only. Never follows payload paths to delete media or shared data."""
import os,re,shutil,time
from contextlib import contextmanager
from pathlib import Path

INACTIVE={'COMPLETED','REVIEW_REQUIRED','INTERRUPTED','ABANDONED','FAILED','WAITING_SCRIPT_APPROVAL','WAITING_TITLE_APPROVAL'}
JOB_ID=re.compile(r'^[a-f0-9]{32}$')

class JobDeletion:
 def __init__(self,store,manager):
  self.store,self.manager=store,manager;self.edits={}
  if hasattr(manager,'control'):manager.control.deletion=self
  self.recover()
 def running(self,j):
  future=getattr(self.manager,'active',{}).get(j['id'])
  control=getattr(self.manager,'control',None)
  busy=control.busy(j['id']) if control else bool(future and not future.done()) or any(job==j['id'] for job,_ in getattr(self.manager,'item_pending',set()))
  return busy or bool(self.edits.get(j['id']))
 def can_delete(self,j):return not j.get('deleted_at') and not self.running(j)
 @contextmanager
 def edit(self,id):
  # A lease spans async operations too; a deletion cannot race approval/translation.
  with self.store.lock:
   if self.store.get(id).get('deleted_at'):raise ValueError('JOB_DELETED')
   if self.store.get(id).get('control_request'):raise ValueError('JOB_CONTROL_PENDING')
   self.edits[id]=self.edits.get(id,0)+1
  try:yield
  finally:
   with self.store.lock:
    self.edits[id]-=1
    if hasattr(self.manager,'control'):
     with self.manager.lock:self.manager.control.settle(id)
 def soft_delete(self,id):
  with self.store.lock,self.store.connect() as db:
   db.execute('BEGIN IMMEDIATE');j=self.store.get(id)
   if self.running(j):raise ValueError('JOB_RUNNING')
   db.execute('UPDATE jobs SET deleted_at=COALESCE(deleted_at,?) WHERE id=?',(time.time(),id))
  return self.store.get(id)
 def restore(self,id):
  with self.store.lock,self.store.connect() as db:
   self.store.get(id);db.execute('UPDATE jobs SET deleted_at=NULL WHERE id=?',(id,))
  return self.store.get(id)
 def locations(self,id):
  if not JOB_ID.fullmatch(id):raise ValueError('JOB_PATH_UNSAFE')
  home=self.store.home.resolve();jobs=home/'jobs';reports=home/'reports'
  if jobs.is_symlink() or reports.is_symlink():raise ValueError('JOB_PATH_UNSAFE')
  work=jobs/id;report=reports/(id+'.md');pending=jobs/('.purge-'+id)
  if any(x.is_symlink() for x in [work,report,pending]):raise ValueError('JOB_PATH_UNSAFE')
  return work,report,pending
 @staticmethod
 def strings(value):
  if isinstance(value,str):yield value
  elif isinstance(value,dict):
   for v in value.values():yield from JobDeletion.strings(v)
  elif isinstance(value,list):
   for v in value:yield from JobDeletion.strings(v)
 def protect(self,j,work,report):
  # Own input and exported outputs remain protected even if unusually placed in a workspace.
  protected=[j.get('input_path','')]
  for item in j['items']:protected.extend(item.get(k,'') for k in ['source_path','output'])
  for other in self.store.jobs(deleted=None):
   if other['id']!=j['id']:protected.extend(self.strings(other))
  for raw in protected:
   if not raw or not raw.startswith(('/','~/')):continue
   path=Path(raw).expanduser().resolve()
   if path==report or path==work or work in path.parents:raise ValueError('JOB_DATA_IN_USE')
  if work.exists() and not work.is_dir() or report.exists() and not report.is_file():raise ValueError('JOB_PATH_UNSAFE')
 def recover_one(self,id):
  work,report,pending=self.locations(id)
  if not pending.exists():return
  # The only names ever created by this component; do not follow nested symlinks.
  if not pending.is_dir() or any(x.name not in {'workspace','report.md'} for x in pending.iterdir()):raise ValueError('JOB_PATH_UNSAFE')
  try:self.store.get(id);exists=True
  except KeyError:exists=False
  if exists:
   for src,dst in [(pending/'workspace',work),(pending/'report.md',report)]:
    if src.exists() or src.is_symlink():
     if dst.exists() or dst.is_symlink():raise ValueError('JOB_PATH_UNSAFE')
     os.replace(src,dst)
  shutil.rmtree(pending)
 def recover(self):
  jobs=self.store.home/'jobs'
  if jobs.is_symlink():return
  for entry in jobs.glob('.purge-*'):
   id=entry.name[len('.purge-'):]
   if JOB_ID.fullmatch(id):
    try:self.recover_one(id)
    except (OSError,ValueError):pass # Keep recoverable data; never broaden cleanup scope.
 def purge(self,id):
  with self.store.lock:
   j=self.store.get(id)
   if not j.get('deleted_at'):raise ValueError('JOB_NOT_DELETED')
   if self.running(j):raise ValueError('JOB_RUNNING')
   work,report,pending=self.locations(id);self.recover_one(id);self.protect(j,work,report)
   try:
    pending.mkdir()
    if work.exists():os.replace(work,pending/'workspace')
    if report.exists():os.replace(report,pending/'report.md')
    with self.store.connect() as db:
     db.execute('BEGIN IMMEDIATE')
     # Delete only task-owned rows; name sequences, presets and caches remain untouched.
     db.execute('DELETE FROM checkpoints WHERE job_id=?',(id,))
     db.execute('DELETE FROM job_items WHERE job_id=?',(id,))
     db.execute('DELETE FROM jobs WHERE id=? AND deleted_at IS NOT NULL',(id,))
    shutil.rmtree(pending)
   except OSError:
    self.recover_one(id)
    raise ValueError('JOB_DELETE_FAILED') from None
   except Exception:
    self.recover_one(id)
    raise
  return dict(status='PURGED',outputs_preserved=True)
