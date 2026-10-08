import json,tempfile,unittest
from pathlib import Path
from fastapi.testclient import TestClient
from autocut.server import create_app
from autocut.storage import Store
from autocut.references import parse
class Keys:
 def __init__(self):self.values={}
 def configured(self,k):return bool(self.values.get(k))
 def set(self,k,v):self.values[k]=v
 def delete(self,k):self.values.pop(k,None)
class Router:
 def health(self):return {}
class Manager:
 def __init__(self):self.started=[]
 def enqueue(self,id):self.started.append(id)
 def stop(self,id):pass
class APITests(unittest.TestCase):
 def test_origin_idempotency_grant_and_key_redaction(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'data');keys=Keys();mgr=Manager();app=create_app(s,keys,Router(),mgr,'test-local-session')
   c=TestClient(app,base_url='http://127.0.0.1:9000',headers={'Authorization':'Bearer test-local-session'})
   self.assertEqual(c.get('/jobs',headers={'Origin':'https://example.org'}).status_code,403)
   self.assertEqual(TestClient(app,base_url='http://127.0.0.1:9000').get('/jobs').status_code,401)
   r={'request_id':'same-request-1234','input_path':d,'product_profile_id':'belt'}
   self.assertEqual(c.post('/jobs',json=r).status_code,403)
   self.assertEqual(c.post('/native/grant',json={'path':d}).status_code,200)
   a=c.post('/jobs',json=r).json();b=c.post('/jobs',json=r).json();self.assertEqual(a['id'],b['id']);self.assertEqual(len(mgr.started),1)
   res=c.post('/settings/providers/openai',json={'key':'test-only-secret-value'});self.assertEqual(res.json(),{'configured':True});self.assertNotIn('test-only-secret-value',res.text)
   self.assertNotIn('test-only-secret-value',s.path.read_bytes().decode('latin1'))
   folder=Path(d)/'outputs';folder.mkdir();out=folder/'test.mp4';out.write_bytes(b'explicit mock output')
   s.item(a['id'],1,status='PASS',output=str(out));s.update(a['id'],state='COMPLETED')
   events=c.post('/runtime/completions',json={}).json()['events']
   self.assertEqual(events,[dict(job_id=a['id'],output_dir=str(folder.resolve()),open_output=True)])
   self.assertEqual(c.post('/runtime/completions',json={}).json()['events'],[])
 def test_real_csv_xlsx_and_invalid(self):
  from openpyxl import Workbook
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'library.csv';p.write_text('编号,pt-BR,备注\nW1,Olha isso,teste\n');r=parse(p);self.assertEqual(r['headers'],['编号','pt-BR','备注']);self.assertEqual(r['count'],1)
   x=Path(d)/'library.xlsx';w=Workbook();w.active.append(['编号','es-MX']);w.active.append(['W2','Mira esto']);w.save(x);self.assertEqual(parse(x)['rows'][0]['es-MX'],'Mira esto')
   p=Path(d)/'bad.xls';p.write_text('not an xls file')
   with self.assertRaisesRegex(ValueError,'SCRIPT_TABLE_INVALID'):parse(p)
if __name__=='__main__':unittest.main()
