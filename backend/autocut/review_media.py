"""Job-local media intake and bounded supplement decisions; shared renderer unchanged."""
import copy,json,math,shutil,subprocess,sys,uuid
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from pydantic import Field
from .drivers import Strict
from .context import RES
from app.s3.headless_runtime import sha,read,write
from app.s3.headless_media import probe

class SupplementCut(Strict):
 supplement_source:str
 source_interval:list[float]=Field(min_length=2,max_length=2)
 target_duration:float=Field(gt=0,le=600)
 insert_position:float=Field(ge=0)
 matched_script_segment:str=Field(min_length=1,max_length=2000)
 reason:str=Field(min_length=1,max_length=600)
class SupplementPlan(Strict):
 status:Literal['MATCH','SUPPLEMENT_CONTENT_MISMATCH']
 clips:list[SupplementCut]=Field(max_length=12)
 reason:str

def inspect_media(path,kind='video'):
 p=Path(path)
 try:data=probe(p)
 except (OSError,ValueError,subprocess.SubprocessError):raise ValueError('SUPPLEMENT_MEDIA_INVALID') from None
 streams=data.get('streams',[])
 stream=next((s for s in streams if s.get('codec_type')==kind),None)
 if not stream:raise ValueError('SUPPLEMENT_MEDIA_INVALID')
 duration=float(stream.get('duration') or data.get('format',{}).get('duration') or 0)
 if not math.isfinite(duration) or not .2<duration<=600:raise ValueError('SUPPLEMENT_MEDIA_INVALID')
 fps=stream.get('avg_frame_rate','0/1').split('/');fps=float(fps[0])/max(float(fps[-1]),1)
 if kind=='video' and (fps<=0 or stream.get('width',0)<=0 or stream.get('height',0)<=0):raise ValueError('SUPPLEMENT_MEDIA_INVALID')
 return dict(path=str(p),local_path=str(p),duration=duration,fps=fps,width=stream.get('width',0),height=stream.get('height',0),sha256=sha(p))

class DownloadService:
 def download(self,url,folder):
  u=urlsplit(url)
  if u.scheme!='https' or not u.hostname or not (u.hostname=='tiktok.com' or u.hostname.endswith('.tiktok.com')) or u.username or u.password or u.port not in (None,443):raise ValueError('SUPPLEMENT_URL_UNSUPPORTED')
  folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);key=uuid.uuid4().hex
  args=[sys.executable,'-m','yt_dlp','--ignore-config','--write-info-json','--no-playlist','--no-progress','--no-warnings','--retries','0','--socket-timeout','20','--max-filesize','500M','--format','best[ext=mp4]/best','--output',str(folder/(key+'.%(ext)s')),'--print','after_move:filepath','--',url]
  try:
   result=subprocess.run(args,capture_output=True,text=True,check=True,timeout=180)
   p=Path(result.stdout.strip().splitlines()[-1]).resolve()
   if p.parent!=folder.resolve() or not p.is_file() or p.stat().st_size>500*1024**2:raise ValueError()
   return p
  except (OSError,ValueError,IndexError,subprocess.SubprocessError):raise ValueError('SUPPLEMENT_DOWNLOAD_FAILED') from None

def import_media(home,id,path,kind='video'):
 source=Path(path).resolve(strict=True)
 allowed={'.mp4','.mov','.m4v','.webm'} if kind=='video' else {'.mp3','.wav','.m4a'}
 if not source.is_file() or source.suffix.lower() not in allowed or source.stat().st_size>500*1024**2:raise ValueError('SUPPLEMENT_MEDIA_INVALID')
 before=sha(source);folder=Path(home)/'jobs'/id/'work'/('supplements' if kind=='video' else 'music');folder.mkdir(parents=True,exist_ok=True)
 # Content-addressed intake is idempotent, including retries after app restart.
 dest=folder/(before+source.suffix.lower())
 if not dest.exists():shutil.copy2(source,dest)
 result=inspect_media(dest,kind)
 if before!=result['sha256'] or sha(source)!=before:raise ValueError('SOURCE_HASH_CHANGED')
 return dict(result,id=dest.stem,source_url=None,source_file=str(source),video_id=dest.stem,download_status='READY')

