"""Plan-executing preflight/final render and one-decode QA frame bank.

Uses the existing frozen visual rasterizer and overlay gate. No new layout,
translation, voice, cleanup or source-selection decisions live here.
"""
import copy
import inspect
import math
from pathlib import Path
import re
import subprocess
import time
import unicodedata
from fractions import Fraction
from PIL import Image, ImageDraw

from .headless_runtime import ROOT, read, write, sha, fingerprint, locked
from .overlay_policy import render_gate
from .visual_preset import visual_inputs, validate_visual_plan

DATA = Path.home() / 'Library/Application Support/Creative Factory'
FRACTIONS = [0, .1, .2, .3, .45, .6, .75, .88, .96]


def run(args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True,
                          text=True, timeout=kwargs.pop('timeout', 900), **kwargs)


def probe(path):
    import json
    return json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',path]).stdout)


def identity(plan):
    # Hash actual dependencies so changing a font/asset/file cannot reuse approval.
    files = [plan['source']['path'], plan['audio']['audio_path']]
    files.extend(s['path'] for s in plan.get('montage_segments', []))
    for layer in ('title','subtitle'):
        p=Path(plan['visual'][layer]['font_file']).expanduser()
        files.append(str(p if p.is_absolute() else ROOT/p))
    if plan['visual']['cta'].get('enabled', True):
        asset = plan['visual']['cta']['asset']
        from app.cta_assets import CTAAssets
        files.append(str(CTAAssets(DATA).path(asset)))
    for segment in plan.get('interval_composition', {}).get('segments', []):
        files.extend([segment['path'], segment['locked_plan']])
    return fingerprint(dict(plan={k:v for k,v in plan.items() if k not in {'output','render_attempts'}},
                            render_contract=fingerprint(inspect.getsource(render_plan)+inspect.getsource(text_timeline)+inspect.getsource(validate_montage)),
                            files={str(p):sha(p) for p in files}))


def validate_montage(plan):
    """Execute an explicitly authored cut list; never choose/reorder footage here."""
    segments = plan.get('montage_segments', [])
    if not segments: return
    if plan.get('interval_composition') or plan.get('source_ranges') or plan['video_speed'] != 1:
        raise ValueError('MONTAGE_CONFLICTING_TIMELINE')
    cursor = 0
    checked = {}
    for s in segments:
        values = [s[k] for k in ('source_start','source_end','source_duration','speed','output_start','output_end')]
        if not all(math.isfinite(v) for v in values): raise ValueError('MONTAGE_INVALID_RANGE')
        a,b,d,speed,lo,hi = values
        if not (0 <= a < b <= d + .001 and .9 <= speed <= 1.3 and lo < hi):
            raise ValueError('MONTAGE_INVALID_RANGE')
        if abs(lo-cursor) > .001 or abs((b-a)/speed-(hi-lo)) > .001:
            raise ValueError('MONTAGE_TIMELINE_GAP')
        if any(abs(t*30-round(t*30)) > .001 for t in (lo,hi)):
            raise ValueError('MONTAGE_FRAME_GRID_REQUIRED')
        if s['path'] not in checked: checked[s['path']] = sha(s['path'])
        if checked[s['path']] != s['sha256']: raise ValueError('MONTAGE_SOURCE_HASH_CHANGED')
        cursor = hi
    if abs(cursor-plan['target_duration']) > .001: raise ValueError('MONTAGE_DURATION_MISMATCH')


def windows(duration):
    # Half-frame midpoints otherwise drop one frame when the three clips concat.
    middle = round(duration * 15) / 30
    intervals = sorted([(0, min(5, duration)), (max(0,middle-1), min(duration,middle+1)),
                        (max(0,duration-5), duration)])
    merged = []
    for a,b in intervals:
        if merged and a <= merged[-1][1]: merged[-1][1] = max(b, merged[-1][1])
        else: merged.append([a,b])
    return merged


