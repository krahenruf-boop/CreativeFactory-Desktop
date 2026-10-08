"""0.1.10 language regressions: local fixtures, no paid API/ASR/render."""
import copy,json,threading,unittest
from unittest.mock import patch,MagicMock
from pathlib import Path
from test_review_flow import ReviewTests,DEC,ANALYSIS,future
from test_copy_presentation import response,context,BR,MX,ZH,MockAdapter
from autocut.script_language import *
from autocut.copy_presentation import production_copy,presented_job
from autocut.workbench import existing_scripts,selected_context,approve_scripts,copy_candidates
from autocut.pipeline import Manager,make_plan
from autocut.item_pipeline import resume_item
from autocut.item_review import get_item
from autocut.provider_models import tts_payload
from app.s3.deepseek_copy import validation,prepare
from app.s3.deepseek_batch import generate_batch
from app.s3.headless_runtime import write,sha

EN='This eyeshadow is so easy to use. You can apply it with your fingers and your skin looks smooth.'
CAT=dict(models=[dict(id='eleven_flash_v2_5',max_chars=5000,languages=[{'language_id':'pt'},{'language_id':'es'}],supported={'speed':True,'stability':True,'similarity_boost':True})],voices=[dict(id='voice',name='Carla')],scope='fixture')
class LanguageTests(unittest.TestCase):
 setUp=ReviewTests.setUp
 tearDown=ReviewTests.tearDown
 post=ReviewTests.post
 draft=ReviewTests.draft
 job=ReviewTests.job
 def market(self,market):self.store.update(self.id,market=market)
 def approve(self,d):return self.service.approve(self.id,1,d['id'],'1')
 def test_a_b_english_source_localizes_both_markets(self):
  for market,lang in [('Mexico','es-MX'),('Brazil','pt-BR')]:
   self.market(market);j=self.store.get(self.id);x=get_item(self.store,self.id,1)[1];bad=response(lang);bad['localized_script']['text']=EN
   r=self.service.localize_response(j,x,bad);self.assertEqual(check(r['localized_script'],lang)['language'],lang)
 def test_c_d_paste_english_and_native_mexico(self):
  self.market('Mexico');d=self.draft(mode='PASTED',text=EN,language='en');self.assertEqual(d['candidates'][0]['text'],MX);self.assertEqual(d['candidates'][0]['language'],'es-MX')
  native='Ya dejé de cargar una bolsa enorme. Mira este detalle para tu ropa.'
  d=self.draft(mode='PASTED',text=native,language='auto');self.assertEqual(d['candidates'][0]['text'],native)
 def test_e_brazil_pasted_chinese(self):
  d=self.draft(mode='PASTED',text='这款眼影方便上手',language='zh-CN');x=self.approve(d);self.assertEqual(x['final_script_language'],'pt-BR');self.assertEqual(x['final_script'],BR);self.assertEqual(x['source_script'],'这款眼影方便上手')
 def test_f_table_english_must_localize(self):
  self.market('Mexico');ref=dict(filename='fixture.csv',rows=[{'编号':'W4','EN':EN,'中文':'英文原稿的中文对照'}]);self.store.update(self.id,reference=ref)
  from autocut.workbench import table_scripts
  d=self.draft(mode='TABLE',script_reference_id=table_scripts(ref,'Mexico')[0]['id']);self.assertEqual(d['candidates'][0]['text'],MX);self.assertEqual(len(self.adapter.calls),1)
 def test_g_history_detects_actual_english(self):
  self.market('Mexico');r=response('es-MX');r['localized_script']['text']=EN;self.store.item(self.id,1,copy=r,final_script=EN,final_script_language='es-MX')
  rows=existing_scripts(self.store,'腰带扣','Mexico');self.assertEqual(rows[0]['language'],'en');self.assertEqual(rows[0]['localization_status'],'NEEDS_LOCALIZATION')
  j=dict(self.store.get(self.id),script_snapshot=rows[0]);c=selected_context(context('es-MX'),j);self.assertEqual(c['source_language'],'en');self.assertEqual(c['language'],'es-MX')
  generated=self.service.localize_response(j,self.store.get(self.id)['items'][0],r);self.assertEqual(generated['localized_script']['text'],MX)
 def test_h_i_selling_points_direct_target(self):
  for market,lang in [('Mexico','es-MX'),('Brazil','pt-BR')]:
   self.market(market);d=self.draft(mode='SELLING_POINTS',selling_points='compact');self.assertEqual(len(d['candidates']),3)
   for c in d['candidates']:self.assertEqual(check(c,lang)['language'],lang)
 def test_j_no_speech_auto_target_and_final_snapshot(self):
  self.market('Mexico');m=Manager(self.store,None,None);m.stops[self.id]=threading.Event();a=dict(ANALYSIS,clean_script='',voice_quality='NO_SPEECH');h=MagicMock();h.__enter__.return_value=h
  def copies(contexts,*_):
   self.assertTrue(all(c['language']=='es-MX' for c in contexts));return {'items':[dict(script_group_id=c['script_group_id'],response=response('es-MX')) for c in contexts]}
  with patch('autocut.pipeline.HOME',self.store.home),patch('autocut.pipeline.Harness',return_value=h),patch('autocut.pipeline.scan',return_value=[self.media]),patch('autocut.pipeline.asr_job',return_value=future({'text':''})),patch('autocut.pipeline.source_sheet',return_value='fixture.jpg'),patch.object(m,'judge',side_effect=lambda *args:a if args[5]=='analyze' else DEC),patch.object(m,'copy_batch',side_effect=copies),patch.object(m,'catalog',side_effect=ValueError('TEST_STOP_AT_TTS')):
   m.run(self.id)
  x=self.store.get(self.id)['items'][0];self.assertEqual(x['final_script'],MX);self.assertEqual(x['final_script_language'],'es-MX');m.queue.shutdown()
 def test_k_music_exempt(self):
  d=self.service.draft(self.id,1,dict(mode='MUSIC_ONLY',music={'path':'fixture.wav','sha256':'x'}));x=self.approve(d);self.assertIsNone(x['final_script']);self.assertEqual(issues(self.store.get(self.id),x),[])
 def test_l_tts_rejects_wrong_body_despite_metadata(self):
  for language in ['es-MX','pt-BR']:
   with self.assertRaisesRegex(ValueError,'FINAL_SCRIPT_LANGUAGE_MISMATCH'):tts_payload(EN,'voice','eleven_flash_v2_5',CAT,language)
 def test_m_subtitle_rejects_source_and_wrong_alignment(self):
  with self.assertRaisesRegex(ValueError,'FINAL_SCRIPT_LANGUAGE_MISMATCH'):subtitle_guard({'text':EN,'language':'es-MX'},'es-MX',{}, {})
  with self.assertRaisesRegex(ValueError,'SUBTITLE_SCRIPT_MISMATCH'):subtitle_guard({'text':MX,'language':'es-MX'},'es-MX',{'characters':list(EN)},{})
 def test_subtitle_numbers_are_same_confirmed_script(self):
  with self.assertRaisesRegex(ValueError,'SUBTITLE_SCRIPT_MISMATCH'):subtitle_guard({'text':'Solo 10 unidades para ti.','language':'es-MX'},'es-MX',{'characters':list('Solo 100 unidades para ti.')},{})
 def test_n_title_renderer_rejects_english(self):
  with self.assertRaisesRegex(ValueError,'TITLE_LANGUAGE_MISMATCH'):
   make_plan(self.media,{},production_copy(response('es-MX')),DEC,'This is so easy',self.root/'work',self.root/'out.mp4',{},target='es-MX')
 def test_o_p_chinese_isolated_from_tts_and_subtitle(self):
  r=response('es-MX');payload,_=tts_payload(MX,'voice','eleven_flash_v2_5',CAT,'es-MX');self.assertNotIn(ZH,json.dumps(payload,ensure_ascii=False))
  align=self.root/'alignment.json';write(align,dict(alignment=dict(characters=list(MX),character_start_times_seconds=[i*.02 for i in range(len(MX))],character_end_times_seconds=[(i+1)*.02 for i in range(len(MX))])))
  audio=dict(duration=6,path='voice.mp3',alignment_path=str(align),script_text=MX)
  p=make_plan(dict(self.media,duration=8),audio,production_copy(r),DEC,r['titles'][0]['text'],self.root/'work',self.root/'out.mp4',{},target='es-MX')
  self.assertNotIn(ZH,json.dumps(p,ensure_ascii=False));self.assertEqual(p['final_script'],MX);self.assertTrue(p['visual']['subtitle']['captions'])
 def legacy(self):
  self.market('Mexico');r=response('es-MX');r['localized_script']['text']=EN;self.store.item(self.id,1,status='REVIEW_REQUIRED',final_script=EN,final_script_language='es-MX',copy=r,analysis=dict(ANALYSIS,clean_script=EN),source_script=EN,source_language='en');self.store.update(self.id,state='COMPLETED')
  # Inject pre-0.1.11 persisted bad data; the new Store correctly refuses to create it.
  with self.store.connect() as db:
   row=self.store.get(self.id)['items'][0];row['status']='PASS';db.execute('UPDATE job_items SET data=? WHERE job_id=? AND rank=?',(json.dumps(row),self.id,'01'))
 def test_q_legacy_get_no_paid_and_no_manual_pass(self):
  self.legacy();raw=self.store.get(self.id);j=self.c.get('/jobs/'+self.id).json();x=j['items'][0]
  self.assertIn('LEGACY_SCRIPT_LANGUAGE_MISMATCH',x['language_issues']);self.assertTrue(x['blocking_review']);self.assertIsNone(x['final_script']);self.assertEqual(self.adapter.calls,[]);self.assertEqual(raw,self.store.get(self.id))
  self.assertEqual(self.post('review/accept').status_code,409);self.c.post('/jobs/'+self.id+'/translations',json={'rank':'01'});self.assertEqual(self.adapter.calls,[])
 def test_r_legacy_continue_localization_downstream_only(self):
  self.legacy();self.store.item(self.id,2,status='WAITING_TITLE_APPROVAL',copy=response('es-MX'));other=self.store.get(self.id)['items'][1]
  self.store.done(self.id,'ASR_DONE',1,'fp',{'path':'old'});self.store.done(self.id,'VIDEO_ANALYSIS',1,'fp',{'old':True});self.store.done(self.id,'TTS_DONE',1,'fp',{'old':True})
  r=self.post('review/continue');self.assertEqual(r.status_code,200,r.text);d=r.json()['draft'];self.assertEqual(r.json()['route'],'LOCALIZATION');self.assertEqual(d['candidates'][0]['text'],MX);self.assertEqual(self.q.enqueued,[])
  self.approve(d);self.assertEqual(self.q.enqueued,[(self.id,1,'SCRIPT')]);self.assertEqual(self.store.get(self.id)['items'][1],other);self.assertTrue(self.store.checkpoint(self.id,'ASR_DONE',1,'fp'));self.assertTrue(self.store.checkpoint(self.id,'VIDEO_ANALYSIS',1,'fp'));self.assertIsNone(self.store.checkpoint(self.id,'TTS_DONE',1,'fp'))
 def test_s_t_ui_final_and_source_separate(self):
  self.market('Mexico');r=response('es-MX');self.store.item(self.id,1,copy=r,title_candidates=r['titles'],title=r['titles'][0]['text'],final_script=MX,final_script_language='es-MX',source_script=EN,analysis=dict(ANALYSIS,clean_script=EN))
  j=self.c.get('/jobs/'+self.id).json();x=j['items'][0];self.assertEqual(x['final_script'],MX);self.assertEqual(x['source_script'],EN)
  ui=Path('web/review.js').read_text();self.assertIn("text=x.final_script||''",ui);self.assertNotIn("x.analysis?.clean_script",ui);self.assertIn('查看原始口播',ui)
 def test_metadata_and_prompt_all_models(self):
  for lang in ['es-MX','pt-BR']:
   for model in ['deepseek-flash','deepseek-v4-pro']:
    a=MockAdapter();out=generate_batch([context(lang)],self.root/lang/model,lambda:a,model=model);self.assertEqual(out['http_requests'],1);self.assertIn(NAMES[lang],a.calls[0][1][0]['content'])
    for t in out['items'][0]['response']['titles']:self.assertEqual(t['language'],lang)
   bad=response(lang);bad['localized_script']['text']=EN
   with self.assertRaises(Exception):validation(bad,context(lang))
 def test_mapping_never_source_and_future_preset(self):
  self.assertEqual(target_language({'market':'MX','language':'en','product_profile':{'language':'pt-BR'}}),'es-MX');self.assertEqual(target_language({'market':'BR','target_language':'en'}),'pt-BR');self.assertEqual(target_language({'market':'Future','preset_snapshot':{'target_language':'en'}}),'en')
 def test_approval_guard_cannot_use_language_label_bypass(self):
  d=self.draft(mode='PASTED',text=EN,language='es-MX');self.assertEqual(d['candidates'][0]['text'],BR)
  d['candidates'][0]['text']=EN;self.store.item(self.id,1,script_draft=d)
  with self.assertRaisesRegex(ValueError,'FINAL_SCRIPT_LANGUAGE_MISMATCH'):self.approve(d)
  self.assertEqual(self.q.enqueued,[])
 def test_resume_rejects_before_catalog_or_tts(self):
  self.legacy();self.store.item(self.id,1,status='COPY_READY');m=Manager(self.store,None,self.router);m.stops[self.id]=threading.Event()
  with patch.object(m,'catalog') as cat,patch('autocut.item_pipeline.tts_job') as tts:
   resume_item(m,self.id,1,'SCRIPT');cat.assert_not_called();tts.assert_not_called()
  self.assertIn('FINAL_SCRIPT_LANGUAGE_MISMATCH',self.store.get(self.id)['items'][0]['issues']);m.queue.shutdown()
 def test_no_localized_script_no_source_fallback(self):
  self.store.item(self.id,1,copy={},analysis=dict(ANALYSIS,clean_script=EN));j=self.c.get('/jobs/'+self.id).json();self.assertIsNone(j['items'][0]['final_script']);self.assertIn('LOCALIZATION_REQUIRED',j['items'][0]['issues'])

if __name__=='__main__':unittest.main()
