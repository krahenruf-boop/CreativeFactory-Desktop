import copy,json,tempfile,unittest,threading
from pathlib import Path
from unittest.mock import patch,MagicMock
from concurrent.futures import Future
from fastapi.testclient import TestClient
from autocut.server import create_app
from autocut.storage import Store
from autocut.workbench import *
from autocut.task_instructions import effective,effective_sources,InstructionService
from autocut.pipeline import Manager
from app.s3.headless_runtime import fingerprint,write

class Queue:
 def __init__(self):self.started=[]
 def enqueue(self,id):self.started.append(id)
 def stop(self,id):pass
class WorkbenchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);self.store=Store(self.home/'data');self.queue=Queue()
  self.c=TestClient(create_app(self.store,None,None,self.queue,'test'),base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer test'})
  self.c.post('/native/grant',json={'path':str(self.home)})
  self.spec=dict(request_id='workbench-test-000',input_path=str(self.home),product_profile_id='general',product_name='太阳镜',market='Brazil')
 def tearDown(self):self.tmp.cleanup()
 def create(self,**extra):
  spec=dict(self.spec,**extra);r=self.c.post('/jobs',json=spec);self.assertEqual(r.status_code,200,r.text);return r.json()
 def test_script_modes_xlsx_snapshot_and_existing(self):
  from openpyxl import Workbook
  p=self.home/'文案.xlsx';w=Workbook();w.active.append(['编号','pt-BR','历史出单','备注']);w.active.append(['W4','Olha estes óculos práticos','2500','测试']);w.save(p)
  self.c.post('/native/grant',json={'path':str(p)});table=self.c.post('/references/inspect',json={'path':str(p)}).json();row=table['scripts']['Brazil'][0]
  a=self.create();self.assertEqual(a['script_mode'],'AUTO')
  b=self.create(request_id='workbench-test-001',script_reference_file=str(p));self.assertEqual(b['script_mode'],'AUTO')
  c=self.create(request_id='workbench-test-002',script_mode='TABLE_SELECTED',script_reference_file=str(p),script_reference_id=row['id']);p.unlink()
  self.assertEqual(c['script_snapshot']['text'],row['text']);self.assertTrue(c['reference_file_id'])
  self.store.item(a['id'],1,copy={'localized_script':{'text':'Olha só que prático'}})
  rows=self.c.get('/scripts',params={'product':'太阳镜','market':'Brazil'}).json();self.assertEqual(len(rows),1)
  d=self.create(request_id='workbench-test-003',script_mode='EXISTING',existing_script_id=rows[0]['id']);self.store.item(a['id'],1,copy={})
  self.assertEqual(d['script_snapshot']['text'],'Olha só que prático')
  self.assertEqual(self.c.get('/scripts',params={'product':'无关产品'}).json(),[])
  self.assertEqual(self.c.post('/jobs',json=dict(self.spec,request_id='invalid-script-mode',script_mode='TABLE_SELECTED',script_reference_id='W0')).status_code,400)
 def test_tags_preset_isolation_reopen_and_guards(self):
  p=self.c.post('/products',json={'product_name':'BR · Beauty','market':'Brazil','language':'pt-BR','task_defaults':{'cta_enabled':True,'subtitle_position_adjustment':'up','video_speed_preference':2.0,'allow_propainter':True}}).json()
  self.assertEqual(p['task_defaults']['video_speed_preference'],1.25);self.assertFalse(p['task_defaults']['allow_propainter'])
  self.assertEqual(self.c.post('/products',json={'product_name':'BAD','shell':'rm'}).status_code,400)
  t=self.c.get('/custom-tags').json()['tags'];t.append(dict(id='short',label='口播短一些',intent='concise'));self.c.post('/custom-tags',json={'tags':t})
  a=self.create(product_profile_id=p['id'],custom_tag_ids=['no_cta','short']);self.assertFalse(effective(a,1)['cta_enabled']);self.assertEqual(effective(a,1)['subtitle_position_adjustment'],'up')
  b=self.create(request_id='next-no-custom',product_profile_id=p['id']);self.assertTrue(effective(b,1)['cta_enabled']);self.assertNotIn('copy_style',effective(b,1))
  self.assertEqual(a['instruction_parse_count'],0)
  self.assertEqual(effective_sources(a,1)['cta_enabled'],'TASK_OVERRIDE');self.assertEqual(effective_sources(a,1)['subtitle_position_adjustment'],'PRESET')
  self.assertEqual(self.c.post('/products/'+p['id']+'/delete',json={}).status_code,400)
  self.c.post('/products/'+p['id']+'/delete',json={'confirmed':True});self.assertEqual(self.store.get(a['id'])['product_profile']['id'],p['id'])
  reopened=Store(self.store.home);self.assertEqual(tags(reopened)[-1]['label'],'口播短一些');self.assertFalse(effective(reopened.get(a['id']),1)['cta_enabled'])
  self.assertEqual(self.c.post('/custom-tags',json={'tags':[dict(id='bad',label='bad',intent='shell')]}).status_code,400)
 def test_legacy_and_title_approval(self):
  j=self.create(title_approval_mode='manual');self.store.item(j['id'],1,title_candidates=[{'id':1,'text':'Olha só'}],selected_title_id=1)
  self.store.update(j['id'],state='WAITING_TITLE_APPROVAL')
  self.assertEqual(self.c.post('/jobs/'+j['id']+'/approve-titles',json={'selections':{'01':7}}).status_code,400)
  self.assertEqual(self.c.post('/jobs/'+j['id']+'/approve-titles',json={'selections':{'01':1},'allow_original':True}).status_code,200)
  with self.store.connect() as db:
   data=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(j['id'],)).fetchone()[0]);data.pop('script_mode');db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(data),j['id']))
  self.assertEqual(self.c.get('/jobs/'+j['id']).json()['script_mode'],'AUTO')
  self.c.post('/products/eye/delete',json={'confirmed':True});self.assertIsNone(Store(self.store.home).product('eye'))
 def test_real_manager_tts_gate_and_selected_resume(self):
  j=self.create(script_mode='REQUIRE_APPROVAL',custom_tag_ids=['no_cta']);id=j['id'];m=Manager(self.store,None,None);m.stops[id]=threading.Event()
  source=self.home/'short.mp4';source.write_bytes(b'explicit mock media; real source smoke is separate')
  s=dict(rank=1,path=str(source),filename='short.mp4',sha256='test-hash',duration=8.,width=1080,height=1920,status='VALID')
  a=dict(voice_quality='CLEAR',clean_script='Original',source_language='en',selling_points=['compact'],allowed_claims=['compact'],promotion_context='',context='Product demonstration',group_key='test',speaker='female',overlays_need_review=False)
  dec=dict(copy_approved=True,issues=[],selected_title_id=1,title_y=.17,subtitle_y=.78,cta_enabled=True,cta_side='left',override_reason='NONE')
  def judge(*args):return copy.deepcopy(a if args[5]=='analyze' else dec)
  def copies(contexts,*args):return {'items':[dict(script_group_id=c['script_group_id'],response=dict(localized_script={'text':f'Roteiro {i} destes óculos'},titles=[dict(id=1,text='Olha só')])) for i,c in enumerate(contexts)]}
  future=Future();future.set_result({'text':'Original'})
  harness=MagicMock();harness.__enter__.return_value=harness
  catalog=MagicMock(side_effect=ValueError('SMOKE_STOP_BEFORE_PAID_TTS'))
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=harness),patch('autocut.pipeline.scan',return_value=[s]),patch('autocut.pipeline.asr_job',return_value=future),patch('autocut.pipeline.source_sheet',return_value='sheet.jpg'),patch.object(m,'judge',side_effect=judge),patch.object(m,'copy_batch',side_effect=copies),patch.object(m,'catalog',catalog),patch('autocut.pipeline.tts_job') as tts:
   m.run(id);first=self.store.get(id);self.assertEqual(first['state'],'WAITING_SCRIPT_APPROVAL',first);self.assertEqual(first['items'][0]['presentation_warnings'],['ZH_TRANSLATION_MISSING']);self.assertEqual(len(first['items'][0]['script_candidates']),3);catalog.assert_not_called();tts.assert_not_called()
   candidates=first['items'][0]['script_candidates'];r=self.c.post('/jobs/'+id+'/approve-scripts',json={'selections':{'01':candidates[1]['id']},'allow_original':True});self.assertEqual(r.status_code,200,r.text)
   m.run(id);catalog.assert_called_once();tts.assert_not_called();result=self.store.get(id);self.assertEqual(result['items'][0]['copy']['localized_script']['text'],candidates[1]['text']);self.assertEqual(result['issues'],['SMOKE_STOP_BEFORE_PAID_TTS'])
  m.queue.shutdown(wait=False)
if __name__=='__main__':unittest.main()