def text_timeline(visual, evidence, folder):
    """Same preset PNGs, multiplexed once instead of one PNG decoder per phrase."""
    layers = evidence['text_layers']; end = math.ceil(visual['duration']*30)
    cuts = sorted({0,end,*[max(0,min(end,math.ceil(float(m[k])*30)))
                          for m in layers for k in ('start','end')]})
    cached = {}; lines = ['ffconcat version 1.0']; last = None
    for lo,hi in zip(cuts,cuts[1:]):
        active = tuple(i for i,m in enumerate(layers) if m['start'] <= lo/30 < m['end'])
        if active not in cached:
            im = Image.new('RGBA',tuple(visual['canvas']))
            for i in active:
                with Image.open(layers[i]['layer']) as layer: im.alpha_composite(layer.convert('RGBA'))
            p = folder / f'state_{len(cached):04}.png'; im.save(p); cached[active] = p
        last = cached[active]
        # Relative generated names avoid FFconcat escaping user paths.
        lines += [f"file '{last.name}'", 'option framerate 30', f'duration {(hi-lo)/30:.9f}']
    lines += [f"file '{last.name}'", 'option framerate 30']
    path = folder/'text.ffconcat'; path.write_text('\n'.join(lines)+'\n')
    return path


def render_plan(plan, output, folder, *, preflight=False, encoder='libx264'):
    validate_montage(plan)
    failures = render_gate(plan)
    if failures: raise ValueError('OVERLAY_GATE:' + ','.join(failures))
    if plan.get('supplements'):
        raise ValueError('SUPPLEMENTS_REQUIRE_EXISTING_TRANSPORT_RENDER_CALLBACK')
    if (any(plan.get(k) for k in ('source_text_masks','overlay_title_events','crop','experimental_overlay','original_mirror'))
        or any(r.get('overlay_class')!='PRODUCT_BRANDING' for r in plan.get('overlays',[]))):
        raise ValueError('OVERLAY_SOURCE_MUST_BE_RESOLVED_BEFORE_VISUAL_RENDER')
    if sha(plan['source']['path']) != plan['source']['sha256'] or sha(plan['audio']['audio_path']) != plan['audio']['sha256']:
        raise ValueError('INPUT_HASH_CHANGED')
    output, folder = Path(output), Path(folder)
    output.parent.mkdir(parents=True, exist_ok=True); folder.mkdir(parents=True, exist_ok=True)
    if output.exists(): raise ValueError('OUTPUT_ALREADY_EXISTS')
    visual = copy.deepcopy(plan['visual']); d = plan['target_duration']
    selections = windows(d) if preflight else [[0,d]]
    select = ''
    if preflight:
        terms='+'.join(f'gte(t,{a})*lt(t,{b})' for a,b in selections)
        select=f"select='{terms}',"
    original_w,original_h = visual['canvas']
    w,h = (540,960) if preflight else visual['canvas']
    _,_,e = visual_inputs(visual, folder, DATA, first_index=2)
    cta_enabled = e['cta'].get('enabled', True)
    extra_input = 4 if cta_enabled else 3
    timeline = text_timeline(visual,e,folder)
    graph = []; base = '0:v:0'
    montage = plan.get('montage_segments', [])
    first = montage[0] if montage else None
    first_input = (['-ss',first['source_start'],'-t',first['source_end']-first['source_start'],'-i',first['path']]
                   if first else ['-i',plan['source']['path']])
    args = ['ffmpeg','-hide_banner','-nostdin','-y','-threads','2','-filter_complex_threads','2',
            *first_input,'-i',plan['audio']['audio_path'],'-safe','0','-i',timeline]
    if cta_enabled:
        args += [*(['-ignore_loop','0'] if e['cta']['frame_count']>1 else ['-loop','1']),'-i',e['cta']['path']]
    for j,seg in enumerate(plan.get('interval_composition',{}).get('segments',[])):
        if sha(seg['locked_plan']) != seg['locked_plan_sha256'] or sha(seg['path']) != seg['sha256'] or render_gate(read(seg['locked_plan'])):
            raise ValueError('OVERLAY_COMPOSITION_CHANGED')
        args += ['-i',seg['path']]
        graph += [f'[{j+extra_input}:v]setpts=PTS-STARTPTS+{seg["start"]}/TB[patch{j}]',
                  f'[{base}][patch{j}]overlay=0:0:enable=\'gte(t,{seg["start"]})*lt(t,{seg["end"]})\':eof_action=pass[clean{j}]']
        base = f'clean{j}'
    ranges = plan.get('source_ranges', [[0,plan['source']['source_end']]])
    if montage:
        for i,s in enumerate(montage):
            index = 0 if i==0 else i+extra_input-1
            if i:
                args += ['-threads','2','-ss',s['source_start'],'-t',s['source_end']-s['source_start'],'-i',s['path']]
            frames = round((s['output_end']-s['output_start'])*30)
            graph.append(f'[{index}:v:0]setpts=(PTS-STARTPTS)/{s["speed"]},fps=30,scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos:out_range=tv,crop={w}:{h},setsar=1,tpad=stop_mode=clone:stop_duration=0.04,trim=end_frame={frames},setpts=PTS-STARTPTS[mix{i}]')
        graph.append(''.join(f'[mix{i}]' for i in range(len(montage)))+f'concat=n={len(montage)}:v=1:a=0[joined]')
        base = 'joined'
    elif len(ranges)>1:
        graph.append(f'[{base}]split={len(ranges)}'+''.join(f'[in{i}]' for i in range(len(ranges))))
        for i,(a,b) in enumerate(ranges): graph.append(f'[in{i}]trim=start={a}:end={b},setpts=PTS-STARTPTS[cut{i}]')
        graph.append(''.join(f'[cut{i}]' for i in range(len(ranges)))+f'concat=n={len(ranges)}:v=1:a=0[joined]'); base='joined'
    else:
        a,b=ranges[0]; graph.append(f'[{base}]trim=start={a}:end={b},setpts=PTS-STARTPTS[cut]'); base='cut'
    graph += [f'[{base}]setpts=(PTS-STARTPTS)/{plan["video_speed"]:.12f},fps=30,{select}scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos:out_range=tv,crop={w}:{h},setsar=1,trim=duration={d}[base]',
              f'[2:v]fps=30,{select}scale={w}:{h}:flags=lanczos,format=rgba[text]', '[base][text]overlay=0:0:eof_action=pass[captioned]']
    # Rasterize the exact final layout once, then scale its pixels/coordinates.
    # Re-fitting fonts or rounding a body-width sprite at 540 can change layout.
    if cta_enabled:
        c=copy.deepcopy(e['cta']); start=c['start']
        sx,sy=w/original_w,h/original_h
        c['x']*=sx;c['base_y']*=sy;c['amplitude_px']*=sy
        cta_scale=f'scale={round(c["width"]*sx)}:{round(c["height"]*sy)}:flags=lanczos,'
        graph += [f'[3:v]{cta_scale}setpts=PTS-STARTPTS+{start}/TB[cta]',
                  f'[captioned][cta]overlay=x={c["x"]}:y=\'{c["base_y"]}+{c["amplitude_px"]}*sin(2*PI*(t-{start})/{c["period"]})\':enable=\'gte(t,{start})*lt(t,{d})\':eof_action=pass,format=yuv420p[fullv]']
    else:
        graph += ['[captioned]format=yuv420p[fullv]']
    audio_trim=''
    if plan.get('benchmark_sample'):
        lo,hi=plan['audio_sample_range'];audio_trim=f'atrim=start={lo}:end={hi},asetpts=PTS-STARTPTS,'
    graph += [f'[1:a]{audio_trim}atempo={plan["audio_speed"]:.12f},apad,atrim=duration={d},asetpts=PTS-STARTPTS[fulla]']
    if preflight:
        n=len(selections)
        graph += [f'[fullv]split={n}'+''.join(f'[pv{i}]' for i in range(n)),
                  f'[fulla]asplit={n}'+''.join(f'[pa{i}]' for i in range(n))]
        for i,(a,b) in enumerate(selections):
            graph += [f'[pv{i}]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]',
                      f'[pa{i}]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]']
        graph.append(''.join(f'[v{i}][a{i}]' for i in range(n))+f'concat=n={n}:v=1:a=1[outv][outa]')
    else:
        graph += ['[fullv]null[outv]','[fulla]anull[outa]']
    if encoder not in {'libx264','h264_videotoolbox'}: raise ValueError('ENCODER_NOT_ALLOWED')
    codec = ['-c:v','libx264','-preset','fast','-crf','18','-threads','4'] if encoder=='libx264' else ['-c:v','h264_videotoolbox','-b:v','12M','-allow_sw','0']
    partial = output.with_name(output.stem+'.partial.mp4')
    args += ['-filter_complex',';'.join(graph),'-map','[outv]','-map','[outa]',*codec,
             '-pix_fmt','yuv420p','-color_range','tv','-r','30','-fps_mode','cfr',
             '-c:a','aac','-b:a','192k','-ar','48000','-t',str(sum(b-a for a,b in selections)),
             '-movflags','+faststart',partial]
    write(folder/'visual_evidence.json',e)
    begin=time.monotonic()
    with (folder/'render.log').open('w') as log:
        completed=subprocess.run(list(map(str,args)),stdout=log,stderr=subprocess.STDOUT,timeout=1800)
    if completed.returncode: raise ValueError('FFMPEG_RENDER_FAILED')
    partial.replace(output)
    result=dict(status='AWAITING_VISUAL_REVIEW',output=str(output),output_sha256=sha(output),
                elapsed_seconds=time.monotonic()-begin,encoder=encoder,selections=selections,
                evidence=str(folder/'visual_evidence.json'),canvas=[w,h],
                artifacts=[dict(path=str(output),sha256=sha(output))])
    write(folder/'result.json',result)
    return result


