"""Narrow, evidence-bound replacement of a damaged top title card.

No source pixels, approved copy, audio or ordinary title preset are modified.
White background and lettering are one timed raster, never separate layers.
"""
import copy, math
from pathlib import Path
from PIL import Image, ImageDraw
from app.s3.headless_runtime import sha, fingerprint
from app.s3.visual_preset import load_font

VERSION = 'TITLE_BLUR_WHITE_COVER'


def rectangle(value):
    if (not isinstance(value, list) or len(value) != 4
            or not all(type(v) in (int, float) and math.isfinite(v) for v in value)):
        raise ValueError('TITLE_COVER_INVALID_REGION')
    l, t, r, b = value
    if not (0 <= l < r <= 1 and 0 <= t < b <= .33 and r-l <= .72 and b-t <= .12):
        raise ValueError('TITLE_COVER_UNSAFE_REGION')
    return value


def intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def prepare(plan, region):
    """Map detected SOURCE timing through actual cuts/speed; abstain when unsafe."""
    if not region or not plan.get('title_enabled', True) or plan.get('montage_segments'):
        return None
    if region.get('kind') != 'BLURRED_TITLE' or region.get('background') not in {'WHITE_CARD', 'BACKGROUND'}:
        return None  # Intact/colored cards and normal titles retain their own policy.
    if region.get('safe_to_cover') is not True or region.get('confidence', 0) < .9:
        return None
    if region.get('source_sha256') != plan['source']['sha256'] or not region.get('evidence'):
        raise ValueError('TITLE_COVER_EVIDENCE_REQUIRED')
    box = rectangle(region.get('bbox'))
    lo, hi = region['source_interval']
    if not (all(type(v) in (int, float) and math.isfinite(v) for v in (lo, hi))
            and 0 <= lo < hi <= plan['source']['duration'] + .001):
        raise ValueError('TITLE_COVER_INVALID_TIME')
    # White-card replacement uses exactly the already occupied rectangle: no
    # padding may grow onto a face/product. A blur over real scenery needs clear
    # subject separation, including all time-overlapping protected regions.
    if region['background'] == 'WHITE_CARD':
        if rectangle(region.get('original_card_bbox')) != box:
            raise ValueError('TITLE_COVER_CARD_EXPANSION')
    else:
        subjects = plan.get('subject_protection', {}).get('protected_regions') or []
        if not subjects:
            return None
        for subject in subjects:
            if subject['start'] <= hi and subject['end'] >= lo and intersects(box, subject['bbox']):
                return None
    persistent = region.get('persistent_source_card') is True
    if persistent and region['background'] != 'WHITE_CARD':
        raise ValueError('TITLE_COVER_PERSISTENT_CARD_REQUIRED')
    intervals = []
    cursor = 0.
    for a, b in plan.get('source_ranges', [[0, plan['source']['source_end']]]):
        start = cursor + max(0, lo-a)/plan['video_speed']
        end = cursor + (min(b, hi)-a)/plan['video_speed']
        end = min(end, plan['target_duration'])
        if not persistent:
            start = max(start, plan['visual']['title']['start'])
            end = min(end, plan['visual']['title']['end'])
        if max(a, lo) < min(b, hi) and start < end:
            intervals.append([start, end])
        cursor += (b-a)/plan['video_speed']
    if not intervals:
        return None
    return dict(strategy=VERSION, source_sha256=region['source_sha256'], bbox=box,
                intervals=intervals, region=copy.deepcopy(region), fill='#FFFFFF',
                text=plan['title'], language=plan['final_title_language'],
                timing_reason='PERSISTENT_SOURCE_CARD_REPLACEMENT' if persistent else 'HOOK_ONLY')


def bind(plan, region):
    cover = prepare(plan, region)
    if cover:
        plan['title_blur_white_cover'] = cover
    return cover


def validate(plan):
    cover = plan.get('title_blur_white_cover')
    if not cover:
        return
    if cover != prepare(plan, cover['region']):
        raise ValueError('TITLE_COVER_CHANGED_AFTER_PLAN')
    from .script_language import check
    check(dict(text=cover['text'], language=cover['language']), plan['target_language'], 'TITLE')
    for evidence in cover['region']['evidence']:
        if sha(evidence['path']) != evidence['sha256']:
            raise ValueError('TITLE_COVER_EVIDENCE_CHANGED')


