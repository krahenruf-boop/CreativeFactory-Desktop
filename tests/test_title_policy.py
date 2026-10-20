"""New submissions freeze the permanent policy without rewriting old jobs."""
import copy
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
import autocut.context
from autocut.storage import Store
from autocut.server import create_app
from autocut.title_policy import snapshot,POLICY
from autocut.task_instructions import effective


class TitlePolicyTests(unittest.TestCase):
    def test_new_web_job_overrides_legacy_preset_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'source.mp4').write_bytes(b'isolated source')
            store=Store(root/'data');preset=store.product('general')
            preset['task_defaults']=dict(title_max_lines=3);store.save_product(preset)
            old,_=store.create(dict(request_id='legacy-title-fixture',product_name='Legacy',market='Brazil',mode='Transport',product_profile=preset))
            store.update(old['id'],state='COMPLETED');before=store.get(old['id'])
            class Manager:
                active={};item_pending=set()
                def enqueue(self,id):pass
            client=TestClient(create_app(store,None,None,Manager(),'fixture',recover_on_startup=False),base_url='http://127.0.0.1:9999',headers={'Authorization':'Bearer fixture'})
            self.assertEqual(client.post('/native/grant',json=dict(path=str(root))).status_code,200)
            response=client.post('/jobs',json=dict(request_id='new-default-title-fixture',input_path=str(root),product_name='New product'))
            self.assertEqual(response.status_code,200,response.text)
            new=store.get(response.json()['id'])
            self.assertEqual(new['title_layout_policy'],POLICY)
            self.assertEqual(effective(new,1)['title_max_lines'],2)
            self.assertEqual(effective(new,1)['title_duration'],5.)
            self.assertTrue(new['content_confirmation_required'])
            self.assertEqual(store.get(old['id']),before)
            self.assertEqual(store.product('general')['task_defaults']['title_max_lines'],3)
            self.assertEqual(store.settings()['title_layout_policy'],POLICY)
            client.close()

    def test_item_override_cannot_restore_automatic_third_line(self):
        old=dict(resolved_overrides_json=dict(values=dict(title_max_lines=3),per_item_overrides=[dict(rank_start=1,rank_end=3,values=dict(title_max_lines=3))]))
        before=copy.deepcopy(old);saved=snapshot(old)
        self.assertEqual(old,before)
        self.assertEqual(effective(saved,2)['title_max_lines'],2)
        self.assertEqual(snapshot(saved),saved)


if __name__=='__main__':unittest.main()