def preflight_job(harness, plan):
    plan=copy.deepcopy(plan)
    key=identity(plan); folder=harness.work/'preflight'/key
    def call():
        result=render_plan(plan,folder/'preview.mp4',folder/'layers',preflight=True)
        bank=frame_bank(result['output'],folder/'frames',canvas=(540,960),
                        duration=sum(b-a for a,b in result['selections']))
        sheet=contact_sheet(bank,folder/'contact.jpg')
        result.update(plan_fingerprint=key,visual_review='PENDING',technical_status=bank['status'],
                      issues=bank['issues'],contact_sheet=sheet['path'],qa_path=str(folder/'frames/qa.json'))
        result['artifacts'] += [sheet,dict(path=result['qa_path'],sha256=sha(result['qa_path']))]
        if bank['status']!='PASS':result.update(status='REVIEW_REQUIRED',error='PREFLIGHT_TECHNICAL_FAILED')
        write(folder/'result.json',result)
        return result
    return harness.submit('LAYOUT_PREFLIGHT',plan['rank'],dict(plan=key),call)


def approve_preflight(harness, plan, review):
    key=identity(plan); path=harness.work/'preflight'/key; result=read(path/'result.json',{})
    checks={'title','subtitle','cta','overlay','source_text','watermark'}
    if (review.get('reviewer','').casefold()!='codex' or review.get('status')!='PASS'
        or set(review.get('checks',{}))!=checks or not all(v is True for v in review['checks'].values())
        or review.get('preview_sha256')!=result.get('output_sha256')
        or result.get('technical_status')!='PASS'
        or not result or sha(result['output'])!=result['output_sha256']):
        raise ValueError('REAL_PREFLIGHT_VISUAL_REVIEW_REQUIRED')
    write(path/'approval.json',dict(review,plan_fingerprint=key))


