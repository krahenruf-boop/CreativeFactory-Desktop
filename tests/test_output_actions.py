"""Output navigation and completed-item safety; no providers or rendering."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from autocut.server import create_app
from autocut.storage import Store
from autocut.ownership import item_state


class OutputActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.store=Store(self.root/'data')
        self.job,_=self.store.create(dict(request_id='output-smoke',input_path=str(self.root),
            product_name='导航测试',product_profile_id='general',market='Brazil',mode='Transport'))
        self.id=self.job['id'];self.output=self.root/'成片 空格;$(test)'/'final.mp4'
        self.output.parent.mkdir();self.output.write_bytes(b'unchanged fixture')
        self.store.item(self.id,1,status='CHECKING',output=str(self.output))
        with patch('autocut.ownership.final_ready',return_value=True):
            self.store.item(self.id,1,status='PASS')
        self.manager=MagicMock()
        self.c=TestClient(create_app(self.store,MagicMock(),MagicMock(),self.manager,'fixture',recover_on_startup=False),
            base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer fixture'})
        self.addCleanup(self.c.close)
        self.run=patch('autocut.output_actions.subprocess.run').start();self.addCleanup(patch.stopall)
        patch('autocut.output_actions.sys.platform','darwin').start()

    def test_open_and_reveal_use_stored_output_and_preserve_job(self):
        before=self.store.get(self.id)
        for url,args in [(f'/jobs/{self.id}/outputs/open',['/usr/bin/open',str(self.output.parent)]),
                         (f'/outputs/{self.id}/01/reveal',['/usr/bin/open','-R',str(self.output)])]:
            response=self.c.post(url,json={})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.run.call_args.args[0],args)
            self.assertNotIn('shell',self.run.call_args.kwargs)
        self.assertEqual(before,self.store.get(self.id))
        self.assertEqual(self.output.read_bytes(),b'unchanged fixture')
        self.manager.enqueue.assert_not_called()

    def test_missing_output_and_bad_rank_never_open_unrelated_files(self):
        r=self.c.post(f'/outputs/{self.id}/99/reveal',json={'path':'/tmp'})
        self.assertEqual(r.json()['error'],'OUTPUT_NOT_FOUND')
        self.output.unlink()
        self.assertEqual(self.c.post(f'/jobs/{self.id}/outputs/open').json()['error'],'OUTPUT_NOT_FOUND')
        self.run.assert_not_called()

    def test_auth_and_origin_required(self):
        self.assertEqual(self.c.post(f'/jobs/{self.id}/outputs/open',headers={'Origin':'https://other.invalid'}).status_code,403)
        self.c.headers.pop('Authorization')
        self.assertEqual(self.c.post(f'/outputs/{self.id}/01/reveal').status_code,401)
        self.run.assert_not_called()

    def test_finder_error_is_specific_and_does_not_change_item(self):
        before=self.store.get(self.id)
        self.run.side_effect=subprocess.CalledProcessError(1,'open')
        r=self.c.post(f'/outputs/{self.id}/01/reveal')
        self.assertEqual(r.json()['error'],'OUTPUT_OPEN_FAILED')
        self.assertEqual(before,self.store.get(self.id))

    def test_completed_content_cannot_be_reopened_from_stale_ui(self):
        # Final validation is covered by production QA. Exercise the action gate here.
        with patch('autocut.ownership.final_ready',return_value=True):
            before=self.store.get(self.id);x=before['items'][0]
            state=item_state(self.store,before,x)
            self.assertNotIn('EDIT_CONTENT',state['allowed_actions'])
            r=self.c.post(f'/jobs/{self.id}/items/01/content/begin',json={'action_token':state['action_token']})
            self.assertNotEqual(r.status_code,200)
            self.assertEqual(before,self.store.get(self.id))
        self.manager.enqueue.assert_not_called()

    def test_new_job_defaults_to_source_subfolder(self):
        from autocut.file_storage import output_directory
        self.c.post('/native/grant',json={'path':str(self.root)})
        r=self.c.post('/jobs',json=dict(request_id='source-location-test',input_path=str(self.root),product_name='素材路径'))
        self.assertEqual(r.status_code,200,r.text)
        raw=self.store.get(r.json()['id'])
        self.assertEqual(raw['storage_policy'],'source-v1')
        self.assertEqual(output_directory(raw).parent,self.root/'outputs')
        self.assertEqual(raw['input_path'],str(self.root))


if __name__=='__main__':unittest.main()