def base_clips(source,decision,task):
 if source.get('remix_clips'):return copy.deepcopy(source['remix_clips'])
 ranges=decision.get('keep_ranges') if task.get('remove_redundant_sections') else None
 ranges=ranges or [[0,source['duration']]];last=0;clips=[]
 for lo,hi in ranges:
  if not all(type(n) in (int,float) and math.isfinite(n) for n in (lo,hi)) or not last<=lo<hi<=source['duration'] or hi-lo<.5:raise ValueError('CUSTOM_INSTRUCTION_RANGE_INVALID')
  clips.append(dict(path=source['path'],sha256=source['sha256'],source_start=lo,source_end=hi,source_duration=source['duration'],source_rank=source['rank'],reason='Existing source ranges'));last=hi
 return clips

def resolve_supplements(source,decision,task,plan,media,script):
 clips=base_clips(source,decision,task);total=sum(c['source_end']-c['source_start'] for c in clips)
 if not plan:return clips
 parsed=SupplementPlan.model_validate(plan)
 if parsed.status!='MATCH' or not parsed.clips:raise ValueError('SUPPLEMENT_CONTENT_MISMATCH')
 lookup={m['id']:m for m in media};seen={};inserts=[]
 for c in parsed.clips:
  m=lookup.get(c.supplement_source);lo,hi=c.source_interval
  if not m or not 0<=lo<hi<=m['duration'] or hi-lo<.5 or not math.isfinite(c.insert_position) or c.insert_position>total or c.matched_script_segment not in script:raise ValueError('SUPPLEMENT_PLAN_INVALID')
  # Never repeat a range to manufacture time. Only unretimed approved sections here.
  if abs(c.target_duration-(hi-lo))>.04 or any(lo<b and a<hi for a,b in seen.get(m['id'],[])):raise ValueError('SUPPLEMENT_PLAN_INVALID')
  if sha(m['path'])!=m['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
  seen.setdefault(m['id'],[]).append((lo,hi));inserts.append((c.insert_position,dict(path=m['path'],sha256=m['sha256'],source_start=lo,source_end=hi,source_duration=m['duration'],source_rank=m['id'],reason=c.reason)))
 result=[];cursor=0
 for clip in clips:
  end=cursor+clip['source_end']-clip['source_start'];local=cursor
  for at,extra in sorted([x for x in inserts if cursor<=x[0]<end or end==total and x[0]==end],key=lambda x:x[0]):
   if at>local:result.append(dict(clip,source_start=clip['source_start']+local-cursor,source_end=clip['source_start']+at-cursor))
   result.append(extra);local=at
  if end>local:result.append(dict(clip,source_start=clip['source_start']+local-cursor))
  cursor=end
 if any(c['source_end']-c['source_start']<.3 for c in result):raise ValueError('SUPPLEMENT_PLAN_INVALID')
 return result

def duration_check(audio_duration,clips,task=None,music_only=False):
 from .duration_fit import solve
 return solve(sum(c['source_end']-c['source_start'] for c in clips),audio_duration,task,music_only)

def music_audio(music,duration,work):
 if sha(music['path'])!=music['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
 folder=Path(work)/'music';folder.mkdir(parents=True,exist_ok=True);out=folder/(music['sha256'][:16]+'-'+str(round(duration*30))+'.wav')
 if not out.exists():subprocess.run(['ffmpeg','-nostdin','-v','error','-stream_loop','-1','-i',music['path'],'-vn','-af',f'afade=t=out:st={max(0,duration-.75)}:d=0.75','-t',str(duration),'-ar','44100','-ac','2','-y',str(out)],capture_output=True,check=True,timeout=120)
 return dict(path=str(out),duration=duration,sha256=sha(out),technical_status='PASS',voiceover_mode='MUSIC_ONLY')
