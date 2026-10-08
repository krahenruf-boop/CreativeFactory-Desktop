"""The renderer accepts the shared contract, never a driver name or a waived check."""
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from autocut.context import ENGINE
from app.s3.headless_media import approve_preflight,identity
from app.s3.headless_runtime import sha,write

class ContractTests(unittest.TestCase):
 def test_driver_neutral_contract_and_tamper_rejection(self):
  with tempfile.TemporaryDirectory() as d:
   h=SimpleNamespace(work=Path(d));dependency=Path(d)/'dependency';dependency.write_bytes(b'fixture')
   plan={'rank':'01','source':{'path':str(dependency)},'audio':{'audio_path':str(dependency)},'visual':{'title':{'font_file':str(dependency)},'subtitle':{'font_file':str(dependency)},'cta':{'enabled':False}}}
   folder=h.work/'preflight'/identity(plan);folder.mkdir(parents=True)
   preview=folder/'preview.mp4';preview.write_bytes(b'contract fixture only; rendering separately verified')
   write(folder/'result.json',{'output':str(preview),'output_sha256':sha(preview),'technical_status':'PASS'})
   review=dict(reviewer='AutoCut',review_contract='AUTOCUT_REVIEW_V1',status='PASS',checks={k:True for k in ['title','subtitle','cta','overlay','source_text','watermark']},preview_sha256=sha(preview))
   approve_preflight(h,plan,review)
   self.assertEqual(json.loads((folder/'approval.json').read_text())['plan_fingerprint'],identity(plan))
   with self.assertRaisesRegex(ValueError,'REAL_PREFLIGHT_VISUAL_REVIEW_REQUIRED'):approve_preflight(h,plan,dict(review,review_contract='wrong'))
   with self.assertRaisesRegex(ValueError,'REAL_PREFLIGHT_VISUAL_REVIEW_REQUIRED'):approve_preflight(h,plan,dict(review,checks=dict(review['checks'],subtitle=False)))
   preview.write_bytes(b'changed after review')
   with self.assertRaisesRegex(ValueError,'REAL_PREFLIGHT_VISUAL_REVIEW_REQUIRED'):approve_preflight(h,plan,review)

if __name__=='__main__':unittest.main()
