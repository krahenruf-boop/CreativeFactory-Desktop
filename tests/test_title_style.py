"""Real copy/API paths with mocked providers; no video pipeline or paid calls."""
import copy,importlib.util,json,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock
import test_batch_workspace as batch_fixture
from test_copy_presentation import response,context,BR,MX,ZH
from autocut.title_style import GUIDANCE,VERSION,brief
from app.s3.deepseek_copy import prepare,validation
from app.s3.deepseek_batch import generate_batch
from app.s2.deepseek import DeepSeekError

TEXTS={
 'pt-BR':[
  'Seu cinto aperta quando você senta e deixa a cintura desconfortável? Olha esse jeito de ajustar sem esforço.',
  'Olha só!',
  'Você ainda precisa ficar ajustando a roupa toda vez que senta? Veja como esse detalhe deixa seu look mais prático no dia a dia.'
 ],
 'es-MX':[
  '¿Tu cinturón aprieta cuando te sientas y deja la cintura incómoda? Mira esta forma de ajustar sin esfuerzo.',
  '¡Mira esto!',
  '¿Todavía tienes que ajustar tu ropa cada vez que te sientas? Mira cómo este detalle hace tu look más práctico en el día a día.'
 ]}

def made(language):
 r=response(language)
 for t,text,angle in zip(r['titles'],TEXTS[language],['discovery','demonstration','contrast']):
  t.update(text=text,angle=angle,text_zh='对应场景与使用结果的中文对照')
 return r

class BatchAdapter:
 def __init__(self):self.calls=[]
 def complete(self,model,messages,**kwargs):
  self.calls.append(messages);items=json.loads(messages[-1]['content'])['items']
  return {'data':{'items':[dict(id=x['id'],result=made(x['context']['language'])) for x in items]}}
 def close(self):pass

class TitleStyleTests(unittest.TestCase):
 def test_single_and_batch_accept_long_short_and_free_angles_in_both_markets(self):
  with tempfile.TemporaryDirectory() as tmp:
   a=BatchAdapter();contexts=[context(lang) for lang in TEXTS]
   for c in contexts:
    c['title_brief']=brief();r=made(c['language']);self.assertGreater(len(r['titles'][0]['text'].split()),14)
    self.assertEqual(validation(r,c)['titles'][1]['text'],TEXTS[c['language']][1])
   out=generate_batch(contexts,Path(tmp),lambda:a)
   self.assertEqual(out['http_requests'],1);self.assertEqual(out['item_repairs'],0)
   self.assertEqual([r['status'] for r in out['items']],['REVIEW','REVIEW'])
   self.assertEqual([t['angle'] for t in out['items'][0]['response']['titles']],['discovery','demonstration','contrast'])
   self.assertIn(GUIDANCE,a.calls[0][0]['content'])
   cached=generate_batch(contexts,Path(tmp),lambda:a)
   self.assertEqual(cached['http_requests'],0);self.assertEqual(cached['cache_hits'],2);self.assertEqual(len(a.calls),1)
 def test_real_wrong_language_duplicates_and_unapproved_claims_still_rejected(self):
  c=context();bad=made('pt-BR');bad['titles'][0].update(text='This belt is easy to use with your clothes',language='pt-BR')
  with self.assertRaises(DeepSeekError):validation(bad,c)
  bad=made('pt-BR');bad['titles'][1]['text']=bad['titles'][0]['text']
  with self.assertRaises(DeepSeekError):validation(bad,c)
  bad=made('pt-BR');bad['titles'][0]['text']='Você precisa aproveitar o desconto de 60% OFF agora'
  with self.assertRaises(DeepSeekError):validation(bad,c)
 def test_all_effective_engine_prompts_have_no_old_contract(self):
  for language in TEXTS:
   _,spec,_,messages=prepare(dict(context(language),title_brief=brief()))
   self.assertIn(VERSION,messages[0]['content']);self.assertEqual(spec['context']['title_brief'],brief())
   self.assertNotIn('3–10 words',messages[0]['content']);self.assertNotIn('1–2 lines',messages[0]['content'])
  self.assertLess(len(brief('x'*9000,True)),3000)
  source=Path('backend/autocut/pipeline.py').read_text();self.assertEqual(source.count('title_brief=title_brief('),2)
  self.assertNotIn('A pain hook, B product benefit, C emotion',source)
 def test_engine_extension_survives_fresh_staging_and_is_idempotent(self):
  spec=importlib.util.spec_from_file_location('title_extension',Path('scripts/title_style_compatibility.py'));mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   names=['prompts/DEEPSEEK_TIKTOK_BR_V1.txt','prompts/DEEPSEEK_TIKTOK_BR_BATCH_V1.txt','app/s3/deepseek_copy.py','app/s3/deepseek_batch.py']
   for name in names:
    target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(Path('vendor/engine')/name,target)
   mod.apply(root);first={n:(root/n).read_text() for n in names};mod.apply(root)
   self.assertEqual(first,{n:(root/n).read_text() for n in names})
   self.assertFalse(any('3–10 words' in s for s in first.values()))
   self.assertNotIn("['pain_hook','product_benefit','emotion']",first[names[3]])
   self.assertNotIn("split())>14",first[names[2]])