def final_job(harness,plan,*,rework_reason=None):
    plan=copy.deepcopy(plan)
    if plan.get('benchmark_sample'): raise ValueError('BENCHMARK_IS_NOT_PRODUCTION')
    key=identity(plan); approval=read(harness.work/'preflight'/key/'approval.json',{})
    if approval.get('status')!='PASS' or approval.get('plan_fingerprint')!=key:
        raise ValueError('PREFLIGHT_PASS_REQUIRED')
    result=read(harness.work/'preflight'/key/'result.json',{})
    if not result or sha(result['output'])!=approval.get('preview_sha256'):
        raise ValueError('PREFLIGHT_CHANGED')
    if plan.get('title_enabled',True) and plan.get('title_status')!='USER_APPROVED':
        raise ValueError('TITLE_USER_APPROVED_REQUIRED')
    if plan.get('title_enabled',True) and not plan['visual']['title']['text'].strip():
        raise ValueError('TITLE_EXISTS_REQUIRED')
    if not plan.get('title_enabled',True) and plan.get('user_revision',{}).get('omit_added_title') is not True:
        raise ValueError('TITLE_USER_EXCEPTION_MISSING')
    if plan['visual']['canvas']!=[1080,1920]:raise ValueError('FINAL_RESOLUTION_MUST_BE_1080_1920')
    config=read(ROOT/'presets/headless_execution.json')
    budget=harness.work/'budgets'/f'render_{fingerprint(str(plan["rank"]))}.json'
    def call():
        if identity(plan)!=key:return dict(status='REVIEW_REQUIRED',error='INPUT_CHANGED_AFTER_PREFLIGHT')
        with locked(budget.with_suffix('.lock')):
            count=read(budget,{}).get('full_render_count',0)
            if count>=2 or (count and rework_reason not in {'ENCODE_CORRUPTION','FULL_DURATION_TIMING','FULL_DECODE_FAILURE'}):
                return dict(status='REVIEW_REQUIRED',error='FULL_RENDER_BUDGET_OR_REASON_REJECTED')
            write(budget,dict(full_render_count=count+1,rework_reason=rework_reason))
        folder=harness.work/'render'/key
        return render_plan(plan,plan['output'],folder,encoder=config['encoder'])
    return harness.submit('FINAL_RENDER',plan['rank'],dict(plan=key,rework=rework_reason),call)