def raster(plan, folder):
    """Fit downward inside the exact source rectangle; never enlarge the cover."""
    validate(plan)
    cover = plan['title_blur_white_cover']; visual = plan['visual']; w, h = visual['canvas']
    l, t, r, b = [round(v*s) for v, s in zip(cover['bbox'], [w, h, w, h])]
    pad = max(3, round(w/180)); style = visual['title']; text = cover['text'].strip()
    words = text.split(); choices = [[text]] + [[' '.join(words[:i]), ' '.join(words[i:])] for i in range(1, len(words))]
    # Same verified font. Dark lettering needs no thick outline against white.
    scale = min(w/1080, h/1920); maximum = round(style['font_size']*scale)
    for size in range(maximum, round(30*scale)-1, -1):
        font, font_path, digest = load_font(style, size, text)
        for lines in choices:
            boxes = [font.getbbox(line) for line in lines]; advance = round(size*style['line_spacing_ratio'])
            top = min(z[1]+i*advance for i, z in enumerate(boxes)); bottom = max(z[3]+i*advance for i, z in enumerate(boxes))
            if max(z[2]-z[0] for z in boxes) > r-l-2*pad or bottom-top > b-t-2*pad:
                continue
            im = Image.new('RGBA', (w, h)); draw = ImageDraw.Draw(im)
            draw.rectangle((l, t, r-1, b-1), fill='#FFFFFF')
            y = t+(b-t-bottom+top)/2-top
            for i, (line, z) in enumerate(zip(lines, boxes)):
                draw.text((l+(r-l-z[2]+z[0])/2-z[0], y+i*advance), line, font=font, fill='#171717')
            path = Path(folder)/'title_blur_white_cover.png'; im.save(path)
            return dict(layer=str(path), layer_sha256=sha(path), bbox=[l,t,r,b], text=text,
                        font_file=font_path, font_sha256=digest, font_loaded=style['font_name'],
                        font_size=size, reference_font_size=style['font_size'], font_fallback=False,
                        lines=lines, background='#FFFFFF', foreground='#171717', strategy=VERSION)
    raise ValueError('TITLE_COVER_TEXT_DOES_NOT_FIT')


def apply_layers(plan, evidence, folder):
    if not plan.get('title_blur_white_cover'):
        return
    layer = raster(plan, folder); intervals = plan['title_blur_white_cover']['intervals']
    # Remove the ordinary Hook only during cover intervals. Source cards starting
    # after the Hook do not accidentally remove an unrelated opening title.
    rows = []
    for row in evidence['text_layers']:
        if row['kind'] != 'title':
            rows.append(row); continue
        ranges = [[row['start'], row['end']]]
        for a, b in intervals:
            ranges = [[x,y] for lo,hi in ranges for x,y in [(lo,min(hi,a)),(max(lo,b),hi)] if x < y]
        rows.extend(dict(row, start=a, end=b) for a,b in ranges)
    covers = [dict(layer, kind=VERSION, start=a, end=b) for a,b in intervals]
    evidence['text_layers'] = covers + rows
    evidence['title_blur_white_cover'] = dict(layer, intervals=intervals,
                                           timing_reason=plan['title_blur_white_cover']['timing_reason'])


def request_repair(manager, job_id, rank, region):
    """Explicit current-item repair. Existing content approvals remain mandatory."""
    from .item_review import get_item
    from .production_policy import approved_content
    from app.s3.headless_runtime import read
    store = manager.store; rank = f'{int(rank):02}'
    with store.lock, manager.lock:
        job, item = get_item(store, job_id, rank)
        if job.get('deleted_at') or not approved_content(item):
            raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
        key = fingerprint(region)
        if item.get('title_cover_repair_key') == key:
            return
        if item['status'] != 'PASS' or (job_id, rank) in manager.item_pending:
            raise ValueError('STATE_TRANSITION_REJECTED')
        plan = read(store.home/'jobs'/job_id/'work/plans'/f'{rank}.json')
        if not bind(plan, region):
            raise ValueError('TITLE_COVER_UNSAFE_REGION')
        validate(plan)
        store.item(job_id, rank, title_cover_region=region, title_cover_repair_key=key)
        manager.enqueue_item(job_id, rank, VERSION)


def resume_repair(manager, job_id, rank):
    from .item_review import get_item, audit
    from app.s3.headless_runtime import read, write, Harness
    from app.s3.overlay_policy import lock_plan
    store = manager.store; job, item = get_item(store, job_id, rank)
    work = store.home/'jobs'/job_id/'work'; path = work/'plans'/f'{rank}.json'
    try:
        manager.check(job_id); plan = read(path)
        # Archive the exact old plan; re-render from source, never stack over the
        # already burned-in title. No ASR/copy/TTS path is reachable here.
        archive = work/'title_cover'/rank; archive.mkdir(parents=True, exist_ok=True)
        write(archive/f'plan-before-{item.get("render_revision",0)}.json', plan)
        if not bind(plan, item['title_cover_region']):
            raise ValueError('TITLE_COVER_UNSAFE_REGION')
        validate(plan)
        revision = item.get('render_revision', 0)+1
        output = Path(job['input_path'])/'outputs'/f'AutoCut_{job_id[:8]}_{rank}_{plan["target_language"]}_r{revision}.mp4'
        while output.exists():
            revision += 1; output = output.with_name(f'AutoCut_{job_id[:8]}_{rank}_{plan["target_language"]}_r{revision}.mp4')
        plan['output'] = str(output); lock_plan(plan); write(path, plan)
        store.item(job_id, rank, status='EDITING', render_revision=revision, issues=[])
        store.update(job_id, state='RUNNING', stage='剪辑')
        with Harness(work, render_workers=1) as harness:
            manager.finish_plans(job_id, harness, job, [plan], work)
        audit(store, job_id, rank, VERSION, output=str(output), cover=plan['title_blur_white_cover'])
    except Exception:
        store.item(job_id, rank, status='FAILED', issues=['TITLE_COVER_REPAIR_FAILED'])
        raise
    finally:
        rows = store.get(job_id)['items']
        store.update(job_id, state='COMPLETED' if all(x['status']=='PASS' for x in rows) else 'PARTIAL_COMPLETED' if any(x['status']=='PASS' for x in rows) else 'FAILED', stage='检查')
