"""Bounded visual requests and exact-evidence cache shared across local jobs."""
import copy,fcntl,hashlib,inspect,json,threading,time
from contextlib import contextmanager
from pathlib import Path
from app.s3.headless_runtime import fingerprint,read,write,sha


class RequestGate:
 def __init__(self,limit=2):
  self.limit=limit;self.active=0;self.condition=threading.Condition()
 @contextmanager
 def enter(self,check):
  started=time.monotonic()
  while True:
   check()
   with self.condition:
    if self.active<self.limit:
     self.active+=1;break
    self.condition.wait(.2)
  try:yield time.monotonic()-started
  finally:
   with self.condition:self.active-=1;self.condition.notify_all()
 def rate_limited(self):
  with self.condition:self.limit=max(1,self.limit-1);self.condition.notify_all()


class AnalysisCache:
 VERSION='SOURCE_ANALYSIS_CACHE_V1'
 def __init__(self,home):self.root=Path(home)/'cache/visual_analysis'
 def key(self,inputs,images,driver):
  from .drivers import AIDriver,Analysis,SYSTEM
  identity=dict(driver=driver.name,model=getattr(driver,'model',None))
  binary=getattr(driver,'binary',None)
  if binary:
   stat=Path(binary).stat();identity['binary']=dict(path=str(Path(binary).resolve()),size=stat.st_size,mtime=stat.st_mtime_ns)
  contract=hashlib.sha256((SYSTEM+inspect.getsource(AIDriver.analyze)+inspect.getsource(type(driver)._request)+json.dumps(Analysis.model_json_schema(),sort_keys=True)).encode()).hexdigest()
  return fingerprint(dict(version=self.VERSION,contract=contract,driver=identity,inputs=inputs,images=[sha(p) for p in images]))
 @contextmanager
 def lock(self,key,check):
  self.root.mkdir(parents=True,exist_ok=True)
  with (self.root/(key+'.lock')).open('a') as stream:
   while True:
    check()
    try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);break
    except BlockingIOError:time.sleep(.1)
   try:yield
   finally:fcntl.flock(stream,fcntl.LOCK_UN)
 @staticmethod
 def valid(value,inputs):
  from .drivers import Analysis
  try:
   payload=copy.deepcopy(value)
   for key in ('driver_provenance','visual_evidence_version'):payload.pop(key,None)
   if payload.get('title_blur_region'):
    for key in ('source_sha256','evidence'):payload['title_blur_region'].pop(key,None)
   a=Analysis.model_validate(payload);duration=inputs['source']['duration']
   if not a.product_description.strip() or not a.main_actions or not a.protected_regions:return False
   if not any(r.kind=='product' for r in a.protected_regions):return False
   if a.people_present and not any(r.kind in {'person','face','hands'} for r in a.protected_regions):return False
   return all(0<=r.bbox[0]<r.bbox[2]<=1 and 0<=r.bbox[1]<r.bbox[3]<=1 and 0<=r.start<=r.end<=duration for r in a.protected_regions)
  except (ValueError,TypeError,KeyError):return False
 def load(self,key,inputs):
  try:
   data=read(self.root/(key+'.json'),{})
   if data.get('version')!=self.VERSION or data.get('key')!=key:return None
   value=data['result']
   if data.get('result_sha256')!=fingerprint(value) or not self.valid(value,inputs):return None
   return data
  except (OSError,ValueError,KeyError,TypeError):return None
 def save(self,key,inputs,result,origin):
  if self.valid(result,inputs):write(self.root/(key+'.json'),dict(version=self.VERSION,key=key,result=result,result_sha256=fingerprint(result),origin=origin))