def frame_bank(video,folder,*,overlay_regions=(),canvas=(1080,1920),duration=None,sample_times=()):
    """One complete FFmpeg decode: PTS, black/silence checks and shared samples.

    ffprobe reads container metadata, not a second pixel decode. Visual judgments
    remain PENDING; a contact sheet is evidence, never an automatic visual PASS.
    """
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    info=probe(video); v=next(s for s in info['streams'] if s['codec_type']=='video')
    a=next((s for s in info['streams'] if s['codec_type']=='audio'),None)
    d=float(v.get('duration',info['format']['duration'])); fps=float(Fraction(v['avg_frame_rate']))
    times=[d*f for f in FRACTIONS]
    times += [max(0,min(d,float(t))) for t in sample_times]
    for r in overlay_regions:
        lo,hi=r['output_interval']
        times += [max(0,lo-.1),min(d,lo+.15),(lo+hi)/2,max(lo,hi-.1)]
    indices=sorted({min(max(0,round(t*fps)),max(0,round(d*fps)-1)) for t in times})
    select='+'.join(f'eq(n,{i})' for i in indices)
    graph=f"[0:v:0]blackdetect=d=0.7:pix_th=0.10,showinfo,select='{select}',scale=540:-2[frames]"
    if a: graph+=';[0:a:0]volumedetect[audio]'
    args=['ffmpeg','-y','-hide_banner','-nostdin','-xerror','-threads','2','-filter_complex_threads','1','-i',video,
          '-filter_complex',graph,'-map','[frames]','-fps_mode','vfr','-c:v','png',folder/'frame_%03d.png']
    if a: args+=['-map','[audio]','-f','null','-']
    begin=time.monotonic(); cp=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=900)
    (folder/'decode.log').write_text(cp.stderr)
    points=re.findall(r'\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([\d.eE+\-]+)',cp.stderr)
    pts=[float(p[2]) for p in points]; issues=[]
    def check(ok,code):
        if not ok: issues.append(code)
    check(cp.returncode==0,'decode_failed');check((v['width'],v['height'])==tuple(canvas),'resolution_mismatch')
    check(abs(fps-30)<.001,'fps_mismatch');check(v['codec_name']=='h264','video_codec_mismatch')
    check(v.get('pix_fmt')=='yuv420p','pixel_format_mismatch');check(bool(a) and a['codec_name']=='aac','audio_missing')
    check(bool(pts) and all(y>x for x,y in zip(pts,pts[1:])),'non_monotonic_pts')
    check(not re.search(r'black_duration:[0-9]',cp.stderr),'black_frame')
    volume=re.findall(r'mean_volume: ([\-\w.]+) dB',cp.stderr)
    check(bool(volume) and float(volume[-1])>-55,'silent_audio')
    if a: check(abs(d-float(a.get('duration',d)))<=.08,'audio_duration_mismatch')
    if duration is not None: check(abs(d-duration)<=.067,'duration_mismatch')
    samples=[]
    for j,i in enumerate(indices,1):
        p=folder/f'frame_{j:03}.png'
        if p.exists(): samples.append(dict(frame=i,time=i/fps,path=str(p)))
    check(len(samples)==len(indices),'sample_frames_missing')
    result=dict(status='PASS' if not issues else 'FAIL',visual_status='PENDING',issues=issues,
                output=str(video),output_sha256=sha(video),samples=samples,full_decode_count=1,
                qa_level=3 if overlay_regions else 2,elapsed_seconds=time.monotonic()-begin,
                frame_count=len(pts),duration=d,ffprobe=info)
    write(folder/'qa.json',result); return result


