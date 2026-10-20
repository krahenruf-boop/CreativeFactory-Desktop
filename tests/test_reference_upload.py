"""Browser table upload regressions; isolated storage and no provider calls."""
import io,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from openpyxl import Workbook
from autocut.server import create_app
from autocut.storage import Store

TEXT='Olha como este produto facilita o seu dia.'
CSV=('文案编号,pt-BR,中文文案\nR01,'+TEXT+',看看这个产品如何让生活更轻松。\n').encode('utf-8-sig')

class ReferenceUploadTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.store=Store(Path(self.temp.name)/'data');self.manager=MagicMock();self.keys=MagicMock()
  self.keys.get.side_effect=AssertionError('Upload must not request provider credentials')
  self.app=create_app(self.store,self.keys,MagicMock(),self.manager,'test',recover_on_startup=False)
  self.client=TestClient(self.app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer test'})
  self.addCleanup(self.client.close)
 def upload(self,data=CSV,name='本地文案.csv'):
  return self.client.post('/references/upload',params={'filename':name},content=data,headers={'Content-Type':'application/octet-stream'})
 def test_csv_upload_cache_dedup_and_existing_selection_contract(self):
  r=self.upload();self.assertEqual(r.status_code,200,r.text);result=r.json();path=Path(result['path'])
  self.assertTrue(path.is_relative_to(self.store.home.resolve()));self.assertEqual(path.read_bytes(),CSV)
  self.assertEqual(self.upload().json()['path'],str(path))
  ref=self.client.post('/references/inspect',json={'path':str(path)}).json()
  row=ref['scripts']['Brazil'][0];self.assertEqual(row['text'],TEXT);self.assertTrue(row['text_zh'])
  self.assertEqual(ref['count'],1);self.assertEqual(ref['filename'],'本地文案.csv')
  self.client.post('/native/grant',json={'path':self.temp.name})
  job=self.client.post('/jobs',json=dict(request_id='upload-selection-fixture',input_path=self.temp.name,product_name='上传测试',market='Brazil',script_reference_file=str(path),script_mode='TABLE_SELECTED',script_reference_id=row['id']))
  self.assertEqual(job.status_code,200,job.text)
  saved=self.store.by_request('upload-selection-fixture')
  self.assertEqual(saved['script_snapshot']['text'],TEXT)
  self.assertTrue(saved['content_confirmation_required']);self.assertFalse(saved.get('scripts_approved'))
  self.manager.enqueue.assert_called_once() # mocked; no worker or provider runs
 def test_xlsx_uses_existing_parser(self):
  book=Workbook();book.active.append(['文案编号','pt-BR','中文文案']);book.active.append(['R02',TEXT,'中文对照'])
  data=io.BytesIO();book.save(data);book.close()
  r=self.upload(data.getvalue(),'双语文案.xlsx');self.assertEqual(r.status_code,200,r.text)
  self.assertEqual(r.json()['reference']['scripts']['Brazil'][0]['text'],TEXT)
 def test_invalid_uploads_not_admitted(self):
  for data,name,code in [(b'','empty.csv','SCRIPT_TABLE_EMPTY'),(CSV,'bad.txt','SCRIPT_TABLE_FORMAT'),(b'broken','bad.xlsx','SCRIPT_TABLE_INVALID')]:
   with self.subTest(name=name):
    r=self.upload(data,name);self.assertEqual(r.status_code,400);self.assertEqual(r.json()['error'],code)
  self.assertEqual(list((self.store.home/'imports'/'references').glob('**/*')),[])
 def test_limit_checks_actual_body(self):
  with patch('autocut.server.MAX_TABLE_BYTES',20):
   r=self.upload();self.assertEqual(r.status_code,400);self.assertEqual(r.json()['error'],'SCRIPT_TABLE_TOO_LARGE')
 def test_filename_cannot_escape_managed_cache(self):
  r=self.upload(name='../../outside.csv');self.assertEqual(r.status_code,200,r.text)
  self.assertTrue(Path(r.json()['path']).is_relative_to((self.store.home/'imports'/'references').resolve()))
  self.assertFalse((Path(self.temp.name)/'outside.csv').exists())
 def test_session_and_origin_protection(self):
  r=self.client.post('/references/upload?filename=a.csv',content=CSV,headers={'Origin':'http://evil.example'})
  self.assertEqual(r.status_code,403)
  self.client.headers.pop('Authorization');self.assertEqual(self.upload().status_code,401)

if __name__=='__main__':unittest.main()
