"""Narrow, evidence-bound replacement of a damaged or opaque white title card.

No source pixels, approved copy, audio or ordinary title preset are modified.
White background and lettering are one timed raster, never separate layers.
"""
import copy, math
from pathlib import Path
from .file_storage import output_directory
from PIL import Image, ImageDraw
from app.s3.headless_runtime import sha, fingerprint
from app.s3.visual_preset import load_font

VERSION = 'TITLE_BLUR_WHITE_COVER'


def rectangle(value, caption_policy=False):
    if (not isinstance(value, list) or len(value) != 4
            or not all(type(v) in (int, float) and math.isfinite(v) for v in value)):
        raise ValueError('TITLE_COVER_INVALID_REGION')
    l, t, r, b = value
    if not (0 <= l < r <= 1 and 0 <= t < b <= (1 if caption_policy else .33)
            and r-l <= (.90 if caption_policy else .72) and b-t <= (.25 if caption_policy else .12)):
        raise ValueError('TITLE_COVER_UNSAFE_REGION')
    return value


def intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def prepare(plan, region):
    """Map detected SOURCE timing through actual cuts/speed; abstain when unsafe."""
    from .source_title_overlay import enabled
    caption_policy = enabled(plan.get('task_overrides', {}))
    if not region or not plan.get('title_enabled', True) or (plan.get('montage_segments') and not caption_policy):
        return None
    intact_card = region.get('kind') == 'INTACT_TITLE' and region.get('background') == 'WHITE_CARD'
    if not intact_card and (region.get('kind') != 'BLURRED_TITLE' or region.get('background') not in {'WHITE_CARD', 'BACKGROUND'}):
        return None  # Colored cards and titles without an opaque card keep their policy.
    # For the explicitly requested caption replacement, model confidence is
    # advisory. Geometry, source identity, evidence and rendered QA still apply.
    if not caption_policy and (region.get('safe_to_cover') is not True or region.get('confidence', 0) < .9):
        return None
    if region.get('source_sha256') != plan['source']['sha256'] or not region.get('evidence'):
        raise ValueError('TITLE_COVER_EVIDENCE_REQUIRED')
    if caption_policy and region['background'] != 'WHITE_CARD':
        return None
    box = rectangle(region.get('bbox'), caption_policy)
    lo, hi = region['source_interval']
    if not (all(type(v) in (int, float) and math.isfinite(v) for v in (lo, hi))
            and 0 <= lo < hi <= plan['source']['duration'] + .001):
        raise ValueError('TITLE_COVER_INVALID_TIME')
    # White-card replacement uses exactly the already occupied rectangle: no
    # padding may grow onto a face/product. A blur over real scenery needs clear
    # subject separation, including all time-overlapping protected regions.
    if region['background'] == 'WHITE_CARD':
        if rectangle(region.get('original_card_bbox'), caption_policy) != box:
            raise ValueError('TITLE_COVER_CARD_EXPANSION')
    else:
        subjects = plan.get('subject_protection', {}).get('protected_regions') or []
        if not subjects:
            return None
        for subject in subjects:
            if subject['start'] <= hi and subject['end'] >= lo and intersects(box, subject['bbox']):
                return None
    # A source white strip must never outlive its translated replacement, even
    # when the normal Hook duration is shorter than the original card.
    persistent = intact_card or region.get('persistent_source_card') is True or caption_policy
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
    if plan.get('montage_segments'):
        # SOURCE intervals belong only to matching source hashes. Never let a
        # main-source cover spill onto an appended donor or use overall speed.
        from .original_text_cover import map_interval
        intervals = map_interval(plan,lo,hi)
    if not intervals:
        return None
    return dict(strategy=VERSION, source_sha256=region['source_sha256'], bbox=box,
                intervals=intervals, region=copy.deepcopy(region), fill='#FFFFFF',
                text=plan['title'], language=plan['final_title_language'],
                timing_reason='PERSISTENT_SOURCE_CARD_REPLACEMENT' if persistent else 'HOOK_ONLY',
                **(dict(warnings=['SOURCE_CARD_MODEL_UNCERTAINTY']) if caption_policy and
                   (region.get('safe_to_cover') is not True or region.get('confidence',0)<.9) else {}))


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
    from .source_frame import canvas_box as fitted_box
    l, t, r, b = fitted_box(plan, cover['bbox']) or [round(v*s) for v, s in zip(cover['bbox'], [w, h, w, h])]
    pad = max(3, round(w/180)); style = visual['title']; text = cover['text'].strip()
    ink = '#000000' if cover['region']['kind'] == 'INTACT_TITLE' else '#171717'
    from .source_title_overlay import enabled
    caption_policy = enabled(plan.get('task_overrides', {}))
    words = text.split(); choices = ([] if caption_policy else [[text]]) + [[' '.join(words[:i]), ' '.join(words[i:])] for i in range(1, len(words))]
    # Same verified font. Dark lettering needs no thick outline against white.
    scale = min(w/1080, h/1920); maximum = round(style['font_size']*scale)
    for size in range(maximum, round((48 if caption_policy else 30)*scale)-1, -1):
        font, font_path, digest = load_font(style, size, text)
        for lines in choices:
            boxes = [font.getbbox(line) for line in lines]; advance = round(size*style['line_spacing_ratio'])
            top = min(z[1]+i*advance for i, z in enumerate(boxes)); bottom = max(z[3]+i*advance for i, z in enumerate(boxes))
            if max(z[2]-z[0] for z in boxes) > r-l-2*pad or bottom-top > b-t-2*pad:
                continue
            if folder is None:
                return dict(lines=lines, font_size=size, bbox=[l,t,r,b],
                            **(dict(warning=cover['warnings'][0]) if cover.get('warnings') else {}))
            im = Image.new('RGBA', (w, h)); draw = ImageDraw.Draw(im)
            draw.rectangle((l, t, r-1, b-1), fill='#FFFFFF')
            y = t+(b-t-bottom+top)/2-top
            for i, (line, z) in enumerate(zip(lines, boxes)):
                draw.text((l+(r-l-z[2]+z[0])/2-z[0], y+i*advance), line, font=font, fill=ink)
            path = Path(folder)/'title_blur_white_cover.png'; im.save(path)
            return dict(layer=str(path), layer_sha256=sha(path), bbox=[l,t,r,b], text=text,
                        font_file=font_path, font_sha256=digest, font_loaded=style['font_name'],
                        font_size=size, reference_font_size=style['font_size'], font_fallback=False,
                        lines=lines, background='#FFFFFF', foreground=ink, strategy=VERSION)
    if caption_policy:
        # Keep the exact white mask while the approved two-line Hook uses the
        # ordinary layout. Never shrink below 48px or enlarge onto the footage.
        result = dict(bbox=[l,t,r,b], text='', lines=[], mask_only=True,
                      warning='TITLE_COVER_TEXT_USES_ORDINARY_LAYOUT', strategy=VERSION,
                      background='#FFFFFF')
        if folder is not None:
            im=Image.new('RGBA',(w,h));ImageDraw.Draw(im).rectangle((l,t,r-1,b-1),fill='#FFFFFF')
            path=Path(folder)/'title_blur_white_cover.png';im.save(path)
            result.update(layer=str(path),layer_sha256=sha(path))
        return result
    raise ValueError('TITLE_COVER_TEXT_DOES_NOT_FIT')


