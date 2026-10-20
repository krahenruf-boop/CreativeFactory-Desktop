"""Source-timed precise text covers with a mandatory subject/size gate.

OCR supplies observations, not permission to erase product/environment lettering.
Regions must be classified from video evidence before this renderer is bound.
"""
import copy, math
from pathlib import Path
from PIL import Image, ImageDraw
from app.s3.headless_runtime import sha

VERSION = 'ORIGINAL_TEXT_CLEANUP_V1'
CODES = {'ORIGINAL_SUBTITLE_VISIBLE', 'OLD_TEXT_RESIDUE', 'BLUR_PATCH_VISIBLE',
         'DOUBLE_SUBTITLE', 'WHITE_COVER_TOO_LARGE', 'COVER_SUBJECT_OCCLUSION'}
ROLES = {'TITLE', 'SUBTITLE', 'TEXT_RESIDUE', 'BLUR_PATCH', 'UNRELATED_STICKER'}


def box(value):
    if (not isinstance(value, list) or len(value)!=4 or
        not all(type(v) in (int,float) and math.isfinite(v) for v in value)):
        raise ValueError('TEXT_REGION_INVALID')
    l,t,r,b=value
    if not 0<=l<r<=1 or not 0<=t<b<=1:raise ValueError('TEXT_REGION_INVALID')
    return value


def intersects(a,b):
    return max(a[0],b[0])<min(a[2],b[2]) and max(a[1],b[1])<min(a[3],b[3])


def map_interval(plan, start, end):
    """Each Trim/Split/Jump Cut maps independently; speed never changes the copy."""
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or not 0<=start<end:
        raise ValueError('TEXT_TIME_INVALID')
    cursor=0.;out=[]
    segments=plan.get('montage_segments')
    if segments:
        for s in segments:
            # A region is bound to one source; never spill into another clip.
            if s['sha256']!=plan['source']['sha256']:continue
            a,b=s['source_start'],s['source_end'];lo,hi=max(a,start),min(b,end)
            if lo<hi:out.append([s['output_start']+(lo-a)/s['speed'],min(s['output_end'],plan['target_duration'],s['output_start']+(hi-a)/s['speed'])])
    else:
        speed=plan['video_speed']
        for a,b in plan.get('source_ranges',[[0,plan['source']['source_end']]]):
            lo,hi=max(a,start),min(b,end)
            if lo<hi:out.append([cursor+(lo-a)/speed,min(plan['target_duration'],cursor+(hi-a)/speed)])
            cursor+=(b-a)/speed
    return [[a,b] for a,b in out if a<b and a<plan['target_duration']]


def canvas_box(plan, region):
    """Mirror the renderer's scale-to-fill and central crop, not a stretch."""
    from .source_frame import canvas_box as fitted_box
    fitted = fitted_box(plan, region)
    if fitted is not None:return fitted
    sw,sh=plan['source']['width'],plan['source']['height'];w,h=plan['visual']['canvas']
    scale=max(w/sw,h/sh);dx=(sw*scale-w)/2;dy=(sh*scale-h)/2
    l,t,r,b=region
    result=[max(0,math.floor(l*sw*scale-dx)),max(0,math.floor(t*sh*scale-dy)),
            min(w,math.ceil(r*sw*scale-dx)),min(h,math.ceil(b*sh*scale-dy))]
    if result[0]>=result[2] or result[1]>=result[3]:raise ValueError('TEXT_REGION_OUTSIDE_CANVAS')
    return result


