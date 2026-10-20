"""Mandatory CTA contract; final-pixel verification and overlay-only repair.

Only a current-job user override may disable the arrow. Driver suggestions,
legacy `omit_added_cta`, presets and empty instructions cannot opt out.
"""
import copy,json,math,subprocess
from pathlib import Path
from PIL import Image,ImageChops,ImageFilter
from .context import RES,HOME,asset
from app.s3.headless_runtime import sha,write
from app.s3.visual_preset import load_preset,normalize_cta

VERSION='CTA_REQUIRED_V1'
MATCHER_VERSION='CTA_PIXELS_V3_LEFT_ONLY'
POSITION_POLICY='CTA_LEFT_ONLY_V1'
# Measured boundary correction for the frozen artwork (nominal centre 15%).
LEFT_ANCHOR=.166


def required(plan):
    return (plan.get('task_overrides') or {}).get('cta_enabled') is not False


def ensure_plan(plan):
    """Repair missing/stale CTA fields before locking; never touch other layers."""
    visual=plan['visual'];preset=load_preset()['cta'];old=visual.get('cta') or {}
    enabled=required(plan);c=copy.deepcopy(preset);c['asset']=old.get('asset') or (asset() if enabled else {})
    # Preserve the frozen artwork's measured boundary correction. The red body
    # remains 16% wide; its side flashes are not mistaken for the arrow body.
    # Global user policy outranks stale model decisions, presets and old jobs.
    # Collisions never authorize a horizontal move; captions stay above CTA.
    pos=preset.get('asset_layout_override',{}).get('chosen_position',[LEFT_ANCHOR,.65])
    c.update(anchor_x=LEFT_ANCHOR,anchor_y=pos[1],
        start=max(0,plan['target_duration']-5),end=plan['target_duration'],enabled=enabled)
    visual['cta']=c
    plan.pop('cta_retained_evidence',None)
    visual['overrides']=[o for o in visual.get('overrides',[]) if o['layer']!='cta']
    visual['overrides'].append(dict(layer='cta',override_reason='CTA_SUBTITLE_COLLISION',
        default_position=[preset['anchor_x'],preset['anchor_y']],chosen_position=[c['anchor_x'],c['anchor_y']],detail='Fixed left side; existing frozen artwork boundary correction'))
    visual['visual_preset_override']=True
    revision=visual.setdefault('user_revision',{})
    revision['omit_added_cta']=not enabled
    if revision.get('reason')=='Task instruction or existing source arrow; source pixels preserved':revision.pop('reason')
    plan['cta_policy']=dict(version=VERSION,required=enabled,layer_order='BELOW_TEXT',subtitle_overlap='ALLOWED',origin='USER_EXPLICIT_OFF' if not enabled else 'USER_EXPLICIT_ON' if plan.get('task_overrides',{}).get('cta_enabled') is True else 'DEFAULT_ON')
    plan['cta_policy']['position_policy']=POSITION_POLICY
    plan.setdefault('resolved_parameters',{})['cta_enabled']=dict(value=enabled,source=plan['cta_policy']['origin'])
    plan['resolved_parameters']['cta_side']=dict(value='left',source=POSITION_POLICY)
    if enabled:
        path=Path(c['asset']['file_path'])
        if not path.is_file() or sha(path)!=c['asset']['sha256']:raise ValueError('CTA_ASSET_UNAVAILABLE')
    return plan['cta_policy']


def check_plan(plan):
    if not required(plan):return
    c=plan.get('visual',{}).get('cta') or {}
    if (abs(c.get('anchor_x',-1)-LEFT_ANCHOR)>.0001 or c.get('horizontal_motion') is not False):
        raise ValueError('CTA_LEFT_REQUIRED')
    proof=plan.get('cta_retained_evidence') or {}
    retained=(proof.get('status')=='PASS' and proof.get('source_sha256')==plan['source']['sha256']
        and proof.get('anchor')==LEFT_ANCHOR and proof.get('right_matched_frames')==0
        and proof.get('matcher')==MATCHER_VERSION
        and proof.get('timeline')==source_timeline(plan))
    if c.get('enabled') is not True and not retained:raise ValueError('CTA_PLAN_REQUIRED')
    if not c.get('asset') or not Path(c['asset']['file_path']).is_file():raise ValueError('CTA_ASSET_UNAVAILABLE')
    if sha(c['asset']['file_path'])!=c['asset']['sha256']:raise ValueError('CTA_ASSET_UNAVAILABLE')
    if abs(c.get('start',-1)-max(0,plan['target_duration']-5))>.001 or abs(c.get('end',-1)-plan['target_duration'])>.001:raise ValueError('CTA_TIME_INVALID')
    for layer in plan.get('cta_preserve_text_layers',[]):
        if sha(layer['layer'])!=layer['layer_sha256']:raise ValueError('CTA_EXISTING_TEXT_CHANGED')


