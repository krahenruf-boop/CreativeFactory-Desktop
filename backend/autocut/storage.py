import hashlib,json,sqlite3,threading,time,uuid,unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from contextlib import contextmanager
from .provider_models import MODEL_DEFAULTS,selected_models
from .title_policy import POLICY as TITLE_POLICY

def stamp():return time.time()
def product_name(value):
 if not isinstance(value,str):raise ValueError('PRODUCT_NAME_REQUIRED')
 value=unicodedata.normalize('NFC',value.strip())
 if not value or len(value)>100 or any(unicodedata.category(c).startswith('C') or c in '/\\' for c in value):raise ValueError('PRODUCT_NAME_REQUIRED')
 return value

class Store:
 def __init__(self,home):
  self.home=Path(home);self.home.mkdir(parents=True,exist_ok=True);self.path=self.home/'autocut.sqlite3';self.lock=threading.RLock()
  for n in ['config','products','jobs','cache/asr','cache/deepseek','cache/tts','runtime','logs','reports','user_assets']:(self.home/n).mkdir(parents=True,exist_ok=True)
  with self.connect() as db:
   db.executescript('''PRAGMA journal_mode=WAL;
   CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY,applied REAL);
   CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,state TEXT,data TEXT,created REAL,updated REAL);
   CREATE TABLE IF NOT EXISTS job_items(job_id TEXT,rank TEXT,data TEXT,PRIMARY KEY(job_id,rank));
   CREATE TABLE IF NOT EXISTS product_profiles(id TEXT PRIMARY KEY,data TEXT);
   CREATE TABLE IF NOT EXISTS app_settings(id TEXT PRIMARY KEY,data TEXT);
   CREATE TABLE IF NOT EXISTS provider_status(id TEXT PRIMARY KEY,data TEXT);
   CREATE TABLE IF NOT EXISTS checkpoints(job_id TEXT,stage TEXT,item TEXT,input_sha TEXT,data TEXT,PRIMARY KEY(job_id,stage,item));
   INSERT OR IGNORE INTO migrations VALUES(1,strftime('%s','now'));
   ''')
   version=db.execute('SELECT MAX(version) FROM migrations').fetchone()[0]
   if version>3:raise ValueError('APP_UPDATE_REQUIRED')
   db.execute('CREATE TABLE IF NOT EXISTS job_sequences(product TEXT,day TEXT,sequence INTEGER NOT NULL,PRIMARY KEY(product,day))')
   db.execute("INSERT OR IGNORE INTO migrations VALUES(2,strftime('%s','now'))")
   if 'deleted_at' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
    db.execute('ALTER TABLE jobs ADD COLUMN deleted_at REAL DEFAULT NULL')
   db.execute('CREATE INDEX IF NOT EXISTS jobs_deleted_created ON jobs(deleted_at,created)')
   db.execute("INSERT OR IGNORE INTO migrations VALUES(3,strftime('%s','now'))")
   db.execute("UPDATE jobs SET state='INTERRUPTED' WHERE deleted_at IS NULL AND state IN ('RUNNING','QUEUED','STOPPING')")
   # A process exit cannot leave an on-demand action looking permanently busy.
   for row in db.execute("SELECT job_id,rank,data FROM job_items WHERE data LIKE '%operation_inflight%'").fetchall():
    item=json.loads(row['data'])
    if item.get('operation_inflight'):
     item.update(operation_inflight=False,status='REVIEW_REQUIRED',issues=['ITEM_INTERRUPTED'],action_revision=item.get('action_revision',0)+1)
     db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(item,ensure_ascii=False),row['job_id'],row['rank']))
  for id,name,market in [('general','通用 · 按素材分析','Brazil'),('eye','凝胶眼影棒','Brazil'),('cream','瞬效紧致眼霜','Brazil'),('bag','MX 高端手机包','Mexico'),('belt','H标腰带扣','Brazil'),('brow','四色染眉膏','Brazil')]:
   if id in self.settings().get('deleted_preset_ids',[]):continue
   with self.connect() as db:db.execute('INSERT OR IGNORE INTO product_profiles VALUES(?,?)',(id,json.dumps(dict(id=id,product_name=name,market=market,language='es-MX' if market=='Mexico' else 'pt-BR',usage_scope='production',voice_preferences={'female':'m151rjrbWXbBqyq56tly'},marketing='Retain source-authorized promotions and effects; never invent facts.'))))
  for k,v in {'ai_driver':'auto','title_approval':'auto','watermark':'auto','open_output':True,'title_layout_policy':TITLE_POLICY,**{p+'_model':v for p,v in MODEL_DEFAULTS.items()}}.items():
   with self.connect() as db:db.execute('INSERT OR IGNORE INTO app_settings VALUES(?,?)',(k,json.dumps(v)))
 @contextmanager
 def connect(self):
  c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
  try:
   with c:yield c
  finally:c.close()
 def settings(self):
  with self.connect() as d:return {r['id']:json.loads(r['data']) for r in d.execute('SELECT * FROM app_settings')}
 def set_setting(self,key,value):
  self.set_settings({key:value})
 def set_settings(self,values):
  with self.connect() as d:d.executemany('INSERT OR REPLACE INTO app_settings VALUES(?,?)',[(k,json.dumps(v)) for k,v in values.items()])
 def products(self):
  with self.connect() as d:return [json.loads(r[0]) for r in d.execute('SELECT data FROM product_profiles')]
 def product(self,id):return next((p for p in self.products() if p['id']==id),None)
 def save_product(self,p):
  p=dict(p,id=p.get('id') or uuid.uuid4().hex)
  with self.connect() as d:d.execute('INSERT OR REPLACE INTO product_profiles VALUES(?,?)',(p['id'],json.dumps(p,ensure_ascii=False)))
  return p
 def next_name(self,name,db=None,now=None):
  name=product_name(name);day=datetime.fromtimestamp(stamp() if now is None else now,ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')
  if db is None:
   with self.connect() as connection:return self.next_name(name,connection,now)
  row=db.execute('SELECT sequence FROM job_sequences WHERE product=? AND day=?',(name.casefold(),day)).fetchone()
  sequence=(row[0] if row else 0)+1
  return dict(product_name=name,job_name=f'{name}_{day[4:]}_{sequence:02}',name_date=day,name_sequence=sequence)
 def create(self,request):
  with self.lock,self.connect() as db:
   # The write lock covers allocation and insertion, including separate Store instances.
   db.execute('BEGIN IMMEDIATE')
   row=db.execute('SELECT id FROM jobs WHERE request_id=?',(request['request_id'],)).fetchone()
   if row:return self.get(row[0]),False
   id=uuid.uuid4().hex;now=stamp();data=dict(request,id=id,stage='分析',progress=0,elapsed=0,issues=[],driver_history=[])
   settings={r['id']:json.loads(r['data']) for r in db.execute('SELECT * FROM app_settings')}
   data['provider_models']=selected_models(settings)
   naming=self.next_name(request.get('product_name',''),db,now);data.update(naming)
   db.execute('INSERT OR REPLACE INTO job_sequences VALUES(?,?,?)',(naming['product_name'].casefold(),naming['name_date'],naming['name_sequence']))
   db.execute('INSERT INTO jobs(id,request_id,state,data,created,updated) VALUES(?,?,?,?,?,?)',(id,request['request_id'],'QUEUED',json.dumps(data,ensure_ascii=False),now,now))
  from .runtime_insights import transition
  transition(self.home,id,'job',None,'QUEUED',data.get('production_preflight_policy'))
  return self.get(id),True
 def by_request(self,request_id):
  with self.connect() as db:row=db.execute('SELECT id FROM jobs WHERE request_id=?',(request_id,)).fetchone()
  return self.get(row[0]) if row else None
 def get(self,id):
  with self.connect() as db:
   r=db.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
   if not r:raise KeyError(id)
   data=json.loads(r['data']);data.update(state=r['state'],created=r['created'],updated=r['updated'],deleted_at=r['deleted_at'])
   data['items']=[json.loads(x[0]) for x in db.execute('SELECT data FROM job_items WHERE job_id=? ORDER BY rank',(id,))];return data
 def jobs(self,deleted=False):
  where='' if deleted is None else ' WHERE deleted_at IS NOT NULL' if deleted else ' WHERE deleted_at IS NULL'
  with self.connect() as db:ids=[r[0] for r in db.execute('SELECT id FROM jobs'+where+' ORDER BY created DESC,id')]
  return [self.get(i) for i in ids]
 def update(self,id,state=None,**fields):
  with self.lock,self.connect() as db:
   row=db.execute('SELECT state,data FROM jobs WHERE id=?',(id,)).fetchone();data=json.loads(row['data']);data.update(fields)
   db.execute('UPDATE jobs SET state=?,data=?,updated=? WHERE id=?',(state or row['state'],json.dumps(data,ensure_ascii=False),stamp(),id))
   from .runtime_insights import transition
   transition(self.home,id,'job',row['state'],state or row['state'],data.get('production_preflight_policy'))
 def item(self,id,rank,**fields):
  rank=f'{int(rank):02}'
  with self.lock,self.connect() as db:
   r=db.execute('SELECT data FROM job_items WHERE job_id=? AND rank=?',(id,rank)).fetchone();data=json.loads(r[0]) if r else {'rank':rank,'status':'WAITING'};previous=data.get('status');data.update(fields)
   if fields.get('status') in {'PASS','USER_APPROVED'}:
    from .ownership import assert_final
    assert_final(self,self.get(id),data,previous)
   if any(k not in {'presentation_zh','presentation_warnings'} for k in fields):data['action_revision']=data.get('action_revision',0)+1
   db.execute('INSERT OR REPLACE INTO job_items VALUES(?,?,?)',(id,rank,json.dumps(data,ensure_ascii=False)))
   from .runtime_insights import transition
   job=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(id,)).fetchone()[0])
   transition(self.home,id,rank,previous,data['status'],job.get('production_preflight_policy'))
 def checkpoint(self,id,stage,item,input_sha):
  with self.connect() as db:r=db.execute('SELECT input_sha,data FROM checkpoints WHERE job_id=? AND stage=? AND item IN (?,?) ORDER BY length(item) DESC LIMIT 1',(id,stage,str(item),f'{int(item):02}' if str(item).isdigit() and len(str(item))<=2 else str(item))).fetchone()
  return json.loads(r['data']) if r and r['input_sha']==input_sha else None
 def done(self,id,stage,item,input_sha,data):
  # Driver identity is provenance only, never part of stage identity.
  canonical=f'{int(item):02}' if str(item).isdigit() and len(str(item))<=2 else str(item)
  with self.lock,self.connect() as db:
   if canonical.isdigit() and len(canonical)==2:db.execute('DELETE FROM checkpoints WHERE job_id=? AND stage=? AND item=? AND item!=?',(id,stage,str(int(canonical)),canonical))
   db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)',(id,stage,canonical,input_sha,json.dumps(data,ensure_ascii=False)))
 def outputs(self):return [dict(x,job_id=j['id'],product=j['product_name'],job_name=j.get('job_name',j['product_name']),mode=j.get('mode','Transport')) for j in self.jobs() for x in j['items'] if x.get('output') and Path(x['output']).is_file()]