def evaluate(plan, spec):
    if spec.get('source_sha256')!=plan['source']['sha256']:raise ValueError('TEXT_SOURCE_CHANGED')
    if not spec.get('evidence'):raise ValueError('TEXT_EVIDENCE_REQUIRED')
    w,h=plan['source']['width'],plan['source']['height'];events=[];issues=[]
    subjects=spec.get('protected_regions') or []
    # A missing face detector result is not proof that the frame is safe.
    if spec.get('subject_scan_complete') is not True or not spec.get('reviewed_intervals'):
        raise ValueError('TEXT_SUBJECT_EVIDENCE_REQUIRED')
    for subject in subjects:
        box(subject['bbox'])
        if not 0<=subject['start']<subject['end']<=plan['source']['duration']+.001:
            raise ValueError('TEXT_SUBJECT_EVIDENCE_REQUIRED')
    for region in spec.get('regions',[]):
        role=region['role']
        if role in {'PRODUCT_BRANDING','ENVIRONMENT_INFORMATION'}:continue
        if role not in ROLES or not region.get('classified_from_video'):raise ValueError('TEXT_CLASSIFICATION_REQUIRED')
        original=box(region['original_bbox']);cover=box(region['bbox']);a,b=region['source_interval']
        if not 0<=a<b<=plan['source']['duration']+.001:raise ValueError('TEXT_TIME_INVALID')
        cursor=a
        for lo,hi in sorted(spec['reviewed_intervals']):
            if lo<=cursor<hi:cursor=hi
        if cursor<b:raise ValueError('TEXT_SUBJECT_EVIDENCE_REQUIRED')
        # At most four source pixels of padding, never percentage-based growth.
        extra=[(original[0]-cover[0])*w,(original[1]-cover[1])*h,(cover[2]-original[2])*w,(cover[3]-original[3])*h]
        area=(cover[2]-cover[0])*(cover[3]-cover[1])
        if min(extra)<-.01:issues.append(dict(code='OLD_TEXT_RESIDUE',region=region['id']))
        if max(extra)>4.01 or cover[2]-cover[0]>.8 or cover[3]-cover[1]>.13 or area>.08:
            issues.append(dict(code='WHITE_COVER_TOO_LARGE',region=region['id']))
        canvas_box(plan,cover)
        # An old opaque title lying across the face is NOT an exemption. The
        # preceding white-card strategy allowed this; full cleanup must not.
        for subject in subjects:
            if subject['kind'] not in {'face','hands','product'}:continue
            lo,hi=max(a,subject['start']),min(b,subject['end'])
            if lo<hi and intersects(cover,box(subject['bbox'])):
                issues.append(dict(code='COVER_SUBJECT_OCCLUSION',region=region['id'],subject=subject['kind'],source_interval=[lo,hi]))
        for lo,hi in map_interval(plan,a,b):
            events.append(dict(id=region['id'],role=role,bbox=cover,start=lo,end=hi,
                               subtitle_layout=region.get('subtitle_layout','KEEP_SAFE_POSITION'),
                               strategy='ORIGINAL_SUBTITLE_WHITE_COVER' if role=='SUBTITLE' else 'ORIGINAL_TEXT_WHITE_COVER'))
    return dict(strategy=VERSION,source_sha256=spec['source_sha256'],spec=copy.deepcopy(spec),
                events=events,issues=issues,status='FAIL' if issues else 'READY')


def bind(plan,spec):
    result=evaluate(plan,spec)
    if result['issues']:raise ValueError(result['issues'][0]['code'])
    plan['original_text_cleanup']=result
    return result


def validate(plan):
    result=plan.get('original_text_cleanup')
    if not result:return
    if result!=evaluate(plan,result['spec']) or result['status']!='READY':raise ValueError('TEXT_CLEANUP_PLAN_CHANGED')
    for evidence in result['spec']['evidence']:
        if sha(evidence['path'])!=evidence['sha256']:raise ValueError('TEXT_EVIDENCE_CHANGED')


def caption_layers(plan,event,folder):
    """Reflow cached alignment into a small plate, never regenerate speech/copy."""
    from app.s3.subtitles import words_from_alignment
    from app.s3.headless_runtime import read
    from app.s3.visual_preset import load_font,display_text
    path=plan['audio']['alignment_path']
    if not any(e['path']==path and e['sha256']==sha(path) for e in plan['original_text_cleanup']['spec']['evidence']):
        raise ValueError('TEXT_ALIGNMENT_EVIDENCE_REQUIRED')
    raw=read(path);alignment=raw.get('alignment') or raw.get('normalized_alignment')
    if ''.join(alignment['characters'])!=plan['final_script']:raise ValueError('ALIGNMENT_TEXT_MISMATCH')
    w,h=plan['visual']['canvas'];l,t,r,b=canvas_box(plan,event['bbox']);style=plan['visual']['subtitle']
    font,fp,fd=load_font(style,round(style['font_size']*min(w/1080,h/1920)),plan['final_script'])
    pad=4;offset=plan['audio_sample_range'][0]/plan['audio_speed'] if plan.get('benchmark_sample') else 0
    words=[dict(x,start=x['start']/plan['audio_speed']-offset,end=x['end']/plan['audio_speed']-offset) for x in words_from_alignment(alignment)]
    words=[x for x in words if x['end']>event['start'] and x['start']<event['end']]
    groups=[];group=[]
    def fits(g):
        bb=font.getbbox(display_text(' '.join(x['text'] for x in g)))
        return bb[2]-bb[0]<=r-l-2*pad and bb[3]-bb[1]<=b-t-2*pad
    for word in words:
        if not fits([word]):raise ValueError('LOCALIZED_TEXT_LAYOUT_REQUIRED')
        if group and (not fits(group+[word]) or word['start']-group[-1]['end']>.25):groups.append(group);group=[]
        group.append(word)
    if group:groups.append(group)
    out=[]
    for i,g in enumerate(groups):
        text=display_text(' '.join(x['text'] for x in g));bb=font.getbbox(text)
        image=Image.new('RGBA',(w,h));draw=ImageDraw.Draw(image)
        draw.text((l+(r-l-bb[2]+bb[0])/2-bb[0],t+(b-t-bb[3]+bb[1])/2-bb[1]),text,font=font,fill='#171717')
        path=Path(folder)/f'caption_{i:03}.png';image.save(path)
        lo=max(event['start'],g[0]['start']);next_start=groups[i+1][0]['start'] if i+1<len(groups) else event['end']
        hi=min(event['end'],next_start,max(g[-1]['end'],min(next_start,g[-1]['end']+.10)))
        if lo<hi:out.append(dict(kind='subtitle',start=lo,end=hi,text=text,layer=str(path),layer_sha256=sha(path),
            bbox=list(image.getbbox()),font_file=fp,font_sha256=fd,font_loaded=style['font_name'],font_size=font.size,foreground='#171717'))
    return out