def source_timeline(plan):
    return {k:plan.get(k) for k in ('source_ranges','montage_segments','video_speed','target_duration')}


def retain_source_arrow(plan,folder):
    """A source detection is only a hint. Inspect the actual kept final window."""
    if not required(plan) or not plan.get('source_arrow_hint'):return False
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    start=max(0,plan['target_duration']-5);segments=[]
    if plan.get('montage_segments'):
        for s in plan['montage_segments']:
            lo,hi=max(start,s['output_start']),min(plan['target_duration'],s['output_end'])
            if lo<hi:segments.append((s['path'],s['source_start']+(lo-s['output_start'])*s['speed'],(hi-lo)*s['speed'],s['speed']))
    else:
        cursor=0.;speed=plan['video_speed']
        for a,b in plan.get('source_ranges',[[0,plan['source']['source_end']]]):
            end=cursor+(b-a)/speed;lo,hi=max(start,cursor),min(end,plan['target_duration'])
            if lo<hi:segments.append((plan['source']['path'],a+(lo-cursor)*speed,(hi-lo)*speed,speed))
            cursor=end
    if not segments:return False
    cmd=[str(RES/'runtime/media/bin/ffmpeg'),'-v','error','-nostdin','-y'];graph=[]
    for i,(path,lo,duration,speed) in enumerate(segments):
        cmd+=['-ss',str(lo),'-t',str(duration),'-i',path]
        graph.append(f'[{i}:v]setpts=(PTS-STARTPTS)/{speed},fps=30,scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1[v{i}]')
    graph.append(''.join(f'[v{i}]' for i in range(len(segments)))+f'concat=n={len(segments)}:v=1:a=0[out]')
    tail=folder/'source-tail.mp4';cmd+=['-filter_complex_threads','2','-filter_complex',';'.join(graph),'-map','[out]','-an','-c:v','libx264','-threads','2','-crf','18','-t',str(plan['target_duration']-start),str(tail)]
    subprocess.run(cmd,check=True,capture_output=True,timeout=180)
    proof=verify(plan,tail,folder/'pixels')
    if proof['status']!='PASS':return False
    plan['cta_retained_evidence']=dict(proof,source_sha256=plan['source']['sha256'],timeline=source_timeline(plan))
    plan['visual']['cta']['enabled']=False
    plan['visual']['user_revision']['omit_added_cta']=True
    plan['cta_policy']['origin']='VERIFIED_SOURCE_ARROW_IN_FINAL_WINDOW'
    return True


def sprite(plan,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);c=plan['visual']['cta'];w,h=plan['visual']['canvas']
    s=normalize_cta(c['asset'],HOME/'user_assets',round(w*c['width_ratio']),folder/'cta.png',c.get('width_basis','visible_artwork'))
    return dict(s,x=w*c['anchor_x']-s['width']/2,base_y=h*c['anchor_y']-s['height']/2,
        amplitude_px=h*c['bounce_amplitude_ratio'],period=c['bounce_period_seconds'],start=c['start'],end=c['end'])


def red_mask(image):
    r,g,b=image.convert('RGB').split()
    return ImageChops.multiply(ImageChops.multiply(r.point(lambda v:255 if v>140 else 0),g.point(lambda v:255 if v<115 else 0)),b.point(lambda v:255 if v<115 else 0))


def white_mask(image):
    r,g,b=image.convert('RGB').split()
    return ImageChops.darker(ImageChops.darker(r,g),b).point(lambda v:255 if v>180 else 0)


def artwork_score(crop,template,outline):
    """Foreground plus the arrow's white contour; skin outside it is irrelevant."""
    actual=red_mask(crop);inter=ImageChops.multiply(actual,template).histogram()[255]
    area=template.histogram()[255];union=area+actual.histogram()[255]-inter
    red=inter/max(1,area)
    white=ImageChops.multiply(white_mask(crop),outline).histogram()[255]/max(1,outline.histogram()[255])
    return inter/max(1,union),red,white