def apply_layers(plan, evidence, folder):
    from .source_title_overlay import verify_layers
    if not plan.get('title_blur_white_cover'):
        verify_layers(plan, evidence)
        return
    layer = raster(plan, folder); intervals = plan['title_blur_white_cover']['intervals']
    # Remove the ordinary Hook only during cover intervals. Source cards starting
    # after the Hook do not accidentally remove an unrelated opening title.
    rows = []
    for row in evidence['text_layers']:
        if row['kind'] != 'title' or layer.get('mask_only'):
            rows.append(row); continue
        ranges = [[row['start'], row['end']]]
        for a, b in intervals:
            ranges = [[x,y] for lo,hi in ranges for x,y in [(lo,min(hi,a)),(max(lo,b),hi)] if x < y]
        rows.extend(dict(row, start=a, end=b) for a,b in ranges)
    covers = [dict(layer, kind=VERSION, start=a, end=b) for a,b in intervals]
    evidence['text_layers'] = covers + rows
    evidence['title_blur_white_cover'] = dict(layer, intervals=intervals,
                                           timing_reason=plan['title_blur_white_cover']['timing_reason'])
    verify_layers(plan, evidence)


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
            # Older repairs consumed the original export's budget. Resume only
            # that specific pre-render failure, keeping its preview and plan.
            if (item['status'] == 'FAILED'
                    and item.get('issues') == ['FULL_RENDER_BUDGET_OR_REASON_REJECTED']
                    and (job_id, rank) not in manager.item_pending):
                store.item(job_id, rank, title_cover_budget_resume=True)
                manager.enqueue_item(job_id, rank, VERSION)
            return
        if item['status'] != 'PASS' or (job_id, rank) in manager.item_pending:
            raise ValueError('STATE_TRANSITION_REJECTED')
        plan = read(store.home/'jobs'/job_id/'work/plans'/f'{rank}.json')
        if not bind(plan, region):
            raise ValueError('TITLE_COVER_UNSAFE_REGION')
        validate(plan)
        store.item(job_id, rank, title_cover_region=region, title_cover_repair_key=key)
        manager.enqueue_item(job_id, rank, VERSION)


def revision_budget(work, rank, key):
    """One bounded render allowance per explicit title revision, not per retry."""
    from app.s3.headless_runtime import read, write, locked
    budget = work/'budgets'/f'render_{fingerprint(str(rank))}.json'
    archive = work/'title_cover'/rank/key/'render-budget-before.json'
    marker = archive.with_name('render-budget-initialized.json')
    with locked(budget.with_suffix('.lock')):
        if marker.exists():
            return
        if not archive.exists():
            write(archive, read(budget, {}))
        # Persist the guard first: a crash cannot grant an extra retry budget.
        write(marker, dict(repair_key=key))
        write(budget, dict(full_render_count=0, reason=VERSION, repair_key=key))


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
        if plan.get('task_overrides',{}).get('source_title_overlay_policy'):
            from .source_title_overlay import bind as bind_source_title
            plan['title_cover_warnings']=[w for w in plan.get('title_cover_warnings',[]) if w!='TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED']
            plan.pop('title_cover_note',None)
            bind_source_title(plan,work)
        if item.get('title_cover_budget_resume') and not Path(plan['output']).exists():
            revision = item['render_revision']; output = Path(plan['output'])
        else:
            revision = item.get('render_revision', 0)+1
            output = output_directory(job)/f'AutoCut_{job_id[:8]}_{rank}_{plan["target_language"]}_r{revision}.mp4'
            while output.exists():
                revision += 1; output = output.with_name(f'AutoCut_{job_id[:8]}_{rank}_{plan["target_language"]}_r{revision}.mp4')
        plan['output'] = str(output); lock_plan(plan); write(path, plan)
        revision_budget(work, rank, item['title_cover_repair_key'])
        store.item(job_id, rank, status='EDITING', render_revision=revision, issues=[], title_cover_budget_resume=False)
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
