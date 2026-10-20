"""Local browser folder selection; no video processing or provider calls."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from autocut.server import create_app
from autocut.storage import Store
from autocut.media_selection import folder_info


class MediaSelectionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name).resolve();self.media=self.root/'素材 空格';self.media.mkdir()
  for name in ['one.mp4','two.MOV','.hidden.mp4','notes.txt']:(self.media/name).write_bytes(b'fixture only; never rendered')
  (self.media/'子文件夹').mkdir();(self.media/'子文件夹'/'nested.mp4').write_bytes(b'fixture')
  self.store=Store(self.root/'data');self.manager=MagicMock();keys=MagicMock()
  keys.get.side_effect=AssertionError('Folder selection must not call providers')
  self.app=create_app(self.store,keys,MagicMock(),self.manager,'test',recover_on_startup=False)
  self.client=TestClient(self.app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer test'})
  self.addCleanup(self.client.close)
 def job(self):
  return self.client.post('/jobs',json=dict(request_id='folder-selection-fixture',input_path=str(self.media),product_name='选择测试',market='Brazil'))
 def test_browse_only_counts_direct_supported_videos_and_does_not_grant(self):
  row=folder_info(self.media);self.assertEqual(row['video_count'],2);self.assertEqual(row['parent'],str(self.root));self.assertEqual([d['name'] for d in row['directories']],['子文件夹'])
  self.assertEqual(self.job().status_code,403);self.manager.enqueue.assert_not_called()
 def test_selection_grants_existing_creation_contract_without_modifying_sources(self):
  before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.media.iterdir() if p.is_file()}
  r=self.client.post('/media/select-folder',json={'path':str(self.media)})
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['video_count'],2)
  job=self.job();self.assertEqual(job.status_code,200,job.text)
  self.assertEqual(self.store.by_request('folder-selection-fixture')['input_path'],str(self.media))
  self.manager.enqueue.assert_called_once() # mocked; no actual pipeline
  after={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.media.iterdir() if p.is_file()}
  self.assertEqual(before,after)
 def test_clear_errors_for_missing_empty_and_non_directory(self):
  empty=self.root/'empty';empty.mkdir()
  for path,code in [('', 'SOURCE_FOLDER_REQUIRED'),(str(self.root/'missing'),'SOURCE_FOLDER_MISSING'),(str(empty),'NO_VALID_SOURCE'),(str(self.media/'one.mp4'),'SOURCE_NOT_FOLDER')]:
   with self.subTest(path=path):
    r=self.client.post('/media/select-folder',json={'path':path});self.assertEqual(r.status_code,400);self.assertEqual(r.json()['error'],code)
  self.manager.enqueue.assert_not_called()
 def test_permission_failure_is_explained(self):
  with patch.object(Path,'iterdir',side_effect=PermissionError()):
   with self.assertRaisesRegex(ValueError,'SOURCE_FOLDER_PERMISSION'):folder_info(self.media)
 def test_default_directory_and_session_protection(self):
  with patch('autocut.media_selection.storage_root',return_value=self.root):
   (self.root/'Media').mkdir();self.assertEqual(folder_info()['path'],str(self.root/'Media'))
  self.assertEqual(self.client.post('/media/pick-folder',json={},headers={'Origin':'http://other.example'}).status_code,403)
  self.client.headers.pop('Authorization');self.assertEqual(self.client.post('/media/pick-folder',json={}).status_code,401)
  self.assertEqual(self.client.post('/media/select-folder',json={'path':str(self.media)}).status_code,401)

if __name__=='__main__':unittest.main()
