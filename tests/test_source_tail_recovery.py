import copy, subprocess, tempfile, unittest
from pathlib import Path
import autocut.context
from autocut.source_tail_recovery import repaired_plan,black_intervals

class SourceTailTests(unittest.TestCase):
 def plan(self):return dict(video_speed=1.,target_duration=2.5,final_script='Unchanged approved words',title='Unchanged approved title',audio={'sha256':'already-paid'},source_frame_exceptions={'first':'HEIGHT_FIT_BLUR_EXCEPTION_V1'},montage_segments=[dict(source_start=0,source_end=2.2,output_start=0,output_end=2.2,source_duration=2.2,speed=1.,sha256='first',source_frame_layout={'foreground':[1,0,1078,1920]}),dict(source_start=0,source_end=.3,output_start=2.2,output_end=2.5,source_duration=1.,speed=1.,sha256='donor')])
 def test_only_black_tail_replaced_same_duration_words_audio_frame_unchanged(self):
  p=self.plan();before=copy.deepcopy(p);out=repaired_plan(p,[(2.,2.2)],[])
  self.assertEqual(p,before)
  for key in ['final_script','title','audio','source_frame_exceptions','target_duration']:self.assertEqual(out[key],p[key])
  first,donor=out['montage_segments'];self.assertAlmostEqual(first['source_end'],2.);self.assertAlmostEqual(donor['source_end'],.5);self.assertAlmostEqual(first['output_end'],donor['output_start']);self.assertEqual(first['source_frame_layout'],p['montage_segments'][0]['source_frame_layout'])
 def test_absent_middle_or_long_black_never_treated_as_short_tail(self):
  for intervals in [[],[(.2,.4)],[(1.,2.2)]]:
   with self.subTest(intervals=intervals),self.assertRaisesRegex(ValueError,'SOURCE_BLACK_TAIL_UNVERIFIED'):repaired_plan(self.plan(),intervals,[])
 def test_insufficient_or_black_donor_and_retimed_plan_rejected(self):
  p=self.plan();p['montage_segments'][1]['source_duration']=.4
  with self.assertRaises(ValueError):repaired_plan(p,[(2.,2.2)],[])
  with self.assertRaises(ValueError):repaired_plan(self.plan(),[(2.,2.2)],[(.4,.6)])
  p=self.plan();p['video_speed']=.9
  with self.assertRaises(ValueError):repaired_plan(p,[(2.,2.2)],[])
 def test_detector_checks_actual_source_pixels(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'tail.mp4'
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=90x160:r=30:d=2','-f','lavfi','-i','color=c=black:s=90x160:r=30:d=0.2','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','libx264','-pix_fmt','yuv420p','-y',str(p)],check=True)
   intervals=black_intervals(p);self.assertEqual(len(intervals),1);self.assertAlmostEqual(intervals[0][0],2.,places=3);self.assertGreaterEqual(intervals[0][1],2.16)

if __name__=='__main__':unittest.main()
