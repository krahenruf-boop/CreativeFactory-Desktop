"""Focused read-only Feishu adapter/API regressions. Never calls paid services."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock
import httpx
from fastapi.testclient import TestClient
from autocut.feishu_copy import *
from autocut.storage import Store
from autocut.server import create_app
from autocut.workbench import script_snapshot

URL='https://my.feishu.cn/wiki/TestNode123?sheet=abc123'
HEAD=['类','中文文案','国家','类型','文案','文案编号','标题编号','音乐编号','出单数']
BR='Você não precisa mais se preocupar com isso. Olha como esta fivela combina com a sua roupa.'
MX='Mira esta correa para tu maleta. Puedes llevar tus bolsas con una mano.'
class Keys:
 def __init__(self):self.data={'FEISHU_APP_ID':'cli_fixture','FEISHU_APP_SECRET':'fixture-secret'}
 def get(self,k):return self.data.get(k,'')
 def configured(self,k):return bool(self.get(k))
 def set(self,k,v):self.data[k]=v
 def delete(self,k):self.data.pop(k,None)
class Tests(unittest.TestCase):
 def test_read_pages_normalize_cache_and_reference(self):
  with tempfile.TemporaryDirectory() as temp:
   calls=[];bad=[False]
   sheets=[dict(sheet_id='abc123',title='MX_行李箱绑带',grid_properties=dict(row_count=502,column_count=9)),dict(sheet_id='br123',title='BR_H标腰带扣',grid_properties=dict(row_count=4,column_count=9))]
   def respond(req):
    calls.append((req.method,req.url.path));p=req.url.path
    if p.endswith('/auth/v3/tenant_access_token/internal'):return httpx.Response(200,json={'code':0,'tenant_access_token':'test-only','expire':7200})
    self.assertEqual(req.method,'GET');self.assertEqual(req.headers['Authorization'],'Bearer test-only')
    if p.endswith('get_node'):data={'node':{'obj_type':'sheet','obj_token':'ActualSheetToken','title':'fixture'}}
    elif p.endswith('/sheets/query'):data={'sheets':sheets}
    else:
     self.assertIn('/ActualSheetToken/values/',p)
     if bad[0]:return httpx.Response(403,json={'code':99991672})
     if 'abc123!A501:' in p:v=[['文案','行李箱绑带','巴西葡萄牙语','软广',MX,'W2']]
     elif 'abc123!' in p:v=[HEAD,['文案','行李箱绑带','巴西葡萄牙语','软广',MX,'W1']]
     else:v=[HEAD,['文案','带扣文案','巴西葡萄牙语','硬广',BR,'B1'],['标题','标题','','','这不是口播'],[]]
     data={'valueRange':{'values':v}}
    return httpx.Response(200,json={'code':0,'data':data})
   source=FeishuCopySource(temp,Keys(),FeishuAPI(Keys(),httpx.MockTransport(respond)))
   source.bind('test',URL);r=source.connect(True)
   self.assertEqual((r['sheet_count'],r['copy_count'],r['br_count'],r['mx_count']),(2,3,1,1))
   cached=source.cache();mx=cached['sheets'][0]['rows'];self.assertEqual([r['row_number'] for r in mx],[2,501]);self.assertEqual(mx[0]['language'],'es-MX');self.assertIn('SOURCE_COUNTRY_LANGUAGE_CONFLICT',mx[0]['warnings'])
   ids=[r['id'] for s in cached['sheets'] for r in s['rows']];source.connect(True)
   self.assertEqual(ids,[r['id'] for s in source.cache()['sheets'] for r in s['rows']])
   self.assertEqual(len([c for c in calls if c[0]=='POST']),1)
   before=json.dumps(source.cache(),sort_keys=True);bad[0]=True
   with self.assertRaises(ValueError):source.connect(True)
   self.assertEqual(before,json.dumps(source.cache(),sort_keys=True))
   reopened=FeishuCopySource(temp,Keys());self.assertEqual(reopened.status()['copy_count'],3)
   self.assertEqual(reopened.matches('H标腰带扣','Brazil')[0]['sheet_id'],'br123')
   with self.assertRaisesRegex(ValueError,'FEISHU_MARKET_MISMATCH'):reopened.reference(cached['source_id'],'br123','Mexico')
   ref=reopened.reference(cached['source_id'],'br123','Brazil');row=ref['script_rows'][0]
   store=Store(Path(temp)/'data');spec=script_snapshot(dict(script_mode='TABLE_SELECTED',reference=ref,market='Brazil',script_reference_id=row['id']),store)
   self.assertEqual(spec['script_snapshot']['text'],BR);self.assertEqual(spec['script_snapshot']['text_zh'],'带扣文案')
   reopened.unbind();self.assertIsNone(reopened.status()['binding']);self.assertTrue((reopened.root/(cached['source_id']+'.json')).exists())
 def test_real_header_variants_and_non_copy_rows(self):
  sheet={'title':'BR_结构样例','sheet_id':'x'}
  rows=[['常规结构','专属结构','类型','源指令'],['文案','这是完整中文口播。','软广',''],['','镜头结构','软广','拍摄指令']]
  n=normalize('s',sheet,rows,1)
  self.assertEqual(n['header_row'],1);self.assertEqual(len(n['rows']),1);self.assertEqual(n['rows'][0]['text_zh'],'这是完整中文口播。')
  n=normalize('s',sheet,[['文案','这是正文。'*30,'软广']],1)
  self.assertEqual(n['rows'],[])
  n=normalize('s',sheet,[['Ai资产表'],['即梦指令','国家','类型'],['拍摄指令','巴西','开头']],1)
  self.assertEqual(n['warnings'],['NON_COPY_SHEET']);self.assertEqual(n['rows'],[])
  n=normalize('s',{'title':'YN_产品','sheet_id':'x'},[['中文文案','印尼文案','出单数'],['原始中文','Produk ini sangat mudah digunakan.','10']],1)
  self.assertEqual(n['rows'][0]['original_text'],'Produk ini sangat mudah digunakan.');self.assertIsNone(n['market']);self.assertEqual(n['rows'][0]['language'],'id')
 def test_link_type_and_auth_safety(self):
  self.assertIsNone(parse_link(URL)['spreadsheet_token'])
  self.assertEqual(parse_link('https://x.feishu.cn/sheets/Real?sheet=x')['spreadsheet_token'],'Real')
  for url in ['http://my.feishu.cn/wiki/x','https://my.feishu.cn.evil.test/wiki/x','https://u:p@my.feishu.cn/wiki/x','file:///tmp/a','https://my.feishu.cn:99/wiki/x']:
   with self.assertRaises(ValueError):parse_link(url)
  api=FeishuAPI(Keys(),httpx.MockTransport(lambda r:httpx.Response(200,json={'code':0,'data':{'node':{'obj_type':'bitable'}}})))
  api.token='t';api.expires=time.time()+100
  with self.assertRaisesRegex(ValueError,'FEISHU_UNSUPPORTED_TYPE'):api.resolve(parse_link(URL))
  row=normalize('s',{'title':'未知分类','sheet_id':'x'},[['其它列'],['some random text']],1)
  self.assertEqual(row['rows'],[]);self.assertIsNone(row['market'])
 def test_authenticated_routes_and_job_snapshot(self):
  with tempfile.TemporaryDirectory() as temp:
   store=Store(Path(temp)/'data');manager=MagicMock();keys=Keys()
   app=create_app(store,keys,MagicMock(),manager,'test',recover_on_startup=False)
   c=TestClient(app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer test'})
   self.assertEqual(c.post('/copy-sources/feishu/bind',json={'name':'test','url':URL}).status_code,200)
   source=app.state.feishu;source_id=source.binding()['source_id']
   normalized=normalize(source_id,{'title':'BR_H标腰带扣','sheet_id':'br123'},[HEAD,['文案','带扣文案','巴西葡萄牙语','软广',BR,'W1']],time.time())
   source.write(source_id+'.json',dict(source_id=source_id,last_synced_at=time.time(),sheets=[normalized]))
   ref=c.get('/copy-sources/feishu/reference',params={'source_id':source_id,'sheet_id':'br123'}).json()
   self.assertEqual(ref['scripts']['Brazil'][0]['language'],'pt-BR')
   c.post('/native/grant',json={'path':temp})
   response=c.post('/jobs',json=dict(request_id='feishu-selection-fixture',input_path=temp,product_name='H标腰带扣',market='Brazil',feishu_source_id=source_id,feishu_sheet_id='br123',script_mode='TABLE_SELECTED',script_reference_id=ref['script_rows'][0]['id']))
   self.assertEqual(response.status_code,200,response.text)
   job=store.by_request('feishu-selection-fixture');self.assertEqual(job['script_snapshot']['text'],BR);self.assertTrue(job['batch_review_required']);self.assertTrue(job['content_confirmation_required']);self.assertFalse(job.get('scripts_approved'))
   self.assertEqual(len(job['reference']['script_rows']),1)
   manager.enqueue.assert_called_once() # mocked enqueue, no pipeline execution
   c.headers.pop('Authorization');self.assertEqual(c.get('/copy-sources/feishu').status_code,401)

if __name__=='__main__':unittest.main()
