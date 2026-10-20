"""Native Mac folder chooser; process mocked except for the manual UI smoke."""
import json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from autocut.media_selection import pick_folder,_picker_lock,_FOLDER_PANEL_SCRIPT
from autocut.server import create_app
from autocut.storage import Store


class NativeFolderPickerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name).resolve();self.media=self.root/'素材 空格';self.media.mkdir()
  (self.media/'test.mp4').write_bytes(b'folder selection fixture; never rendered')
  self.run=patch('autocut.media_selection.subprocess.run').start();self.addCleanup(patch.stopall)
  patch('autocut.media_selection.sys.platform','darwin').start()
  self.run.return_value=MagicMock(returncode=0,stdout=json.dumps({'path':str(self.media)}))
 def test_system_chooser_returns_validated_path_and_no_shell_interpolation(self):
  result=pick_folder(str(self.media));self.assertEqual(result['video_count'],1);self.assertEqual(result['path'],str(self.media))
  args=self.run.call_args.args[0];self.assertEqual(args[0],'/usr/bin/osascript');self.assertEqual(args[-1],str(self.media));self.assertNotIn('shell',self.run.call_args.kwargs)
 def test_cancel_returns_without_a_selection(self):
  self.run.return_value.stdout='{"cancelled":true}'
  self.assertEqual(pick_folder(),{'cancelled':True})
 def test_duplicate_request_does_not_open_another_window(self):
  _picker_lock.acquire()
  try:
   with self.assertRaisesRegex(ValueError,'SOURCE_PICKER_BUSY'):pick_folder()
   self.run.assert_not_called()
  finally:_picker_lock.release()
 def test_failure_and_timeout_release_picker_for_retry(self):
  self.run.side_effect=subprocess.TimeoutExpired('osascript',300)
  with self.assertRaisesRegex(ValueError,'SOURCE_PICKER_TIMEOUT'):pick_folder()
  self.run.side_effect=None;self.run.return_value.returncode=1
  with self.assertRaisesRegex(ValueError,'SOURCE_PICKER_UNAVAILABLE'):pick_folder()
  self.run.return_value.returncode=0;self.run.return_value.stdout='{"cancelled":true}'
  self.assertTrue(pick_folder()['cancelled'])
 def test_route_requires_auth_and_grants_only_completed_selection(self):
  store=Store(self.root/'data');manager=MagicMock()
  app=create_app(store,MagicMock(),MagicMock(),manager,'test',recover_on_startup=False)
  with TestClient(app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer test'}) as c:
   self.run.return_value.stdout='{"cancelled":true}'
   self.assertTrue(c.post('/media/pick-folder',json={}).json()['cancelled'])
   spec=dict(request_id='native-folder-fixture',input_path=str(self.media),product_name='选择测试')
   self.assertEqual(c.post('/jobs',json=spec).status_code,403)
   self.run.return_value.stdout=json.dumps({'path':str(self.media)})
   r=c.post('/media/pick-folder',json={'initial_path':str(self.media)})
   self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['video_count'],1)
   self.assertEqual(c.post('/jobs',json=spec).status_code,200)
   manager.enqueue.assert_called_once() # mocked; never starts processing
   calls=self.run.call_count;c.headers.pop('Authorization')
   self.assertEqual(c.post('/media/pick-folder',json={}).status_code,401);self.assertEqual(self.run.call_count,calls)

class NativeBridgeRegression(unittest.TestCase):
 @unittest.skipUnless(sys.platform=='darwin','macOS JXA bridge')
 def test_actual_nsmodal_response_and_cancel_are_distinguished(self):
  # Execute the production result handler with real bridged AppKit constants.
  # Override only the entry point so this regression opens no UI.
  script=_FOLDER_PANEL_SCRIPT+'''
function run() {
 const panel={URL:{path:$('/tmp/folder-selection-fixture')}};
 return JSON.stringify([$.NSModalResponseOK,'1',$.NSModalResponseCancel,'0'].map(function(code){return JSON.parse(folderSelectionResult(code,panel));}));
}
'''
  response=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',script],capture_output=True,text=True,check=True,timeout=10)
  rows=json.loads(response.stdout)
  self.assertEqual(rows,[{'path':'/tmp/folder-selection-fixture'}]*2+[{'cancelled':True}]*2)

if __name__=='__main__':unittest.main()
