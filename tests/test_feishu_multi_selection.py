"""Multi-selection routing using an isolated store; no workers or providers run."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from autocut.feishu_copy import normalize
from autocut.server import create_app
from autocut.storage import Store
from autocut.workbench import script_snapshot, selected_contexts, selected_script, assign_scripts, SCRIPT_ASSIGNMENT_POLICY

class MultiSelectionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.store=Store(Path(self.temp.name)/'data');self.manager=MagicMock()
  self.app=create_app(self.store,MagicMock(),MagicMock(),self.manager,'fixture',recover_on_startup=False)
  self.client=TestClient(self.app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer fixture'})
  self.addCleanup(self.client.close)
  self.source=self.app.state.feishu
  self.source.bind('test','https://my.feishu.cn/wiki/TestNode123?sheet=mx123')
  self.sid=self.source.binding()['source_id']
  values=[['类','中文文案','国家','类型','文案','文案编号','出单数']]
  for i,orders in enumerate(['≥10','≥500',0,''],1):
   values.append(['文案',f'第{i}条中文文案','墨西哥西班牙语','软广',f'Mira esta bolsa. Puedes llevar tu teléfono con comodidad. Opción {i}.',f'W{i}',orders])
  normalized=normalize(self.sid,{'sheet_id':'mx123','title':'MX_手机包'},values,time.time())
  self.source.write(self.sid+'.json',dict(source_id=self.sid,last_synced_at=time.time(),sheets=[normalized]))
  self.rows=normalized['rows'];self.ids=[r['id'] for r in self.rows]
  self.client.post('/native/grant',json={'path':self.temp.name})
 def spec(self,**overrides):
  return dict(dict(request_id='multi-selection-fixture',input_path=self.temp.name,product_name='手机包',market='Mexico',feishu_source_id=self.sid,feishu_sheet_id='mx123',script_mode='TABLE_SELECTED',script_reference_ids=self.ids[:2]),**overrides)
 def test_snapshot_persistence_selected_only_and_idempotent(self):
  for mode in ('Transport','Remix'):
   with self.subTest(mode=mode):
    body=self.spec(request_id='multi-selection-'+mode,mode=mode,script_reference_ids=[self.ids[1],self.ids[0],self.ids[1]])
    response=self.client.post('/jobs',json=body);self.assertEqual(response.status_code,200,response.text)
    repeated=self.client.post('/jobs',json=body);self.assertEqual(repeated.json()['id'],response.json()['id'])
    job=Store(self.store.home).by_request(body['request_id'])
    self.assertEqual(job['script_reference_ids'],[self.ids[1],self.ids[0]])
    self.assertEqual([r['id'] for r in job['script_snapshots']],job['script_reference_ids'])
    self.assertEqual([r['id'] for r in job['reference']['script_rows']],job['script_reference_ids'])
    self.assertTrue(job['batch_review_required']);self.assertTrue(job['content_confirmation_required']);self.assertFalse(job.get('scripts_approved'))
    self.assertEqual(job['script_assignment_policy'],SCRIPT_ASSIGNMENT_POLICY)
    self.assertIsNone(selected_script(job,'02')) # Must wait for real probed durations.
  self.assertEqual(self.manager.enqueue.call_count,2)
  self.assertEqual(len(self.source.cache()['sheets'][0]['rows']),4)
 def test_invalid_or_empty_selection_is_atomic(self):
  for ids in ([self.ids[0],'missing'],[]):
   response=self.client.post('/jobs',json=self.spec(script_reference_ids=ids))
   self.assertIn(response.status_code,(400,422),response.text)
   self.assertIsNone(self.store.by_request('multi-selection-fixture'))
  self.manager.enqueue.assert_not_called()
 def test_order_counts_preserve_threshold_zero_and_missing(self):
  response=self.client.get('/copy-sources/feishu/reference',params={'source_id':self.sid,'sheet_id':'mx123','market':'Mexico'})
  self.assertEqual(response.status_code,200,response.text)
  self.assertEqual([r['performance'] for r in response.json()['scripts']['Mexico']],['≥10','≥500','0',''])
 def test_ten_outputs_split_shared_group_without_changing_language(self):
  job=script_snapshot(dict(script_mode='TABLE_SELECTED',market='Mexico',reference=self.source.reference(self.sid,'mx123','Mexico'),script_reference_ids=self.ids[:2]),self.store)
  job.pop('script_assignment_policy') # Legacy jobs retain their original distribution.
  original=dict(script_group_id='same-visual-group',video_context='真实商品画面',clean_script='old',source_language='en',language='es-MX')
  before=copy.deepcopy(original)
  contexts,mapping,groups=selected_contexts([original],{str(r):'same-visual-group' for r in range(1,11)},job)
  self.assertEqual(len(contexts),2)
  for rank in range(1,11):
   context=groups[mapping[str(rank)]];expected=self.rows[(rank-1)%2]
   self.assertEqual(context['source_script'],expected['text']);self.assertEqual(context['clean_script'],expected['text'])
   self.assertEqual(context['source_language'],expected['language']);self.assertEqual(context['language'],'es-MX')
   self.assertEqual(selected_script(job,rank)['id'],expected['id'])
  self.assertEqual(original,before)
  selected_script(job,1)['text']='changed';self.assertEqual(job['script_snapshots'][0]['text'],self.rows[0]['text'])
 def test_legacy_single_selection_and_unselected_contexts(self):
  ref=self.source.reference(self.sid,'mx123','Mexico',self.ids[2]);self.assertEqual(ref['count'],1)
  old=script_snapshot(dict(script_mode='TABLE_SELECTED',reference=ref,market='Mexico',script_reference_id=self.ids[2]),self.store)
  old.pop('script_snapshots');self.assertEqual(selected_script(old,10)['id'],self.ids[2])
  c=dict(script_group_id='g',video_context='scene')
  contexts,mapping,_=selected_contexts([c],{'01':'g','02':'g'},{});self.assertEqual(contexts,[c]);self.assertEqual(mapping,{'01':'g','02':'g'})
 def test_snapshot_rejects_partial_match(self):
  with self.assertRaisesRegex(ValueError,'SCRIPT_SELECTION_INVALID'):
   script_snapshot(dict(script_mode='TABLE_SELECTED',reference=self.source.reference(self.sid,'mx123','Mexico'),market='Mexico',script_reference_ids=[self.ids[0],'missing']),self.store)

 def duration_job(self):
  return dict(script_assignment_policy=SCRIPT_ASSIGNMENT_POLICY,script_snapshots=[
   dict(id='long',reference_id='L',text='l'*900,language='pt-BR',text_zh='中'),
   dict(id='short',reference_id='S',text='s'*100,language='pt-BR',text_zh='中'*1000),
   dict(id='medium',reference_id='M',text='m'*400,language='pt-BR')])

 def test_duration_order_not_rank_or_click_order(self):
  job=self.duration_job();original=copy.deepcopy(job)
  sources=[dict(rank=1,duration=20),dict(rank=2,duration=90),dict(rank=3,duration=40)]
  job.update(assign_scripts(job,sources))
  self.assertEqual([selected_script(job,n)['id'] for n in [1,2,3]],['short','long','medium'])
  self.assertEqual(job['script_snapshots'],original['script_snapshots'])
  self.assertEqual(assign_scripts(job,list(reversed(sources))),{})
  self.assertEqual(assign_scripts(original,sources),assign_scripts(original,list(reversed(sources))))

 def test_unequal_counts_reuse_length_bands_and_ignore_invalid_sources(self):
  for count in [2,3,10]:
   job=self.duration_job();sources=[dict(rank=i,duration=i*10) for i in range(1,count+1)]+[dict(rank=99,status='INVALID')]
   job.update(assign_scripts(job,sources));rows=[selected_script(job,i) for i in range(1,count+1)]
   sizes=[len(r['text']) for r in rows]
   self.assertEqual(sizes,sorted(sizes));self.assertEqual(sizes[0],100);self.assertEqual(sizes[-1],900)
   self.assertEqual(len(job['script_assignments']),count)
   self.assertTrue(all(r['id'] in {'short','medium','long'} for r in rows))

 def test_duration_assignment_persists_and_context_uses_each_video_duration(self):
  job=self.duration_job();job.update(assign_scripts(job,[dict(rank=i,duration=i*10) for i in range(1,7)]))
  saved,_=self.store.create(dict(request_id='duration-fixture',product_name='测试',**job))
  saved=self.store.get(saved['id'])
  original=dict(script_group_id='g',video_context='scene',target_duration_seconds=999)
  _,mapping,groups=selected_contexts([original],{i:'g' for i in range(1,7)},saved)
  for i in range(1,7):
   c=groups[mapping[i]];self.assertEqual(c['target_duration_seconds'],i*10)
   self.assertEqual(c['source_script'],selected_script(saved,i)['text'])
  self.assertNotEqual(mapping[1],mapping[2]) # Same script, different duration contexts.
  self.assertEqual(assign_scripts(saved,[dict(rank=1,duration=1000)]),{})

 def test_legacy_and_confirmed_jobs_are_not_reassigned(self):
  legacy=self.duration_job();legacy.pop('script_assignment_policy');legacy['scripts_approved']=True
  before=copy.deepcopy(legacy)
  self.assertEqual(assign_scripts(legacy,[dict(rank=1,duration=1)]),{})
  self.assertEqual(selected_script(legacy,1)['id'],'long');self.assertEqual(legacy,before)

 def test_invalid_durations_are_not_silently_matched(self):
  for duration in [None,0,-1,float('nan'),float('inf')]:
   with self.assertRaisesRegex(ValueError,'SCRIPT_ASSIGNMENT_DURATION_INVALID'):
    assign_scripts(self.duration_job(),[dict(rank=1,duration=duration)])

if __name__=='__main__':unittest.main()
