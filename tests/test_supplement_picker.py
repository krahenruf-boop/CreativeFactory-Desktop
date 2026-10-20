"""Supplement chooser only: no provider, render, or real job mutation."""
import json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from autocut.media_selection import pick_videos,select_videos,_picker_lock,_VIDEO_PANEL_SCRIPT
from autocut.server import create_app
from autocut.storage import Store

class SupplementPickerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
  self.videos=[self.root/'补充 空格.mp4',self.root/'second.MOV']
  for p in self.videos:p.write_bytes(b'isolated chooser fixture, not a playable video')
 def test_native_multiple_selection_and_cancel(self):
  before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in self.videos]
  with patch('autocut.media_selection.sys.platform','darwin'),patch('autocut.media_selection.subprocess.run') as run:
   run.return_value=MagicMock(returncode=0,stdout=json.dumps({'paths':[str(p) for p in self.videos]*2}))
   result=pick_videos(str(self.videos[0]));self.assertEqual(result['paths'],[str(p) for p in self.videos])
   self.assertEqual(run.call_args.args[0][-1],str(self.root));self.assertNotIn('shell',run.call_args.kwargs)
   run.return_value.stdout='{"cancelled":true}';self.assertEqual(pick_videos(),{'cancelled':True})
  self.assertEqual(before,[(p.read_bytes(),p.stat().st_mtime_ns) for p in self.videos])
 def test_invalid_files_and_limits(self):
  invalid=self.root/'text.txt';invalid.write_text('x');empty=self.root/'empty.mp4';empty.touch()
  for paths,code in [([], 'SUPPLEMENT_FILE_SELECTION_INVALID'),([str(self.root)],'SUPPLEMENT_FILE_SELECTION_INVALID'),([str(invalid)],'SUPPLEMENT_FILE_SELECTION_INVALID'),([str(empty)],'SUPPLEMENT_FILE_SIZE'),([str(self.root/'missing.mp4')],'SUPPLEMENT_FILE_UNREADABLE'),([str(self.root/f'{i}.mp4') for i in range(6)],'SUPPLEMENT_FILE_LIMIT')]:
   with self.subTest(paths=paths),self.assertRaisesRegex(ValueError,code):select_videos(paths)
 def test_busy_and_timeout_do_not_leave_picker_locked(self):
  with patch('autocut.media_selection.sys.platform','darwin'),patch('autocut.media_selection.subprocess.run') as run:
   _picker_lock.acquire()
   try:
    with self.assertRaisesRegex(ValueError,'SOURCE_PICKER_BUSY'):pick_videos()
    run.assert_not_called()
   finally:_picker_lock.release()
   run.side_effect=subprocess.TimeoutExpired('osascript',300)
   with self.assertRaisesRegex(ValueError,'SOURCE_PICKER_TIMEOUT'):pick_videos()
   self.assertFalse(_picker_lock.locked())
 def test_route_grants_paths_without_running_workers(self):
  store=Store(self.root/'data');manager=MagicMock();app=create_app(store,MagicMock(),MagicMock(),manager,'test',recover_on_startup=False)
  j,_=store.create(dict(request_id='supplement-picker-test',input_path=str(self.root),product_name='fixture',product_profile_id='general',market='Brazil',mode='Transport'))
  before=store.get(j['id'])
  with TestClient(app,base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer test'}) as c,patch('autocut.server.require_action'),patch.object(app.state.review,'add_media',return_value={'status':'fixture-only'}) as add,patch('autocut.server.pick_videos') as picker:
   picker.return_value={'cancelled':True};self.assertTrue(c.post('/media/pick-videos',json={}).json()['cancelled'])
   body={'paths':[str(self.videos[0])],'urls':[],'action_token':'fixture'};url=f"/jobs/{j['id']}/items/01/supplements"
   self.assertEqual(c.post(url,json=body).status_code,403);add.assert_not_called()
   picker.return_value=select_videos(body['paths']);self.assertEqual(c.post('/media/pick-videos',json={}).json()['paths'],body['paths'])
   self.assertEqual(c.post(url,json=body).status_code,200);add.assert_called_once_with(j['id'],'01',body['paths'],[],'fixture')
   self.assertEqual(c.post('/media/select-videos',json={'paths':[str(self.videos[1])]}).status_code,200)
   c.headers.pop('Authorization');n=picker.call_count
   self.assertEqual(c.post('/media/pick-videos',json={}).status_code,401);self.assertEqual(picker.call_count,n)
   self.assertEqual(c.post('/media/select-videos',json={'paths':[str(self.videos[0])]}).status_code,401)
  self.assertEqual(store.get(j['id']),before);manager.enqueue.assert_not_called()
 @unittest.skipUnless(sys.platform=='darwin','requires AppKit')
 def test_actual_jxa_bridge_urls_and_cancel(self):
  script=_VIDEO_PANEL_SCRIPT+'''
function run(){
 const panel=$.NSOpenPanel.openPanel;
 panel.setCanChooseFiles(true);panel.setCanChooseDirectories(false);panel.setAllowsMultipleSelection(true);
 panel.setAllowedFileTypes(['mp4','mov','m4v','webm']);
 const resultPanel={URLs:$([$.NSURL.fileURLWithPath('/tmp/视频 one.mp4'),$.NSURL.fileURLWithPath('/tmp/two.mov')])};
 return JSON.stringify([videoSelectionResult('1',resultPanel),videoSelectionResult('0',resultPanel)]);
}
'''
  r=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',script],capture_output=True,text=True,timeout=10,check=True)
  selected,cancelled=map(json.loads,json.loads(r.stdout));self.assertEqual(selected['paths'],['/tmp/视频 one.mp4','/tmp/two.mov']);self.assertEqual(cancelled,{'cancelled':True})

if __name__=='__main__':unittest.main()
