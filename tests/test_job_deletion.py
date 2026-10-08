import copy,importlib.util,json,os,shutil,tempfile,threading,unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from autocut.job_deletion import JobDeletion

class Manager:
 def __init__(self):self.active={};self.enqueued=[]
 def enqueue(self,id):self.enqueued.append(id);self.active[id]=Future()
 def stop(self,id):pass

class JobDeletionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data');self.manager=Manager()
  self.app=create_app(self.store,None,None,self.manager,'local-fixture');self.service=self.app.state.deletion
  self.client=TestClient(self.app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer local-fixture'})
  self.source=self.root/'source.mp4';self.source.write_bytes(b'fixture source; not rendered')
  self.output=self.root/'outputs/final.mp4';self.output.parent.mkdir();self.output.write_bytes(b'fixture output; not rendered')
  self.j=self.make();self.id=self.j['id']
 def tearDown(self):self.client.close();self.tmp.cleanup()
 def make(self,state='COMPLETED'):
  j,_=self.store.create(dict(request_id=os.urandom(12).hex(),product_name='太阳镜',market='Brazil',mode='Transport',input_path=str(self.root),product_profile_id='general',title_approval_mode='auto'))
  self.store.update(j['id'],state=state,progress=100,stage='检查',custom_instruction_raw='不加CTA',resolved_overrides_json={'cta_enabled':False},completion_notified=True)
  self.store.item(j['id'],1,status='PASS',filename='source.mp4',source_path=str(self.source),output=str(self.output),title='Olha só',title_text_zh='看看这个',copy={'localized_script':{'text':'Veja este detalhe','text_zh':'看看这个细节'}})
  self.store.done(j['id'],'QA_DONE','01','hash',{'qa':'PASS'})
  w=self.store.home/'jobs'/j['id']/'work';w.mkdir(parents=True);(w/'plan.json').write_text('{"fixture":true}')
  (self.store.home/'reports'/(j['id']+'.md')).write_text('fixture report')
  return self.store.get(j['id'])
 def post(self,op,**body):return self.client.post('/jobs/'+self.id+'/'+op,json=body)
 def test_01_soft_delete_list_partition_and_no_content_changes(self):
  before=self.store.get(self.id);r=self.post('delete');self.assertEqual(r.status_code,200,r.text)
  self.assertEqual(self.client.get('/jobs').json(),[]);self.assertEqual(len(self.client.get('/jobs?deleted=true').json()),1)
  after=self.store.get(self.id);after['deleted_at']=None;self.assertEqual(before,after)
  self.assertTrue((self.store.home/'jobs'/self.id/'work/plan.json').is_file())
 def test_02_restore_exact_original_snapshot(self):
  before=self.store.get(self.id);self.post('delete');self.post('restore');self.assertEqual(self.store.get(self.id),before)
 def test_03_all_executing_states_blocked(self):
  for state in ['RUNNING','PROCESSING','WAITING_AI','RENDERING','QUEUED','STOPPING','UNKNOWN_NEW_STATE']:
   with self.subTest(state=state):
    self.store.update(self.id,state=state);r=self.post('delete');self.assertEqual(r.json()['error'],'JOB_RUNNING');self.assertIsNone(self.store.get(self.id)['deleted_at'])
 def test_04_worker_finishing_and_queued_future_blocked(self):
  f=Future();self.manager.active[self.id]=f;self.assertEqual(self.post('delete').json()['error'],'JOB_RUNNING');f.set_result(None);self.assertEqual(self.post('delete').status_code,200)
 def test_05_schema2_migration_preserves_historical_rows(self):
  with self.store.connect() as db:
   db.execute('DROP INDEX jobs_deleted_created');db.execute('ALTER TABLE jobs DROP COLUMN deleted_at');db.execute('DELETE FROM migrations WHERE version=3')
   before=[tuple(r) for r in db.execute('SELECT * FROM jobs')]
  new=Store(self.store.home)
  with new.connect() as db:
   self.assertEqual([tuple(r)[:-1] for r in db.execute('SELECT * FROM jobs')],before);self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)
  self.assertIsNone(new.get(self.id)['deleted_at'])
 def test_06_restart_retains_deleted_state_and_content(self):
  self.post('delete');before=self.store.get(self.id);new=Store(self.store.home);self.assertEqual(new.jobs(),[]);self.assertEqual(new.get(self.id),before)
 def test_07_api_replacement_retains_deleted_state(self):
  self.post('delete');c=TestClient(create_app(Store(self.store.home),None,None,Manager(),'local-fixture'),base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer local-fixture'})
  self.assertEqual(c.get('/jobs').json(),[]);self.assertEqual(c.get('/jobs?deleted=true').json()[0]['id'],self.id);c.close()
 def test_08_deleted_detail_readable_production_routes_blocked(self):
  self.post('delete');self.assertEqual(self.client.get('/jobs/'+self.id).status_code,200)
  for route in ['approve-scripts','approve-titles','retry','stop','abandon','translations']:
   with self.subTest(route=route):self.assertEqual(self.post(route).json()['error'],'JOB_DELETED')
  self.assertEqual(self.manager.enqueued,[])
 def test_09_deleted_video_still_playable(self):
  self.post('delete');r=self.client.get('/outputs/'+self.id+'/01/video');self.assertEqual(r.content,self.output.read_bytes());self.assertEqual(self.store.outputs(),[])
 def test_10_purge_requires_deleted_and_explicit_confirmation(self):
  self.assertEqual(self.post('purge').json()['error'],'JOB_PURGE_CONFIRMATION_REQUIRED')
  self.assertEqual(self.post('purge',confirmed=True).json()['error'],'JOB_NOT_DELETED');self.assertTrue(self.store.get(self.id))
 def test_11_purge_removes_only_owned_rows_and_workspace(self):
  other=self.make();self.post('delete');r=self.post('purge',confirmed=True);self.assertEqual(r.status_code,200,r.text)
  for t,k in [('jobs','id'),('job_items','job_id'),('checkpoints','job_id')]:
   with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM '+t+' WHERE '+k+'=?',(self.id,)).fetchone()[0],0)
  self.assertFalse((self.store.home/'jobs'/self.id).exists());self.assertFalse((self.store.home/'reports'/(self.id+'.md')).exists());self.assertEqual(self.store.get(other['id']),other)
 def test_12_source_and_export_protection(self):
  before=[f.read_bytes() for f in [self.source,self.output]];self.post('delete');self.post('purge',confirmed=True);self.assertEqual([f.read_bytes() for f in [self.source,self.output]],before)
 def test_13_shared_cache_presets_assets_protected(self):
  files=[self.store.home/n/'fixture' for n in ['cache/asr','cache/tts','cache/deepseek','user_assets','products']]
  for f in files:f.write_text('shared')
  before=self.store.products();settings=self.store.settings();self.post('delete');self.post('purge',confirmed=True)
  self.assertTrue(all(f.read_text()=='shared' for f in files));self.assertEqual(before,self.store.products());self.assertEqual(settings,self.store.settings())
 def test_14_workspace_symlink_refused(self):
  w=self.store.home/'jobs'/self.id;shutil.rmtree(w);w.symlink_to(self.root,target_is_directory=True);self.post('delete');self.assertEqual(self.post('purge',confirmed=True).json()['error'],'JOB_PATH_UNSAFE');self.assertTrue(self.source.is_file())
 def test_15_nested_symlink_never_followed(self):
  (self.store.home/'jobs'/self.id/'work/link').symlink_to(self.source);self.post('delete');self.assertEqual(self.post('purge',confirmed=True).status_code,200);self.assertTrue(self.source.is_file())
 def test_16_other_job_reference_protected(self):
  other=self.make();self.store.item(other['id'],1,plan_path=str(self.store.home/'jobs'/self.id/'work/plan.json'));self.post('delete');self.assertEqual(self.post('purge',confirmed=True).json()['error'],'JOB_DATA_IN_USE')
 def test_17_export_inside_workspace_refused(self):
  self.store.item(self.id,1,output=str(self.store.home/'jobs'/self.id/'work/plan.json'));self.post('delete');self.assertEqual(self.post('purge',confirmed=True).json()['error'],'JOB_DATA_IN_USE')
 def test_18_active_edit_lease_blocks_race(self):
  with self.service.edit(self.id):self.assertEqual(self.post('delete').json()['error'],'JOB_RUNNING')
  self.assertEqual(self.post('delete').status_code,200)
 def test_19_purge_rename_failure_restores_data(self):
  self.post('delete');original=os.replace
  def fail_report(a,b):
   if str(a).endswith('.md'):raise OSError('fixture failure')
   return original(a,b)
  with patch('autocut.job_deletion.os.replace',side_effect=fail_report):self.assertEqual(self.post('purge',confirmed=True).json()['error'],'JOB_DELETE_FAILED')
  self.assertTrue((self.store.home/'jobs'/self.id/'work/plan.json').exists());self.assertTrue(self.store.get(self.id)['deleted_at'])
 def test_20_crash_recovery_before_and_after_commit(self):
  self.post('delete');work,report,pending=self.service.locations(self.id);pending.mkdir();os.replace(work,pending/'workspace');os.replace(report,pending/'report.md')
  JobDeletion(self.store,self.manager);self.assertTrue(work.exists());self.assertTrue(report.exists());self.assertFalse(pending.exists())
  pending.mkdir();(pending/'workspace').mkdir();(pending/'workspace/fixture').write_text('orphan');
  with self.store.connect() as db:db.execute('DELETE FROM jobs WHERE id=?',(self.id,))
  JobDeletion(self.store,self.manager);self.assertFalse(pending.exists());self.assertTrue(self.source.exists())
 def test_21_idempotent_soft_delete_and_restore(self):
  first=self.post('delete').json()['deleted_at'];self.assertEqual(self.post('delete').json()['deleted_at'],first);self.post('restore');self.assertEqual(self.post('restore').status_code,200)
 def test_22_deleted_not_in_counts_completions_or_script_library(self):
  self.store.update(self.id,completion_notified=False);self.post('delete');self.assertEqual(self.client.get('/runtime/active').json()['count'],0);self.assertEqual(self.client.post('/runtime/completions',json={}).json()['events'],[]);self.assertEqual(self.client.get('/scripts?product=太阳镜&market=Brazil').json(),[])
 def test_23_deleted_waiting_approval_preserves_state(self):
  self.store.update(self.id,state='WAITING_SCRIPT_APPROVAL');self.post('delete');self.post('restore');self.assertEqual(self.store.get(self.id)['state'],'WAITING_SCRIPT_APPROVAL')
 def test_24_missing_task_does_not_delete_files(self):
  self.assertEqual(self.client.post('/jobs/'+'0'*32+'/purge',json={'confirmed':True}).status_code,404);self.assertTrue(self.source.is_file())
if __name__=='__main__':unittest.main()
