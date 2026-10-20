import json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock
import autocut.context
from autocut.storage import Store
from autocut.source_analysis import resume_failed_analysis
from app.s3.headless_runtime import sha,write


class SourceResumeTests(unittest.TestCase):
 def test_initial_resume_keeps_successes_and_does_not_touch_other_jobs(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);source=root/'source.mp4';source.write_bytes(b'original')
   store=Store(root/'data');job,_=store.create(dict(request_id='analysis-resume-fixture',product_name='Fixture',market='Brazil',mode='Remix',original_input_path=str(root)))
   id=job['id'];store.update(id,state='REVIEW_REQUIRED',issues=['AI_DRIVER_EXEC_FAILED'])
   store.done(id,'VIDEO_ANALYSIS','01','completed-input',dict(verified=True))
   write(store.home/'jobs'/id/'work/manifest.json',dict(sources=[dict(rank=1,path=str(source),sha256=sha(source)),dict(rank=2,path=str(source),sha256=sha(source))]))
   other,_=store.create(dict(request_id='old-completed-fixture',product_name='Old',market='Brazil',mode='Remix'));store.update(other['id'],state='COMPLETED');before=store.get(other['id'])
   manager=Mock(store=store,lock=threading.RLock(),active={},item_pending=set())
   resume_failed_analysis(manager,id)
   manager.enqueue.assert_called_once_with(id)
   self.assertEqual(store.checkpoint(id,'VIDEO_ANALYSIS','01','completed-input'),dict(verified=True))
   self.assertEqual(store.get(other['id']),before)
   self.assertEqual(source.read_bytes(),b'original')
   store.update(id,state='REVIEW_REQUIRED',issues=['AI_DRIVER_EXEC_FAILED'])
   with self.assertRaisesRegex(ValueError,'RECOVERY_ALREADY_REQUESTED'):resume_failed_analysis(manager,id)
   store.done(id,'TTS_DONE','01','paid',dict(retained=True))
   with self.assertRaisesRegex(ValueError,'STATE_TRANSITION_REJECTED'):resume_failed_analysis(manager,id)


if __name__=='__main__':unittest.main()