def verify(plan,video,folder):
    """Decode every final-five-second MP4 frame, match the actual arrow artwork.

    A red shirt is not enough: arrow foreground, white outline and vertical movement must
    agree. This result is bound to output bytes, never a config-only CTA PASS.
    """
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    if not required(plan):
        result=dict(status='NOT_REQUIRED',reason='USER_EXPLICIT_OFF',output_sha256=sha(video));write(folder/'cta-qa.json',result);return result
    check_plan(plan)
    s=sprite(plan,folder);info=json.loads(subprocess.check_output([str(RES/'runtime/media/bin/ffprobe'),'-v','error','-show_streams','-of','json',str(video)]))
    v=next(x for x in info['streams'] if x['codec_type']=='video');duration=float(v['duration']);start=max(0,duration-5)
    w=540;h=round(int(v['height'])*w/int(v['width']));scale=w/plan['visual']['canvas'][0]
    with Image.open(s['path']) as im:
        reference=im.convert('RGBA').resize((round(s['width']*scale),round(s['height']*scale)))
    template=red_mask(reference)
    outline=ImageChops.multiply(white_mask(reference),template.filter(ImageFilter.MaxFilter(9)))
    outline=ImageChops.multiply(outline,reference.getchannel('A').point(lambda v:255 if v>220 else 0))
    captions=[]
    for layer in plan.get('cta_preserve_text_layers',[]):
        if sha(layer['layer'])!=layer['layer_sha256']:raise ValueError('CTA_EXISTING_TEXT_CHANGED')
        with Image.open(layer['layer']) as im:ink=im.getchannel('A').resize((w,h)).point(lambda v:255 if v>0 else 0)
        captions.append((layer['start'],layer['end'],ink))
    cmd=[str(RES/'runtime/media/bin/ffmpeg'),'-v','error','-ss',str(start),'-i',str(video),'-an','-vf',f'fps=30,scale={w}:{h}','-f','rawvideo','-pix_fmt','rgb24','pipe:1']
    rows=[];first=math.ceil(start*30-1e-3)/30;size=w*h*3
    # Cover both historical right-side centres, including the pre-correction .85.
    right_xs={round(w*a-reference.width/2) for a in (1-LEFT_ANCHOR,.85)}
    with (folder/'decode.log').open('wb') as log:
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=log)
        try:
            while data:=proc.stdout.read(size):
                if len(data)!=size:raise ValueError('CTA_DECODE_FAILED')
                n=len(rows);t=first+n/30;frame=Image.frombytes('RGB',(w,h),data)
                x=round(s['x']*scale);y=round((s['base_y']+s['amplitude_px']*math.sin(2*math.pi*(t-start)/s['period']))*scale)
                def match_at(left,top=y):
                    best=(0,0,0,0,0,0)
                    for dx in (-2,0,2):
                        for dy in (-2,0,2):
                            box=(left+dx,top+dy,left+dx+template.width,top+dy+template.height)
                            crop=frame.crop(box);body,edge=template,outline
                            for lo,hi,ink in captions:
                                if lo<=t<hi:
                                    uncovered=ImageChops.invert(ink.crop(box))
                                    body=ImageChops.multiply(body,uncovered);edge=ImageChops.multiply(edge,uncovered)
                            if body.histogram()[255]<max(50,template.histogram()[255]*.10):continue
                            iou,red,white=artwork_score(crop,body,edge);score=min(red/.92,white/.70)
                            if score>best[0]:best=(score,dx,dy,iou,red,white)
                    return best
                best=match_at(x)
                right_ys={y,round(y+(.65-plan['visual']['cta']['anchor_y'])*h)}
                right=any(match_at(rx,ry)[0]>=1 for rx in right_xs for ry in right_ys)
                rows.append(dict(time=round(t,4),iou=round(best[3],4),red_recall=round(best[4],4),outline_recall=round(best[5],4),matched=best[0]>=1,dx=best[1],dy=best[2],right_matched=right))
                if n in {4,45,90,140}:frame.save(folder/f'frame-{n:03}.jpg',quality=95)
        finally:
            proc.stdout.close()
            if proc.poll() is None:proc.wait(timeout=30)
    expected=round((duration-first)*30);bad=[r for r in rows if not r['matched']]
    ok=proc.returncode==0 and len(rows)>=expected-1 and len(rows)>0 and len(bad)<=max(1,len(rows)*.02)
    right_frames=sum(r['right_matched'] for r in rows)
    issues=([] if ok else ['CTA_MISSING'])+(['CTA_RIGHT_FORBIDDEN'] if right_frames else [])
    result=dict(status='PASS' if not issues else 'FAIL',issues=issues,output=str(video),output_sha256=sha(video),
        start=start,end=duration,frames=len(rows),matched_frames=len(rows)-len(bad),minimum_iou=min((r['iou'] for r in rows),default=0),
        matcher=MATCHER_VERSION,position_policy=POSITION_POLICY,right_matched_frames=right_frames,samples=rows,asset_sha256=s['source_sha256'],anchor=plan['visual']['cta']['anchor_x'],y=plan['visual']['cta']['anchor_y'])
    write(folder/'cta-qa.json',result);return result