def technical_qa(plan,evidence,bank):
    issues=list(bank['issues'])+render_gate(plan)+validate_visual_plan(plan['visual'])
    try: validate_montage(plan)
    except ValueError as exc: issues.append(str(exc))
    if evidence['font_fallback']: issues.append('font_fallback')
    d=plan['target_duration']
    if plan['visual']['cta'].get('enabled', True):
        if evidence['cta'].get('enabled', True) is False: issues.append('cta_missing')
        else:
            if evidence['cta']['horizontal_drift_px']>1: issues.append('CTA_HORIZONTAL_DRIFT')
            if evidence['cta']['start']!=max(0,d-5): issues.append('cta_window_mismatch')
    elif evidence['cta'].get('enabled') is not False:
        issues.append('UNWANTED_CTA_LAYER')
    if plan['audio']['duration']/plan['audio_speed']>bank['duration']+.03: issues.append('voice_truncated')
    for cap in plan['visual']['subtitle']['captions']:
        if any(unicodedata.category(c).startswith('P') for c in cap['subtitle_display_text']): issues.append('SUBTITLE_HAS_PUNCTUATION')
    for kind in ('source','audio'):
        path=plan[kind].get('path') or plan[kind].get('audio_path')
        if sha(path)!=plan[kind]['sha256']: issues.append(kind+'_hash_changed')
    if plan.get('title_enabled',True) and plan.get('title_status')!='USER_APPROVED': issues.append('TITLE_USER_APPROVED')
    if plan.get('title_enabled',True) and not plan['visual']['title']['text'].strip():issues.append('TITLE_EXISTS')
    if not plan.get('title_enabled',True):
        if plan.get('user_revision',{}).get('omit_added_title') is not True:issues.append('TITLE_USER_EXCEPTION_MISSING')
        if any(m['kind']=='title' for m in evidence['text_layers']):issues.append('UNWANTED_TITLE_LAYER')
    return dict(bank,issues=sorted(set(issues)),status='FAIL' if issues else 'PASS')


def contact_sheet(bank,path):
    canvas=Image.new('RGB',(810,1530),'#eeeeee'); draw=ImageDraw.Draw(canvas)
    for i,f in enumerate(FRACTIONS):
        sample=min(bank['samples'],key=lambda s:abs(s['time']-bank['duration']*f))
        with Image.open(sample['path']) as im: canvas.paste(im.resize((270,480)),((i%3)*270,(i//3)*510))
        draw.text(((i%3)*270+8,(i//3)*510+484),f'{sample["time"]:.2f}s',fill='black')
    canvas.save(path,quality=93)
    return dict(path=str(path),sha256=sha(path),decode_count=0)


def batch_sheets(banks,overlays,folder):
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    canvas=Image.new('RGB',(960,max(1,len(banks))*310),'#eeeeee'); draw=ImageDraw.Draw(canvas)
    for row,bank in enumerate(banks):
        draw.text((5,row*310+5),str(bank.get('rank',row+1))+' · '+bank['status'],fill='black')
        for col,f in enumerate([0,.45,.88,.96]):
            sample=min(bank['samples'],key=lambda s:abs(s['time']-bank['duration']*f))
            with Image.open(sample['path']) as im: canvas.paste(im.resize((152,270)),(col*240,row*310+28))
    canvas.save(folder/'BATCH_CONTACT_SHEET.jpg',quality=92)
    panel=Image.new('RGB',(960,max(1,len(overlays))*230),'#eeeeee'); draw=ImageDraw.Draw(panel)
    if not overlays: draw.text((10,10),'No modified overlays in this batch',fill='black')
    for row,r in enumerate(overlays):
        draw.text((5,row*230+5),str(r['id'])+' · '+r.get('status','PENDING'),fill='black')
        for col,k in enumerate(['before_crop','after_crop','mask_preview']):
            if r.get(k):
                with Image.open(r[k]) as im:
                    im=im.convert('RGB');im.thumbnail((310,195));panel.paste(im,(col*320,row*230+28))
    panel.save(folder/'BATCH_OVERLAY_QA.jpg',quality=92)
    return dict(decode_count=0,contact=str(folder/'BATCH_CONTACT_SHEET.jpg'),overlay=str(folder/'BATCH_OVERLAY_QA.jpg'))
