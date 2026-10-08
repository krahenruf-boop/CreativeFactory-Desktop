"""Batch barriers against real SQLite/API, without paid services or rendering."""
import copy,json,threading,time,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from test_content_confirmation import ContentTests,TitleAdapter
from test_ownership import OwnershipTests
from autocut.ownership import checkpoints,item_state
from autocut.storage import Store
from autocut.pipeline import Manager
from autocut.item_pipeline import resume_item

class BatchTests(unittest.TestCase):
 tearDown=OwnershipTests.tearDown
 raw=OwnershipTests.raw
 state=OwnershipTests.state
 post=OwnershipTests.post
 def setUp(self):
  OwnershipTests.setUp(self);self.store.update(self.id,source_count=3,batch_review_required=True,content_confirmation_required=True)
  base=self.raw()
  for n in [2,3]:
   row=copy.deepcopy(base);row.pop('rank');row['final_script']+=' Olha só!'+(' Veja agora.' if n==3 else '');row['copy']['localized_script']['text']=row['final_script'];self.store.item(self.id,n,**row)
  self.queue.batches=[]
  def batch(id,rows):self.queue.batches.append(rows);self.queue.enqueued.extend((id,rank,phase) for rank,phase in rows)
  self.queue.enqueue_batch=batch;self.adapter=TitleAdapter();self.app.state.translations.factory=lambda:self.adapter
 def public(self):return self.c.get('/jobs/'+self.id).json()
 def view(self):return self.public()['batch_review_view']
 def payload(self,phase):
  b=self.view();d=dict(action_token=b['action_token'],request_id='fixture-'+phase+'-'+b['action_token'][:12])
  if phase=='titles':d['selections']={x['rank']:int(x['selected_title'] or 1) for x in b['items'] if x['in_scope']}
  return d
 def submit(self,phase,data=None):return self.c.post('/jobs/'+self.id+'/batch/approve-'+phase,json=data or self.payload(phase))
 def scripts(self):
  r=self.submit('scripts');self.assertEqual(r.status_code,200,r.text);return r
 def rows(self):return self.store.get(self.id)['items']
 def test_01_all_independent_scripts_visible_default_ready(self):
  b=self.view();self.assertEqual(len(b['items']),3);self.assertTrue(b['all_scripts_ready']);self.assertEqual(b['phase'],'SCRIPTS');self.assertEqual(len({r['script']['text'] for r in b['items']}),3);self.assertTrue(all(r['script_zh'] for r in b['items']))
 def test_02_one_atomic_script_barrier_no_tts_no_title_approval(self):
  self.scripts();self.assertEqual(self.view()['phase'],'TITLES');self.assertEqual(self.queue.enqueued,[])
  for x in self.rows():self.assertEqual(x['script_status'],'USER_APPROVED');self.assertEqual(x['status'],'WAITING_TITLE_APPROVAL');self.assertIn('SCRIPT_APPROVED',checkpoints(self.store,self.id,x['rank']));self.assertNotIn('TITLE_APPROVED',checkpoints(self.store,self.id,x['rank']))
 def test_03_unready_script_no_partial_write(self):
  self.store.item(self.id,3,final_script=None,localized_script=None,copy={},script_candidates=[]);before=self.rows();r=self.submit('scripts');self.assertEqual(r.status_code,400,r.text);self.assertEqual(self.rows(),before);self.assertFalse(any('SCRIPT_APPROVED' in checkpoints(self.store,self.id,x['rank']) for x in self.rows()))
 def test_04_language_guard_whole_batch(self):
  self.store.item(self.id,3,final_script='This is your new product and you can use it.',final_script_language='en');before=self.rows();self.assertEqual(self.submit('scripts').status_code,400);self.assertEqual(self.rows(),before)
 def test_05_missing_item_blocks_barrier(self):
  self.store.update(self.id,source_count=4);self.assertFalse(self.view()['all_scripts_ready']);self.assertEqual(self.submit('scripts').status_code,400)
 def test_06_music_exempts_script_but_requires_local_music(self):
  self.store.item(self.id,2,voiceover_mode='MUSIC_ONLY',music={'path':'fixture.wav'},final_script=None);self.scripts();self.assertIsNone(self.rows()[1]['final_script']);self.assertEqual(self.queue.enqueued,[])
 def test_07_replacement_updates_only_one_row(self):
  before=self.rows();r=self.post('script/draft',dict(mode='PASTED',text='Olha esta fivela! Fica linda.',language='pt-BR'));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.rows()[1:],before[1:]);self.assertEqual(self.view()['items'][0]['script']['text'],'Olha esta fivela! Fica linda.');self.scripts();self.assertEqual(self.raw()['final_script'],'Olha esta fivela! Fica linda.')
 def test_08_three_titles_each_A_default(self):
  self.scripts();b=self.view();self.assertEqual(len(b['items']),3);self.assertTrue(all(len(x['title_candidates'])==3 and x['selected_title']==1 for x in b['items']))
 def test_09_single_refresh_no_other_item_change(self):
  self.scripts();before=self.rows();r=self.post('content/titles');self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.rows()[1:],before[1:]);self.assertEqual(self.raw()['final_script'],before[0]['final_script']);self.assertEqual(len(self.adapter.calls),1);self.assertEqual(len(self.raw()['content_titles']),3)
 def test_10_refresh_failure_preserves_titles(self):
  self.scripts();before=self.view()['items'][0]['title_candidates'];self.app.state.content.title_request=MagicMock(side_effect=ValueError('fixture'));r=self.post('content/titles');self.assertEqual(r.status_code,400);self.assertEqual(self.view()['items'][0]['title_candidates'],before)
 def test_11_custom_localization(self):
  self.scripts();r=self.post('content/custom-title',dict(text='看看这个带扣'));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.view()['items'][0]['selected_title'],4);self.assertEqual(self.raw()['content_custom_title']['language'],'pt-BR');self.assertTrue(self.raw()['content_custom_title']['text_zh']);self.assertEqual(self.submit('titles').status_code,200)
 def test_12_all_titles_atomic_independent_selections(self):
  self.scripts();d=self.payload('titles');d['selections']={'01':1,'02':2,'03':3};r=self.submit('titles',d);self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.view()['phase'],'RUNNING');self.assertEqual(len(self.queue.batches),1);self.assertEqual(len(self.queue.batches[0]),3)
  for i,x in enumerate(self.rows(),1):self.assertEqual(x['selected_title_id'],i);self.assertEqual(x['status'],'QUEUED');self.assertTrue(x['content_confirmed']);self.assertIn('TITLE_APPROVED',checkpoints(self.store,self.id,x['rank']));self.assertNotIn('FINAL_APPROVED',checkpoints(self.store,self.id,x['rank']))
 def test_13_missing_title_choice_no_half_commit(self):
  self.scripts();before=self.rows();d=self.payload('titles');d['selections'].pop('03');r=self.submit('titles',d);self.assertEqual(r.status_code,400);self.assertEqual(self.rows(),before);self.assertEqual(self.queue.batches,[])
 def test_14_invalid_last_title_no_half_commit(self):
  self.scripts();before=self.rows();d=self.payload('titles');d['selections']['03']=99;self.assertEqual(self.submit('titles',d).status_code,400);self.assertEqual(self.rows(),before);self.assertFalse(any('TITLE_APPROVED' in checkpoints(self.store,self.id,x['rank']) for x in self.rows()))
 def test_15_title_language_guard(self):
  self.scripts();self.store.item(self.id,3,content_titles=[dict(id=i,text='This is your new product',language='en',text_zh='这是你的新产品') for i in [1,2,3]]);self.assertEqual(self.submit('titles').status_code,400);self.assertEqual(self.queue.enqueued,[])
 def test_16_stale_script_action(self):
  d=self.payload('scripts');self.store.item(self.id,2,script_status='WAITING_APPROVAL');self.assertEqual(self.submit('scripts',d).json()['error'],'STALE_ACTION')
 def test_17_stale_title_action_after_refresh(self):
  self.scripts();d=self.payload('titles');self.post('content/titles');self.assertEqual(self.submit('titles',d).json()['error'],'STALE_ACTION');self.assertEqual(self.queue.enqueued,[])
 def test_18_script_repeated_request_idempotent(self):
  d=self.payload('scripts');self.assertEqual(self.submit('scripts',d).status_code,200);before=self.rows();r=self.submit('scripts',d);self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['batch_result']['replayed']);self.assertEqual(self.rows(),before);self.assertEqual(self.queue.enqueued,[]);self.assertEqual(self.adapter.calls,[])
 def test_19_title_repeated_request_no_second_dispatch(self):
  self.scripts();d=self.payload('titles');self.assertEqual(self.submit('titles',d).status_code,200);before=self.rows();self.assertEqual(self.submit('titles',d).status_code,200);self.assertEqual(self.rows(),before);self.assertEqual(len(self.queue.batches),1);self.assertEqual(self.adapter.calls,[])
 def test_20_receipt_reused_with_different_selection_rejected(self):
  self.scripts();d=self.payload('titles');self.submit('titles',d);d['selections']['02']=2;self.assertEqual(self.submit('titles',d).json()['error'],'STALE_ACTION');self.assertEqual(len(self.queue.batches),1)
 def test_21_single_item_approval_cannot_bypass_batch(self):
  self.assertEqual(self.post('content/approve',dict(title_id=1)).status_code,409);self.assertEqual(self.post('approve-title',dict(candidate=1)).status_code,409);self.assertEqual(self.queue.enqueued,[])
 def test_22_titles_before_scripts_rejected(self):
  self.assertEqual(self.submit('titles').status_code,400);self.assertEqual(self.post('content/titles').status_code,409);self.assertEqual(self.adapter.calls,[])
 def test_23_reopen_script_invalidates_only_current_title(self):
  self.scripts();before=self.rows();b=self.view();r=self.c.post('/jobs/'+self.id+'/batch/reopen-script',json=dict(rank='01',action_token=b['action_token']));self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.rows()[1:],before[1:]);self.assertEqual(self.view()['phase'],'SCRIPTS');self.post('script/draft',dict(mode='PASTED',text='Olha esta fivela! Fica linda.',language='pt-BR'));self.scripts();self.assertEqual(self.rows()[1:],before[1:])
 def test_24_legacy_waiting_job_aggregates_readonly(self):
  self.store.update(self.id,batch_review_required=False);before=self.store.get(self.id);self.assertEqual(self.view()['phase'],'SCRIPTS');self.assertEqual(self.store.get(self.id),before);self.scripts();self.assertEqual(self.view()['phase'],'TITLES')
 def test_25_legacy_mixed_running_unchanged(self):
  self.store.update(self.id,batch_review_required=False,state='RUNNING');self.store.item(self.id,3,status='VOICE',content_edit=False);before=self.rows()[2];self.assertTrue(self.view()['legacy_partial']);self.scripts();self.submit('titles');self.assertEqual(self.rows()[2],before);self.assertEqual(len(self.queue.batches[0]),2)
 def test_26_restart_preserves_phase_and_receipt_schema3(self):
  d=self.payload('scripts');self.scripts();s=Store(self.store.home);self.assertEqual(s.get(self.id)['batch_review']['phase'],'TITLES');self.assertEqual(self.submit('scripts',d).status_code,200)
  with s.connect() as db:self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)
 def test_27_deleted_job_rejects_batch(self):
  d=self.payload('scripts');self.c.post('/jobs/'+self.id+'/delete');self.assertEqual(self.view()['allowed_actions'],[]);self.assertEqual(self.submit('scripts',d).status_code,400)
 def test_28_batch_workers_independent_exception_does_not_block(self):
  self.scripts();self.submit('titles');m=Manager(self.store,None,MagicMock());entered=threading.Barrier(2);seen=[]
  def work(manager,id,rank,phase):
   if rank in ['01','02']:entered.wait(timeout=4)
   seen.append(rank);manager.store.item(id,rank,status='WAITING_SUPPLEMENT_MEDIA' if rank=='01' else 'CHECKING',issues=['VISUAL_DURATION_INSUFFICIENT'] if rank=='01' else [])
  with patch('autocut.item_pipeline.resume_item',side_effect=work):m.enqueue_batch(self.id,[('01','TITLE'),('02','TITLE'),('03','TITLE')]);m.active[self.id].result(timeout=6)
  m.queue.shutdown();self.assertEqual(set(seen),{'01','02','03'});self.assertEqual(self.rows()[0]['status'],'WAITING_SUPPLEMENT_MEDIA');self.assertEqual(self.view()['phase'],'RUNNING');self.assertEqual(m.item_pending,set())
 def test_29_batch_gate_stops_old_resume_before_tts(self):
  self.scripts();m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event()
  with patch('autocut.item_pipeline.tts_job') as tts:resume_item(m,self.id,1,'TITLE');tts.assert_not_called()
  m.queue.shutdown();self.assertNotIn(self.raw()['status'],['PASS','USER_APPROVED'])
 def test_30_sql_failure_rolls_back_entire_batch(self):
  before=self.rows()
  with self.store.connect() as db:db.execute("CREATE TRIGGER reject_third BEFORE UPDATE ON job_items WHEN NEW.rank='03' BEGIN SELECT RAISE(ABORT,'fixture'); END")
  with self.assertRaises(Exception):self.app.state.batch.scripts(self.id,self.payload('scripts'))
  self.assertEqual(self.rows(),before);self.assertEqual(self.view()['phase'],'SCRIPTS');self.assertNotIn('SCRIPT_APPROVED',checkpoints(self.store,self.id,1))
 def test_31_frontend_workspace_navigation_contract(self):
  app=Path('web/app.js').read_text();ws=Path('web/workspace.js').read_text();self.assertNotIn('taskDialog',app);self.assertNotIn('function taskContent',app);self.assertIn('id="task-workspace"',app)
  nav=ws[ws.index('function backToTasks'):ws.index('function refreshWorkspace')];self.assertNotIn('/stop',nav);self.assertNotIn('dialog(',nav)
  for phrase in ['← 返回任务','确认全部口播并继续','确认全部标题并开始剪辑','batchTitleRequest','workspacePage=page||j.batch_review_view.phase']:self.assertIn(phrase,ws)
 def test_32_reading_job_has_no_side_effects(self):
  before=self.store.get(self.id)
  for _ in range(3):self.c.get('/jobs/'+self.id+'/content-review');self.c.get('/jobs');self.public()
  self.assertEqual(self.store.get(self.id),before);self.assertEqual(self.queue.enqueued,[])

 def test_33_waiting_batch_not_reported_as_exception(self):
  self.assertEqual(self.public()['content_counts']['pending'],3);self.assertEqual(self.public()['content_counts']['exception'],0)
  self.scripts();self.assertEqual(self.public()['content_counts']['pending'],3);self.assertEqual(self.public()['content_counts']['exception'],0)
 def test_34_finished_peer_does_not_interrupt_live_workers(self):
  self.scripts();self.submit('titles');m=Manager(self.store,None,MagicMock());m.stops[self.id]=threading.Event();m.item_pending={(self.id,'01'),(self.id,'02')};self.store.item(self.id,2,status='VOICE')
  with patch('autocut.item_pipeline.ItemReview.evidence',side_effect=ValueError('VISUAL_DURATION_INSUFFICIENT')):resume_item(m,self.id,1,'TITLE')
  self.assertEqual(self.store.get(self.id)['state'],'RUNNING');self.assertEqual(item_state(self.store,self.store.get(self.id),self.rows()[1])['current_owner'],'SYSTEM');m.queue.shutdown()

 def test_35_failed_preparation_is_actionable_not_infinite_analysis(self):
  self.store.update(self.id,state='REVIEW_REQUIRED');self.store.item(self.id,3,status='WAITING',issues=['NO_RELIABLE_SCRIPT']);self.assertEqual(self.view()['label'],'需要处理');self.assertFalse(self.view()['all_scripts_ready']);self.assertEqual(self.public()['items'][2]['current_owner'],'USER')