def overlay_only(plan,base,output,folder):
    """Burn only CTA over the completed MP4; audio stream is copied verbatim."""
    check_plan(plan);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);s=sprite(plan,folder)
    output=Path(output)
    if output.exists():raise ValueError('OUTPUT_ALREADY_EXISTS')
    graph=f"[1:v]setpts=PTS-STARTPTS+{s['start']}/TB[cta];[0:v][cta]overlay=x={s['x']}:y='{s['base_y']}+{s['amplitude_px']}*sin(2*PI*(t-{s['start']})/{s['period']})':enable='gte(t,{s['start']})*lt(t,{s['end']})':eof_action=pass[arrow]"
    text_inputs=[];last='arrow'
    for i,layer in enumerate(plan.get('cta_preserve_text_layers',[])):
        # Reuse the exact already-rendered PNG and timing. No caption builder,
        # alignment, transcription or TTS call is involved.
        text_inputs+=['-loop','1','-i',layer['layer']]
        graph+=f";[{last}][{i+2}:v]overlay=0:0:enable='gte(t,{max(s['start'],layer['start'])})*lt(t,{layer['end']})':eof_action=pass[text{i}]"
        last=f'text{i}'
    graph+=f';[{last}]format=yuv420p[out]'
    partial=output.with_suffix('.partial.mp4')
    cmd=[str(RES/'runtime/media/bin/ffmpeg'),'-hide_banner','-nostdin','-v','error','-i',str(base),'-ignore_loop','0','-i',s['path'],
         *text_inputs,'-filter_complex_threads','2','-filter_complex',graph,'-map','[out]','-map','0:a:0','-c:v','libx264','-preset','fast','-crf','18','-threads','4',
         '-c:a','copy','-t',str(plan['target_duration']),'-movflags','+faststart',str(partial)]
    with (folder/'overlay.log').open('wb') as log:
        cp=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=900)
    if cp.returncode:raise ValueError('CTA_RENDER_FAILED')
    partial.replace(output)
    return dict(output=str(output),output_sha256=sha(output),cta_overlay_from=dict(path=str(base),sha256=sha(base)))


def verify_or_repair(plan,video,folder,repair_output,allow_repair=True):
    before=verify(plan,video,Path(folder)/'before')
    if before['status'] in {'PASS','NOT_REQUIRED'}:return dict(output=str(video),output_sha256=sha(video),cta_verification=before,repaired=False)
    # Painting a second arrow would leave the old right arrow in the MP4.
    if 'CTA_RIGHT_FORBIDDEN' in before.get('issues',[]):raise ValueError('CTA_RIGHT_REQUIRES_RERENDER')
    if not allow_repair:raise ValueError('CTA_RENDER_FAILED')
    plan['visual']['cta']['enabled']=True;plan.pop('cta_retained_evidence',None)
    plan['visual'].setdefault('user_revision',{})['omit_added_cta']=False
    plan['cta_policy']['origin']='CTA_MISSING_REPAIR'
    if Path(repair_output).exists():
        # Recover a crash after render but before metadata commit, never overlay
        # a second arrow or overwrite an existing diagnostic result.
        result=dict(output=str(repair_output),output_sha256=sha(repair_output),cta_overlay_from=dict(path=str(video),sha256=sha(video)))
    else:result=overlay_only(plan,video,repair_output,Path(folder)/'repair')
    after=verify(plan,repair_output,Path(folder)/'after')
    if after['status']!='PASS':raise ValueError('CTA_RENDER_FAILED')
    return dict(result,cta_verification=after,repaired=True)
