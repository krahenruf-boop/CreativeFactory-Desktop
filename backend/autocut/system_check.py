import os,subprocess,time,json
from pathlib import Path
_cached=None

def check(resources):
 global _cached
 if _cached and time.monotonic()-_cached[0]<120:return _cached[1]
 r=Path(resources)
 def run(args):
  try:return subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=60).returncode==0
  except Exception:return False
 results=dict(media=run([r/'runtime/media/bin/ffmpeg','-version']) and run([r/'runtime/media/bin/ffprobe','-version']),whisper=(r/'engine/models/ggml-base.bin').is_file() and run([r/'runtime/media/bin/whisper-cli','--help']),mps=run([r/'bin/mps-python','-c','import torch; assert torch.backends.mps.is_available(); assert torch.ones(1, device="mps").sum().item() == 1']))
 try:
  enc=subprocess.run([str(r/'runtime/media/bin/ffmpeg'),'-hide_banner','-encoders'],capture_output=True,text=True,timeout=10)
  results['videotoolbox']=enc.returncode==0 and 'h264_videotoolbox' in enc.stdout
 except Exception:results['videotoolbox']=False
 _cached=(time.monotonic(),results);return results