class TitleEndpointTests(unittest.TestCase):
 setUp=batch_fixture.BatchTests.setUp
 tearDown=batch_fixture.BatchTests.tearDown
 raw=batch_fixture.BatchTests.raw
 state=batch_fixture.BatchTests.state
 post=batch_fixture.BatchTests.post
 def title_phase(self):
  self.store.update(self.id,state='WAITING_TITLE_APPROVAL',batch_review={'phase':'TITLES','ranks':['01','02','03']})
  for n in (1,2,3):self.store.item(self.id,n,status='WAITING_TITLE_APPROVAL',script_status='USER_APPROVED',batch_script_approved=True)
 def test_refresh_uses_style_and_preserves_script_other_ranks_and_confirmations(self):
  self.title_phase()
  before=copy.deepcopy(self.store.get(self.id));messages=[]
  def complete(model,sent,**kw):
   messages.append(sent);return {'data':{'titles':[{k:t[k] for k in ('text','text_zh','language')} for t in made('pt-BR')['titles']]}}
  self.adapter.complete=complete;r=self.post('content/titles')
  self.assertEqual(r.status_code,200,r.text);self.assertIn(GUIDANCE,messages[0][0]['content'])
  self.assertEqual(self.raw()['final_script'],before['items'][0]['final_script']);self.assertEqual(self.raw()['content_titles'][0]['text'],TEXTS['pt-BR'][0])
  self.assertEqual(self.store.get(self.id)['items'][1:],before['items'][1:]);self.assertEqual(self.queue.enqueued,[])
  self.assertEqual(self.raw()['script_status'],before['items'][0]['script_status'])
 def test_custom_native_title_is_not_rewritten_to_match_style(self):
  self.title_phase()
  text=TEXTS['pt-BR'][0];sent=[]
  def complete(model,messages,**kw):
   sent.append(messages);return {'data':{'title':dict(language='pt-BR',text='Olha este detalhe',text_zh=ZH)}}
  self.adapter.complete=complete;r=self.post('content/custom-title',dict(text=text))
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.raw()['content_custom_title']['text'],text);self.assertNotIn(GUIDANCE,sent[0][0]['content'])
 def test_native_manuscript_metadata_and_copy_draft_share_style(self):
  sent=[]
  def complete(model,messages,**kw):
   sent.append(messages);c=json.loads(messages[-1]['content'])
   rows=[{k:t[k] for k in ('text','text_zh','language')} for t in made('pt-BR')['titles']]
   if 'existing_zh' in c:return {'data':dict(text_zh=ZH,titles=rows)}
   return {'data':dict(detected_language='en',candidates=[dict(language='pt-BR',text=BR,text_zh=ZH)],titles=rows)}
  self.adapter.complete=complete
  self.store.item(self.id,1,title_candidates=[],copy={})
  r=self.post('script/draft',dict(mode='PASTED',text=BR,language='pt-BR'))
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['candidates'][0]['text'],BR)
  self.assertIn(GUIDANCE,sent[0][0]['content'])
  r=self.post('script/draft',dict(mode='PASTED',text='This buckle is easy to use with your clothes.',language='en'))
  self.assertEqual(r.status_code,200,r.text);self.assertIn(GUIDANCE,sent[1][0]['content']);self.assertEqual(self.queue.enqueued,[])
