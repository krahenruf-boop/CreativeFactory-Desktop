import copy,json,sqlite3,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from autocut.remix import remix_items,apply_montage

class NamesTests(unittest.TestCase):
 def test_atomic_daily_sequence_idempotency_and_reopen(self):
  with tempfile.TemporaryDirectory() as d,patch('autocut.storage.stamp',return_value=1790726400): # 2026-09-30 CST
   a=Store(d);b=Store(d)
   self.assertEqual(a.next_name('凝胶提拉霜')['job_name'],'凝胶提拉霜_0930_01')
   def create(i):return (a if i%2 else b).create(dict(request_id=str(i),product_name='凝胶提拉霜'))[0]
   with ThreadPoolExecutor(max_workers=4) as pool:jobs=list(pool.map(create,range(8)))
   self.assertEqual(sorted(j['name_sequence'] for j in jobs),list(range(1,9)))
   self.assertEqual(a.create(dict(request_id='0'))[0]['id'],jobs[0]['id'])
   a.done(jobs[0]['id'],'TTS_DONE','01','hash',{'cached':'yes'})
   reopened=Store(d)
   self.assertEqual(reopened.next_name('凝胶提拉霜')['name_sequence'],9)
   self.assertEqual(reopened.checkpoint(jobs[0]['id'],'TTS_DONE','01','hash'),{'cached':'yes'})
   self.assertEqual(reopened.next_name('另一产品')['name_sequence'],1)
   self.assertEqual(reopened.next_name('凝胶提拉霜',now=1790812800)['job_name'],'凝胶提拉霜_1001_01')
 def test_api_name_is_independent_and_mode_reaches_manager(self):
  with tempfile.TemporaryDirectory() as d:
   class Manager:
    def __init__(self):self.started=[]
    def enqueue(self,id):self.started.append(id)
   store=Store(d);manager=Manager();client=TestClient(create_app(store,None,None,manager,'local-test'),base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer local-test'})
   client.post('/native/grant',json={'path':d})
   preview=client.get('/job-name',params={'product_name':'凝胶提拉霜'}).json()
   spec=dict(request_id='unique-request-name',input_path=d,product_profile_id='eye',product_name='凝胶提拉霜',mode='Remix',output_count=3)
   r=client.post('/jobs',json=spec);self.assertEqual(r.status_code,200,r.text);j=r.json()
   self.assertEqual(j['job_name'],preview['job_name']);self.assertEqual(j['product_name'],'凝胶提拉霜');self.assertEqual(j['product_profile']['product_name'],'凝胶提拉霜')
   self.assertEqual(store.product('eye')['product_name'],'凝胶眼影棒');self.assertEqual(j['mode'],'Remix');self.assertEqual(j['output_count'],3)
   self.assertEqual(client.post('/jobs',json=spec).json()['id'],j['id']);self.assertEqual(manager.started,[j['id']])
   self.assertEqual(client.post('/jobs',json=dict(spec,request_id='invalid-name-id',product_name='   ')).status_code,400)
   self.assertEqual(client.post('/jobs',json=dict(spec,request_id='invalid-count-id',output_count=0)).status_code,422)
   self.assertEqual(client.post('/jobs',json=dict(spec,request_id='invalid-mode-id',mode='Random')).status_code,400)
 def test_v1_upgrade_retains_old_name(self):
  with tempfile.TemporaryDirectory() as d:
   store=Store(d);j,_=store.create(dict(request_id='legacy',product_name='原有产品'))
   with store.connect() as db:
    data=json.loads(db.execute('SELECT data FROM jobs').fetchone()[0]);data.pop('job_name');db.execute('UPDATE jobs SET data=?',(json.dumps(data),));db.execute('DROP TABLE job_sequences');db.execute('DELETE FROM migrations WHERE version=2')
   reopened=Store(d);self.assertNotIn('job_name',reopened.get(j['id']));self.assertEqual(reopened.get(j['id'])['product_name'],'原有产品')
   with reopened.connect() as db:self.assertEqual(db.execute('SELECT max(version) FROM migrations').fetchone()[0],2)

class RemixTests(unittest.TestCase):
 def test_ranges_timeline_and_hash_guards(self):
  from app.s3.headless_runtime import sha
  from app.s3.headless_media import validate_montage
  with tempfile.TemporaryDirectory() as d:
   sources=[]
   for i in [1,2]:
    p=Path(d)/f'{i}.mp4';p.write_bytes(f'fixture-{i}'.encode());sources.append(dict(rank=i,path=str(p),sha256=sha(p),duration=6,filename=p.name))
   a={i:dict(clean_script='source text',context='product in hand',selling_points=['compact'],allowed_claims=['compact'],overlays_need_review=False,existing_arrow=False) for i in [1,2]}
   selection=dict(outputs=[dict(rank=1,primary_source_rank=1,angle='demonstration',clips=[dict(source_rank=i,start=0,end=3,reason='product') for i in [1,2]])])
   items,_=remix_items(selection,sources,a,1);plan=dict(target_duration=5.6,source_ranges=[[0,6]],video_speed=1.1)
   audited=dict(selection,driver_provenance={'actual_driver':'codex'},source_arrow_hint=False,subject_protection={})
   self.assertEqual(remix_items(audited,sources,a,1)[0],items)
   with self.assertRaises(ValueError):remix_items(dict(audited,path='/untrusted/video.mp4'),sources,a,1)
   apply_montage(plan,items[0]);validate_montage(plan);self.assertEqual(len(plan['montage_segments']),2);self.assertNotIn('source_ranges',plan)
   self.assertAlmostEqual(plan['montage_segments'][-1]['output_end'],5.6)
   bad=copy.deepcopy(selection);bad['outputs'][0]['clips'][1]['source_rank']=99
   with self.assertRaisesRegex(ValueError,'REMIX_PLAN_INVALID'):remix_items(bad,sources,a,1)
   bad=copy.deepcopy(selection);bad['outputs'][0]['clips'][0]['end']=60
   with self.assertRaisesRegex(ValueError,'REMIX_PLAN_INVALID'):remix_items(bad,sources,a,1)
   with self.assertRaisesRegex(ValueError,'REMIX_PLAN_INVALID'):remix_items(selection,sources,a,2)
   Path(sources[1]['path']).write_bytes(b'changed')
   with self.assertRaisesRegex(ValueError,'MONTAGE_SOURCE_HASH_CHANGED'):validate_montage(plan)

if __name__=='__main__':unittest.main()
