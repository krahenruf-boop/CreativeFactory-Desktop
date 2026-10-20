"""Bounded removal of baked black borders; preserve the entire inner picture."""
import copy,json,re,subprocess
from pathlib import Path
from .context import RES
from .item_review import get_item
from .production_policy import approved_content
from .script_language import script,title
from .title_layout import apply as fit_title
from app.s3.headless_runtime import read,write,sha
from app.s3.headless_media import probe
from app.s3.overlay_policy import lock_plan


def validate(segment):
    rect=segment.get('letterbox_crop')
    if rect is None:return None
    if not isinstance(rect,list) or len(rect)!=4 or any(type(n)is not int or n%2 for n in rect):raise ValueError('MONTAGE_FRAME_INVALID')
    x,y,w,h=rect;info=probe(Path(segment['path']));v=next(s for s in info['streams'] if s['codec_type']=='video')
    if x<0 or y<0 or w<2 or h<2 or x+w>v['width'] or y+h>v['height']:raise ValueError('MONTAGE_FRAME_INVALID')
    if sha(segment['path'])!=segment['sha256']:raise ValueError('MONTAGE_SOURCE_HASH_CHANGED')
    return rect


def frame_filters(segment,index,frames,width,height,number):
    rect=validate(segment)
    if rect is None:return None
    x,y,w,h=rect;tag=f'frame{number}'
    # Fit, rather than crop, the inner picture; only the background is enlarged.
    return (f'[{index}:v:0]crop={w}:{h}:{x}:{y},split=2[{tag}b][{tag}f];'
            f'[{tag}b]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},gblur=sigma=20[{tag}bg];'
            f'[{tag}f]scale={width}:{height}:force_original_aspect_ratio=decrease[{tag}fg];'
            f'[{tag}bg][{tag}fg]overlay=(W-w)/2:(H-h)/2,setpts=(PTS-STARTPTS)/{segment["speed"]},fps=30,setsar=1,'
            f'tpad=stop_mode=clone:stop_duration=0.04,trim=end_frame={frames},setpts=PTS-STARTPTS[mix{number}]')


def request(manager,id,rank,source_rank):
    store=manager.store;rank=f'{int(rank):02}'
    with store.lock,manager.lock:
        j,x=get_item(store,id,rank)
        if j.get('deleted_at') or j.get('control_request') or not approved_content(x) or (j.get('batch_review') or {}).get('phase')!='RUNNING':raise ValueError('STATE_TRANSITION_REJECTED')
        if (id,rank) in manager.item_pending or x.get('status')=='PASS':return
        if x.get('status')!='FAILED' or x.get('issues')!=['SEVERE_VISUAL_QA'] or x.get('montage_frame_recovery'):raise ValueError('STATE_TRANSITION_REJECTED')
        p=read(x['plan']);expected=None if x.get('voiceover_mode')=='MUSIC_ONLY' else script(x)['text']
        if p.get('final_script')!=expected or p['title']!=title(x)['text'] or Path(p['output']).exists():raise ValueError('APPROVED_CONTENT_CHANGED')
        segments=[s for s in p.get('montage_segments',[]) if s.get('source_rank')==source_rank]
        if not segments:raise ValueError('MONTAGE_FRAME_INVALID')
        crops=[]
        for s in segments:
            if sha(s['path'])!=s['sha256']:raise ValueError('SOURCE_HASH_CHANGED')
            r=subprocess.run([str(RES/'runtime/media/bin/ffmpeg'),'-nostdin','-v','info','-i',s['path'],'-vf','cropdetect=24:2:0','-an','-f','null','-'],capture_output=True,text=True,check=True,timeout=60)
            found=re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)',r.stderr)
            if len(found)<30 or len(set(found))!=1:raise ValueError('MONTAGE_FRAME_UNCERTAIN')
            w,h,a,b=map(int,found[0]);s['letterbox_crop']=[a,b,w,h];validate(s)
            info=probe(Path(s['path']));v=next(v for v in info['streams'] if v['codec_type']=='video')
            if a==b==0 and (w,h)==(v['width'],v['height']):raise ValueError('MONTAGE_FRAME_INVALID')
            crops.append(dict(source_rank=source_rank,source_sha256=s['sha256'],rectangle=s['letterbox_crop'],detected_frames=len(found)))
        if sha(p['audio']['audio_path'])!=p['audio']['sha256']:raise ValueError('RECOVERY_CACHE_INVALID')
        fit_title(p['visual'],p['title'],max_lines=(p.get('task_overrides') or {}).get('title_max_lines'))
        work=store.home/'jobs'/id/'work';archive=work/'failed_plans';archive.mkdir(exist_ok=True)
        old=Path(x['plan']);backup=archive/(rank+'-'+sha(old)+'.json')
        if not backup.exists():backup.write_bytes(old.read_bytes())
        lock_plan(p);write(old,p)
        store.item(id,rank,status='QUEUED',issues=[],resume_pending=True,resume_phase='AI_RENDER_QA',montage_frame_recovery=crops)
        store.update(id,state='RUNNING',stage='剪辑');manager.enqueue_batch(id,[(rank,'AI_RENDER_QA')])