def apply_layers(plan,evidence,folder):
    result=plan.get('original_text_cleanup')
    if not result:return
    validate(plan);w,h=plan['visual']['canvas'];layers=[];cache={};titles=[];caption_windows=[];captions=[]
    for event in result['events']:
        bbox=tuple(canvas_box(plan,event['bbox']));key=(bbox,event['role'])
        if key not in cache:
            path=Path(folder)/f'original_text_cover_{len(cache):03}.png'
            im=Image.new('RGBA',(w,h));ImageDraw.Draw(im).rectangle((bbox[0],bbox[1],bbox[2]-1,bbox[3]-1),fill='#FFFFFF');im.save(path)
            cache[key]=dict(layer=str(path),layer_sha256=sha(path),bbox=list(bbox))
            if event['role']=='TITLE':
                # Reuse the verified title fitting code and the exact approved
                # wording. The new subject gate applies even to old white cards.
                from .title_blur_cover import bind as bind_title, raster
                p=copy.deepcopy(plan);rect=[v/s for v,s in zip(bbox,[w,h,w,h])]
                p['source_ranges']=[[0,p['source']['duration']]]
                bind_title(p,dict(kind='BLURRED_TITLE',background='WHITE_CARD',bbox=rect,original_card_bbox=rect,
                    source_interval=[0,p['source']['duration']],persistent_source_card=True,safe_to_cover=True,
                    confidence=1.,source_sha256=p['source']['sha256'],evidence=result['spec']['evidence']))
                sub=Path(folder)/f'original_title_{len(cache):03}';sub.mkdir(exist_ok=True)
                cache[key]=raster(p,sub)
        layers.append(dict(cache[key],kind=event['strategy'],start=event['start'],end=event['end']))
        if event['role']=='TITLE':titles.append([event['start'],event['end']])
        if event['role']=='SUBTITLE' and event['subtitle_layout']=='IN_COVER':
            sub=Path(folder)/f'original_caption_{len(caption_windows):03}';sub.mkdir(exist_ok=True)
            captions.extend(caption_layers(plan,event,sub));caption_windows.append([event['start'],event['end']])
    rows=[]
    for row in evidence['text_layers']:
        intervals=[[row['start'],row['end']]]
        if row['kind'] in {'title','subtitle'}:
            for a,b in titles if row['kind']=='title' else caption_windows:
                intervals=[[x,y] for lo,hi in intervals for x,y in [(lo,min(hi,a)),(max(lo,b),hi)] if x<y]
        # Keep captions at their existing safe position (option B). If entirely
        # inside a white plate, use dark lettering only during its active time.
        cuts=sorted({v for interval in intervals for v in interval} |
                    {v for layer in layers for v in (layer['start'],layer['end']) if row['start']<v<row['end']})
        for a,b in zip(cuts,cuts[1:]):
            if not any(lo<=a<hi for lo,hi in intervals):continue
            covers=[x for x in layers if x['start']<=a<x['end'] and intersects(x['bbox'],row['bbox'])]
            updated=dict(row,start=a,end=b)
            if covers:
                if not any(x['bbox'][0]<=row['bbox'][0] and x['bbox'][1]<=row['bbox'][1] and x['bbox'][2]>=row['bbox'][2] and x['bbox'][3]>=row['bbox'][3] for x in covers):
                    raise ValueError('LOCALIZED_TEXT_LAYOUT_REQUIRED')
                path=Path(folder)/f'original_text_dark_{len(rows):03}.png'
                with Image.open(row['layer']) as im:
                    dark=Image.new('RGBA',im.size,'#171717');dark.putalpha(im.getchannel('A'));dark.save(path)
                updated.update(layer=str(path),layer_sha256=sha(path),foreground='#171717')
            rows.append(updated)
    # Covers are BELOW localized title/subtitles. This keeps one target caption
    # track; no source wording is copied into TTS or into subtitle text.
    evidence['text_layers']=layers+rows+captions
    evidence['original_text_cleanup']=dict(events=result['events'],source_sha256=result['source_sha256'])


def review_issues(plan,review):
    """The previous ordinary-warning policy cannot waive known old text here."""
    if not plan.get('original_text_cleanup'):return []
    codes=[]
    if review.get('source_text') is not True:codes.append('ORIGINAL_SUBTITLE_VISIBLE')
    if review.get('overlay') is not True:codes.append('OLD_TEXT_RESIDUE')
    if review.get('product_preserved') is not True:codes.append('COVER_SUBJECT_OCCLUSION')
    # Explicit findings remain blocking even if a visual service calls them mild.
    notes=' '.join(review.get('issues') or []).upper()
    codes.extend(code for code in CODES if code in notes)
    return sorted(set(codes))
