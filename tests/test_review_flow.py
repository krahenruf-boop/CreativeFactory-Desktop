"""0.1.9 isolated fixtures. No network, paid providers, ASR or ProPainter."""
import copy,json,os,tempfile,threading,unittest
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import patch,MagicMock
from fastapi.testclient import TestClient
from autocut.storage import Store
from autocut.server import create_app
from autocut.item_review import ItemReview,review_route,get_item
from autocut.review_media import *
from autocut.copy_presentation import TranslationService,production_copy
from autocut.pipeline import Manager,make_plan
from autocut.item_pipeline import resume_item
from app.s3.headless_runtime import write,sha
from test_copy_presentation import response,BR,MX,ZH

DEC=dict(copy_approved=True,issues=[],selected_title_id=1,title_y=.17,subtitle_y=.78,cta_enabled=False,cta_side='left',override_reason='NONE')
ANALYSIS=dict(voice_quality='GOOD',clean_script=BR,source_language='en',selling_points=['compact buckle'],allowed_claims=['compact'],promotion_context='',context='Hands demonstrating a buckle',group_key='buckle',speaker='female',overlays_need_review=False)
def future(value):
 f=Future();f.set_result(value);return f
class CopyAdapter:
 def __init__(self):self.calls=[]
 def complete(self,model,messages,**kw):
  c=json.loads(messages[-1]['content']);self.calls.append(c)
  if 'items' in c:return dict(data={'items':[dict(id=r['id'],text_zh=ZH) for r in c['items']]})
  text=MX if c.get('target_language')=='es-MX' else BR
  if c.get('mode')=='SHORTEN':text='Olha esta fivela.'
  n=3 if c.get('mode')=='SELLING_POINTS' else 1
  return dict(data=dict(detected_language=c.get('input_language','pt-BR') if c.get('input_language')!='auto' else c.get('target_language','pt-BR'),candidates=[dict(language=c.get('target_language','pt-BR'),text=text+(['',' Veja este detalhe.',' Combine com seu estilo.'][i] if n==3 else ''),text_zh=ZH+(['',' 看看这个细节。',' 搭配你的风格。'][i] if n==3 else '')) for i in range(n)],titles=[dict(text=t['text'],text_zh=t['text_zh'],language=c.get('target_language','pt-BR')) for t in response(c.get('target_language','pt-BR'))['titles']]))
 def close(self):pass
class Queue:
 def __init__(self):self.enqueued=[];self.active={}
 def enqueue(self,*a):self.enqueued.append(a)
 def enqueue_item(self,*a):self.enqueued.append(a)
 def stop(self,*a):pass
class ReviewTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data');self.q=Queue();self.router=MagicMock();self.app=create_app(self.store,None,self.router,self.q,'fixture');self.service=self.app.state.review;self.adapter=CopyAdapter();self.app.state.translations.factory=lambda:self.adapter
  self.c=TestClient(self.app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer fixture'});self.source=self.root/'source.mp4';self.source.write_bytes(b'fixture source')
  self.j=self.job();self.id=self.j['id'];self.media=dict(path=str(self.source),rank=1,sha256=sha(self.source),duration=23.,width=1080,height=1920,fps=30,filename='source.mp4',status='VALID');self.store.item(self.id,1,status='REVIEW_REQUIRED',filename='source.mp4',source_path=str(self.source),source_snapshot=self.media,analysis=ANALYSIS,copy=response(),title_candidates=response()['titles'],selected_title_id=1,title=response()['titles'][0]['text'],issues=['NO_RELIABLE_SCRIPT'],decision=DEC)
 def tearDown(self):self.c.close();self.tmp.cleanup()
 def job(self,market='Brazil'):
  j,_=self.store.create(dict(request_id=os.urandom(8).hex(),input_path=str(self.root),product_name='腰带扣',product_profile_id='general',market=market,mode='Transport',script_mode='AUTO',title_approval_mode='auto',watermark_mode='handled'));self.store.update(j['id'],state='REVIEW_REQUIRED');return j
 def post(self,path,data=None):return self.c.post(f'/jobs/{self.id}/items/01/'+path,json=data or {})
 def draft(self,**data):
  r=self.post('script/draft',data);self.assertEqual(r.status_code,200,r.text);return r.json()
 def test_01_review_only_two_actions(self):
  b=Path('web/app.js').read_text()+Path('web/review.js').read_text()
  for old in ['保留原画后继续','生成中文对照','生成本页中文对照','重新随机生成脚本']:self.assertNotIn(old,b)
  self.assertIn('id="review-accept">确认通过',b);self.assertIn('id="review-continue">继续处理',b)
 def test_02_accept_never_queues_and_retains_artifacts(self):
  before=get_item(self.store,self.id,1)[1];r=self.post('review/accept');self.assertEqual(r.status_code,200);after=get_item(self.store,self.id,1)[1]
  self.assertEqual(after['status'],'PASS');self.assertEqual(after['copy'],before['copy']);self.assertEqual(after['decision'],before['decision']);self.assertEqual(self.q.enqueued,[]);self.assertEqual(self.adapter.calls,[])
 def test_03_routes_are_item_scoped(self):
  for code,route in [('NO_SPEECH','SCRIPT'),('TITLE_TOO_LONG','TITLE'),('TEXT_GHOSTING','VISUAL'),('VISUAL_DURATION_INSUFFICIENT','SUPPLEMENT')]:
   self.store.item(self.id,1,issues=[code]);r=self.post('review/continue');self.assertEqual(r.json()['route'],route)
  self.assertEqual(self.q.enqueued,[(self.id,'01','VISUAL')])
 def test_04_four_modes_present(self):
  b=Path('web/review.js').read_text()
  for label in ['从文案表选择','直接粘贴文案','纯音乐','根据卖点自动生成']:self.assertIn(label,b)
 def test_05_paste_target_keeps_original_and_one_bilingual_request(self):
  raw='  Olha esta fivela!\nFica linda.  ';d=self.draft(mode='PASTED',text=raw,language='pt-BR')
  self.assertEqual(d['candidates'][0]['text'],'Olha esta fivela! Fica linda.');self.assertTrue(d['candidates'][0]['text_zh']);self.assertEqual(len(self.adapter.calls),1);self.assertEqual(self.q.enqueued,[])
  self.draft(mode='PASTED',text=raw,language='pt-BR');self.assertEqual(len(self.adapter.calls),1)
 def test_06_paste_chinese_to_mexico(self):
  self.store.update(self.id,market='Mexico');d=self.draft(mode='PASTED',text='看看这个精致的带扣',language='zh-CN');self.assertEqual(d['candidates'][0]['text'],MX);self.assertEqual(d['original_text'],'看看这个精致的带扣');self.assertEqual(len(self.adapter.calls),1)
 def test_07_paste_english_to_brazil(self):
  d=self.draft(mode='PASTED',text='Look at this buckle.',language='en');self.assertEqual(d['candidates'][0]['text'],BR);self.assertEqual(d['original_language'],'en')
 def test_08_chinese_default_cache_and_no_repeated_http(self):
  self.store.item(self.id,1,copy=response(zh=False),title_candidates=response(zh=False)['titles']);r=self.c.post('/jobs/'+self.id+'/translations',json={'rank':'01'}).json();self.assertTrue(r['job']['items'][0]['script_text_zh']);self.assertEqual(r['http_requests'],1)
  self.assertEqual(self.c.post('/jobs/'+self.id+'/translations',json={'rank':'01'}).json()['http_requests'],0)
 def test_09_table_chinese_reused_and_snapshot_survives_file(self):
  from openpyxl import Workbook
  p=self.root/'文案.xlsx';w=Workbook();w.active.append(['编号','pt-BR','中文']);w.active.append(['W4',BR,ZH]);w.save(p);self.c.post('/native/grant',json={'path':str(p)});rows=self.c.post('/references/inspect',json={'path':str(p)}).json()['scripts']['Brazil']
  d=self.draft(mode='TABLE',reference_path=str(p),script_reference_id=rows[0]['id']);p.unlink();self.assertEqual(d['candidates'][0]['text'],BR);self.assertEqual(self.adapter.calls,[]);self.assertTrue(d['reference_snapshot']['rows'])
 def test_10_music_no_copy_provider_and_explicit_confirm(self):
  music=dict(path='fixture.wav',sha256='sha');d=self.service.draft(self.id,1,dict(mode='MUSIC_ONLY',music=music));self.assertEqual(d['candidates'],[]);self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.q.enqueued,[])
  self.service.approve(self.id,1,d['id'],None);x=get_item(self.store,self.id,1)[1];self.assertEqual(x['voiceover_mode'],'MUSIC_ONLY');self.assertIsNone(x['final_script'])
 def test_11_music_plan_no_captions_and_no_chinese(self):
  with patch('autocut.context.HOME',self.store.home):
   p=make_plan(self.media,dict(duration=23,path='music.wav',sha256='music'),production_copy(response()),DEC,'Olha esta fivela',self.root/'work',self.root/'final.mp4',{},music_only=True)
  self.assertEqual(p['visual']['subtitle']['captions'],[]);self.assertEqual(p['audio_speed'],1.);self.assertEqual(p['voiceover_mode'],'MUSIC_ONLY');self.assertNotIn(ZH,json.dumps(p,ensure_ascii=False))
 def test_12_selling_exact_three_bilingual(self):
  d=self.draft(mode='SELLING_POINTS',selling_points='小巧，便于搭配');self.assertEqual(len(d['candidates']),3);self.assertTrue(all(c['text_zh'] for c in d['candidates']));self.assertEqual(len(self.adapter.calls),1)
 def test_13_gate_before_tts(self):
  d=self.draft(mode='PASTED',text=BR,language='pt-BR');m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event()
  with patch('autocut.item_pipeline.tts_job') as tts:resume_item(m,self.id,'01','SCRIPT');tts.assert_not_called()
  self.assertEqual(get_item(self.store,self.id,1)[1]['status'],'WAITING_SCRIPT_APPROVAL');m.queue.shutdown()
 def test_14_approval_invalidates_only_current_downstream(self):
  self.store.item(self.id,2,status='PASS',copy=response(),audio_result={'path':'other.mp3'});other=get_item(self.store,self.id,2)[1]
  for rank in [1,2]:
   for stage in ['ASR_DONE','VIDEO_ANALYSIS','COPY_DONE','TTS_DONE','FINAL_RENDERED','QA_DONE']:self.store.done(self.id,stage,rank,'fp',{'kept':True})
  d=self.draft(mode='PASTED',text=BR,language='pt-BR');self.service.approve(self.id,1,d['id'],'1');self.service.approve(self.id,1,d['id'],'1')
  self.assertEqual(self.q.enqueued,[(self.id,1,'SCRIPT')]);self.assertEqual(get_item(self.store,self.id,2)[1],other)
  self.assertTrue(self.store.checkpoint(self.id,'ASR_DONE',1,'fp'));self.assertTrue(self.store.checkpoint(self.id,'TTS_DONE',2,'fp'));self.assertIsNone(self.store.checkpoint(self.id,'TTS_DONE',1,'fp'))
 def test_15_duration_42_23_and_effective_ranges(self):
  r=duration_check(42,base_clips(self.media,DEC,{}));self.assertFalse(r['fits']);self.assertEqual(r['missing_duration'],19)
  r=duration_check(20,base_clips(self.media,dict(DEC,keep_ranges=[[0,5],[10,15]]),{'remove_redundant_sections':True}));self.assertEqual(r['available_visual_duration'],10);self.assertFalse(r['fits'])
  self.assertTrue(duration_check(25,base_clips(self.media,DEC,{}))['fits']);self.assertFalse(duration_check(30,base_clips(self.media,DEC,{}),{'audio_speed_preference':1})['fits'])
 def test_16_url_downloader_fixed_argv_fixture(self):
  folder=self.store.home/'jobs'/self.id/'work/supplements';folder.mkdir(parents=True);out=folder/'fixture.mp4';out.write_bytes(b'fixture download')
  with patch('autocut.review_media.subprocess.run',return_value=MagicMock(stdout=str(out)+'\n')) as run:
   self.assertEqual(DownloadService().download('https://www.tiktok.com/@fixture/video/1',folder),out.resolve());args=run.call_args.args[0];self.assertIn('--ignore-config',args);self.assertEqual(args[-1],'https://www.tiktok.com/@fixture/video/1');self.assertNotIn('shell',run.call_args.kwargs)
 def test_17_download_failure_item_isolation(self):
  self.store.item(self.id,2,status='PASS');before=get_item(self.store,self.id,2)[1];self.service.downloader=MagicMock();self.service.downloader.download.side_effect=ValueError('SUPPLEMENT_DOWNLOAD_FAILED')
  r=self.service.add_media(self.id,1,[],['https://www.tiktok.com/@fixture/video/1']);self.assertEqual(len(r['failures']),1);self.assertEqual(get_item(self.store,self.id,2)[1],before)
 def test_18_local_supplement_source_unchanged(self):
  before=sha(self.source)
  with patch('autocut.review_media.probe',return_value={'format':{'duration':23},'streams':[{'codec_type':'video','avg_frame_rate':'30/1','width':1080,'height':1920}]}):r=import_media(self.store.home,self.id,self.source)
  self.assertEqual(sha(self.source),before);self.assertEqual(r['sha256'],before);self.assertIn('/work/supplements/',r['path']);self.assertNotEqual(r['path'],str(self.source))
 def supplement(self):
  p=self.root/'supp.mp4';p.write_bytes(b'separate fixture');return dict(id='supp',path=str(p),duration=25,sha256=sha(p),fps=30,width=1080,height=1920)
 def plan(self):return dict(status='MATCH',reason='相同产品近景',clips=[dict(supplement_source='supp',source_interval=[0,20],target_duration=20,insert_position=23,matched_script_segment=BR[:10],reason='对应搭配卖点')])
 def test_19_semantic_plan_uses_whitelisted_interval(self):
  clips=resolve_supplements(self.media,DEC,{},self.plan(),[self.supplement()],BR);self.assertEqual(len(clips),2);self.assertEqual(clips[1]['source_end'],20)
 def test_20_mismatch_and_fabricated_plan_not_used(self):
  for plan in [dict(status='SUPPLEMENT_CONTENT_MISMATCH',reason='不同产品',clips=[]),dict(self.plan(),clips=[dict(self.plan()['clips'][0],matched_script_segment='not in script')]),dict(self.plan(),clips=[dict(self.plan()['clips'][0],supplement_source='/etc/passwd')])]:
   with self.assertRaises(ValueError):resolve_supplements(self.media,DEC,{},plan,[self.supplement()],BR)
 def test_21_recheck_and_job_local_reuse(self):
  self.store.item(self.id,1,audio_result={'duration':42},status='WAITING_SUPPLEMENT_MEDIA');self.store.update(self.id,supplement_media=[self.supplement()]);self.router.resolve.return_value._request.return_value=SupplementPlan.model_validate(self.plan())
  with patch('autocut.pipeline.source_sheet',return_value='fixture.jpg'):self.service.plan_media(self.id,1)
  x=get_item(self.store,self.id,1)[1];self.assertTrue(x['duration_match']['fits']);self.assertEqual(self.q.enqueued,[(self.id,1,'SUPPLEMENT')]);self.assertIn('supplement_plan',x);self.assertNotIn('supplement_media',self.job())
 def test_22_shorten_requires_new_confirmation(self):
  self.store.item(self.id,1,duration_match={'available_visual_duration':23});d=self.draft(mode='SHORTEN');self.assertEqual(len(d['candidates']),1);self.assertEqual(self.adapter.calls[0]['target_seconds'],23);self.assertEqual(self.q.enqueued,[])
 def test_23_one_waits_other_nine_continue(self):
  m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event();sources=[dict(self.media,rank=i) for i in range(1,11)]
  def copies(ctx,*a):return {'items':[dict(script_group_id=c['script_group_id'],response=response()) for c in ctx]}
  def tts(*a,**k):
   n=tts.count;tts.count+=1;return future(dict(duration=42 if n==0 else 5,path='mock.wav',sha256='mock',alignment_path='mock.json',fingerprint='fp'+str(n),technical_status='PASS'))
  tts.count=0
  def finish(id,h,j,plans,work):
   for p in plans:self.store.item(id,p['rank'],status='PASS')
  cat={'voices':[dict(id='v',name='Carla')],'scope':'mock'};h=MagicMock();h.__enter__.return_value=h
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=h),patch('autocut.pipeline.scan',return_value=sources),patch('autocut.pipeline.asr_job',return_value=future({'text':'speech'})),patch('autocut.pipeline.source_sheet',return_value='sheet.jpg'),patch.object(m,'judge',side_effect=lambda *a:copy.deepcopy(ANALYSIS if a[5]=='analyze' else DEC)),patch.object(m,'copy_batch',side_effect=copies),patch.object(m,'catalog',return_value=cat),patch('autocut.pipeline.tts_payload',return_value=({'tts_text':BR},None)),patch('autocut.pipeline.tts_job',side_effect=tts),patch('autocut.pipeline.make_plan',side_effect=lambda s,*a,**kw:dict(rank=f'{s["rank"]:02}')),patch.object(m,'finish_plans',side_effect=finish):m.run(self.id)
  j=self.store.get(self.id);self.assertEqual(j['items'][0]['status'],'WAITING_SUPPLEMENT_MEDIA',j);self.assertEqual(sum(i['status']=='PASS' for i in j['items']),9);m.queue.shutdown()
 def test_24_cached_tts_supplement_resume_does_not_charge(self):
  audio=self.root/'tts.wav';audio.write_bytes(b'audio fixture');m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event();supp=self.supplement();self.store.update(self.id,supplement_media=[supp]);self.store.item(self.id,1,status='COPY_READY',script_status='USER_APPROVED',audio_result=dict(path=str(audio),sha256=sha(audio),duration=42,fingerprint='existing'),supplement_plan=self.plan());h=MagicMock();h.__enter__.return_value=h
  with patch('autocut.item_pipeline.Harness',return_value=h),patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.pipeline.make_plan',return_value={'rank':'01'}) as plan,patch.object(m,'finish_plans',side_effect=lambda *a:self.store.item(self.id,1,status='PASS')):
   resume_item(m,self.id,'01','SUPPLEMENT');tts.assert_not_called();plan.assert_called_once();self.assertEqual(len(plan.call_args.args[0]['remix_clips']),2)
  self.assertEqual(get_item(self.store,self.id,1)[1]['status'],'PASS');m.queue.shutdown()
 def test_25_schema3_restart_old_jobs_and_new_draft(self):
  d=self.draft(mode='PASTED',text=BR,language='pt-BR');old=self.job();self.store.item(old['id'],1,status='PASS');again=Store(self.store.home);self.assertEqual(again.get(self.id)['items'][0]['script_draft'],d);self.assertEqual(again.get(old['id'])['items'][0]['status'],'PASS')
  with again.connect() as db:self.assertEqual(db.execute('SELECT MAX(version) FROM migrations').fetchone()[0],3)
 def test_26_final_script_rework_requires_explicit_ack(self):
  output=self.root/'old.mp4';output.write_bytes(b'export');self.store.item(self.id,1,output=str(output));self.assertEqual(self.post('script/draft',dict(mode='PASTED',text=BR)).json()['error'],'ITEM_RENDER_CONFIRMATION_REQUIRED')
  d=self.draft(mode='PASTED',text=BR,confirmed_rework=True);self.service.approve(self.id,1,d['id'],'1');self.assertEqual(output.read_bytes(),b'export')
 def test_27_deleted_production_routes_blocked(self):
  self.c.post('/jobs/'+self.id+'/delete',json={})
  for route in ['script/draft','script/approve','review/accept','review/continue','supplements','supplements/reuse','title/approve','title/draft']:self.assertEqual(self.post(route).json()['error'],'JOB_DELETED',route)
 def test_28_malicious_urls_and_ungranted_local_paths(self):
  for url in ['file:///etc/passwd','http://127.0.0.1/test','https://tiktok.com.evil.example/','https://user@tiktok.com/a']:
   with self.assertRaises(ValueError):DownloadService().download(url,self.root)
  self.assertEqual(self.post('supplements',{'paths':[str(self.source)]}).status_code,403)
 def test_29_title_change_reuses_audio(self):
  self.store.item(self.id,1,audio_result={'path':'voice.wav'},issues=['TITLE_TOO_LONG']);r=self.post('title/approve',{'candidate':2});self.assertEqual(r.status_code,200,r.text);self.assertEqual(get_item(self.store,self.id,1)[1]['audio_result'],{'path':'voice.wav'});self.assertEqual(self.q.enqueued[-1],(self.id,'01','TITLE'))
 def test_30_invalidated_same_paste_new_revision(self):
  d=self.draft(mode='PASTED',text=BR,language='pt-BR');self.service.approve(self.id,1,d['id'],'1');next_d=self.draft(mode='PASTED',text=BR,language='pt-BR');self.assertNotEqual(next_d['id'],d['id']);self.service.approve(self.id,1,next_d['id'],'1');self.assertEqual(get_item(self.store,self.id,1)[1]['script_revision'],2)
 def test_31_music_resume_makes_zero_tts_calls(self):
  d=self.service.draft(self.id,1,dict(mode='MUSIC_ONLY',music={'path':'fixture.wav','sha256':'sha'}));self.service.approve(self.id,1,d['id'],None);m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h
  with patch('autocut.item_pipeline.Harness',return_value=h),patch.object(m,'judge',return_value=DEC),patch('autocut.item_pipeline.music_audio',return_value={'duration':23,'path':'fixture.wav','sha256':'sha'}),patch('autocut.item_pipeline.tts_job') as tts,patch('autocut.pipeline.make_plan',return_value={'rank':'01'}) as plan,patch.object(m,'finish_plans',side_effect=lambda *a:self.store.item(self.id,1,status='PASS')):
   resume_item(m,self.id,'01','SCRIPT');tts.assert_not_called();plan.assert_called_once();self.assertTrue(plan.call_args.kwargs['music_only'])
  self.assertEqual(get_item(self.store,self.id,1)[1]['status'],'PASS');m.queue.shutdown()
 def test_32_asr_failure_auto_fallback_validates_real_copy_contract(self):
  from test_copy_presentation import MockAdapter
  from app.s3.deepseek_batch import generate_batch
  m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h;adapter=MockAdapter();contexts=[]
  def copies(rows,work,model):contexts.extend(rows);return generate_batch(rows,work,lambda:adapter,model=model)
  analysis=dict(ANALYSIS,voice_quality='NO_SPEECH',clean_script='')
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=h),patch('autocut.pipeline.scan',return_value=[self.media]),patch('autocut.pipeline.asr_job',return_value=future({'error':'NO_SPEECH'})),patch('autocut.pipeline.source_sheet',return_value='sheet.jpg'),patch.object(m,'judge',side_effect=lambda *a:copy.deepcopy(analysis if a[5]=='analyze' else DEC)),patch.object(m,'copy_batch',side_effect=copies),patch.object(m,'catalog',side_effect=ValueError('FIXTURE_STOP_BEFORE_TTS')):m.run(self.id)
  x=get_item(self.store,self.id,1)[1];self.assertEqual(x['script_source']['type'],'SELLING_POINTS');self.assertTrue(x['copy']['localized_script']['text']);self.assertIn('No reliable source speech',contexts[0]['clean_script']);self.assertEqual(len(adapter.calls),1);m.queue.shutdown()
 def test_33_resumed_tts_receives_only_approved_foreign_text(self):
  d=self.draft(mode='PASTED',text=BR,language='pt-BR');self.service.approve(self.id,1,d['id'],'1');m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event();h=MagicMock();h.__enter__.return_value=h
  def fake_tts(h,payload,**kw):
   self.assertEqual(kw['review']['script_status'],'CODEX_APPROVED');self.assertEqual(payload['tts_text'],BR);self.assertNotIn(ZH,json.dumps(payload,ensure_ascii=False));return future(dict(duration=5,path='mock.wav',sha256='mock',fingerprint='fp',technical_status='PASS'))
  with patch('autocut.item_pipeline.Harness',return_value=h),patch.object(m,'judge',return_value=DEC),patch.object(m,'catalog',return_value={'voices':[{'id':'v','name':'Carla'}],'scope':'fixture'}),patch('autocut.item_pipeline.tts_payload',return_value=({'tts_text':BR},None)),patch('autocut.item_pipeline.tts_job',side_effect=fake_tts) as tts,patch('autocut.pipeline.make_plan',return_value={'rank':'01'}),patch.object(m,'finish_plans',side_effect=lambda *a:self.store.item(self.id,1,status='PASS')):
   resume_item(m,self.id,'01','SCRIPT');tts.assert_called_once()
  self.assertEqual(get_item(self.store,self.id,1)[1]['status'],'PASS');m.queue.shutdown()
 def test_34_inflight_copy_cannot_be_replaced_before_initial_tts(self):
  self.store.update(self.id,state='RUNNING');self.store.item(self.id,1,status='COPY_READY');self.assertEqual(self.post('script/draft',dict(mode='PASTED',text=BR)).json()['error'],'ITEM_BUSY');self.assertEqual(self.adapter.calls,[])
 def test_35_queue_duplicate_approval_is_coalesced(self):
  m=Manager(self.store,None,self.router);gate=threading.Event();started=threading.Event();calls=[]
  def worker(*args):calls.append(args);started.set();gate.wait(2)
  with patch('autocut.item_pipeline.resume_item',side_effect=worker):
   m.enqueue_item(self.id,1);started.wait(2);m.enqueue_item(self.id,1);gate.set();m.queue.shutdown()
  self.assertEqual(len(calls),1)
 def test_36_original_approval_preserves_item_replacement(self):
  from autocut.workbench import copy_candidates
  r=response();candidates=copy_candidates({'g':{'response':r}},'g')
  self.store.update(self.id,state='WAITING_SCRIPT_APPROVAL',script_mode='REQUIRE_APPROVAL')
  self.store.item(self.id,1,status='COPY_READY',script_candidates=candidates,script_status='WAITING_APPROVAL')
  self.store.item(self.id,2,status='COPY_READY',copy=r,title_candidates=r['titles'],script_candidates=candidates,script_status='WAITING_APPROVAL')
  d=self.draft(mode='PASTED',text='Olha esta fivela nova.',language='pt-BR');self.service.approve(self.id,1,d['id'],'1')
  out=self.c.post('/jobs/'+self.id+'/approve-scripts',json={});self.assertEqual(out.status_code,200,out.text)
  self.assertEqual(get_item(self.store,self.id,1)[1]['copy']['localized_script']['text'],'Olha esta fivela nova.')
  self.assertTrue(all(len(call)==3 for call in self.q.enqueued));self.assertIn((self.id,'02','SCRIPT'),self.q.enqueued)
if __name__=='__main__':unittest.main()
