"""Effect-first Headless policy. Visual observations are supplied by the editor.

This module routes and validates; it never invents a visual score, infers rights,
launches a model during planning, or changes ASR/TTS/render settings.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

EFFECT_FIRST_OVERLAY_ROUTER = True
ALLOW_EXPERIMENTAL_PROPAINTER = True  # effective only inside RESEARCH_SCOPES
POLICY_VERSION = 1
RESEARCH_SCOPES = {'training_research', 'internal_training_research'}
CLASSES = {'PLAIN_TEXT', 'WHITE_CARD', 'COLOR_CARD', 'COMMENT_CARD', 'POV_CARD',
           'SEMI_TRANSPARENT_CARD', 'CORNER_WATERMARK', 'STATIC_WATERMARK',
           'STATIC_OVER_TEXTURE', 'MOVING_OVER_TEXTURE', 'PRODUCT_BRANDING', 'UNKNOWN'}
CARDS = {'WHITE_CARD', 'COLOR_CARD', 'COMMENT_CARD', 'POV_CARD', 'SEMI_TRANSPARENT_CARD'}
ARTIFACTS = {'TEMPORAL_INPAINT_ARTIFACT', 'SMEARING', 'TEXT_GHOSTING', 'TEXT_RESIDUE',
             'BACKGROUND_BLUR', 'REPEATED_TEXTURE', 'PATCH_EDGE_VISIBLE'}
QA_RULES = ARTIFACTS | {'OVERLAY_AREA_MISMATCH', 'OVERLAY_BOUNDARY_MISMATCH',
                      'GENERIC_LARGE_COVER_USED', 'CARD_STYLE_MISMATCH',
                      'PROPAINTER_MASK_TOO_LARGE', 'PROPAINTER_SUBJECT_DAMAGE',
                      'MOVING_MASK_REQUIRED', 'TEXT_FIT_REVIEW'}
# Higher is better; these are editor assessments, not automatic aesthetic metrics.
QUALITY = ('BACKGROUND_CONTINUITY', 'CLEAN_REMOVAL', 'TEMPORAL_STABILITY',
           'EDGE_MATCH', 'LAYOUT_MATCH', 'TEXT_RESIDUE', 'PRODUCT_PRESERVATION')
FINAL_SAMPLES = (0, .10, .20, .30, .45, .60, .75, .88, .96)
METHODS = {'KEEP_ORIGINAL_CARD_REPLACE_TEXT', 'EXACT_CARD_REBUILD', 'CROP',
           'PRECISE_BACKGROUND_PATCH', 'PRECISE_INPAINT', 'EXPERIMENTAL_HARD_INPAINT', 'KEEP'}


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def experimental_allowed(scope):
    return ALLOW_EXPERIMENTAL_PROPAINTER and scope in RESEARCH_SCOPES


def bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError('OVERLAY_BBOX_REQUIRED')
    if any(type(n) not in (int, float) or not math.isfinite(n) for n in value):
        raise ValueError('OVERLAY_BBOX_INVALID')
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0:
        raise ValueError('OVERLAY_BBOX_INVALID')
    return x, y, w, h


def geometry(original, replacement):
    x, y, w, h = bbox(original); a, b, c, d = bbox(replacement)
    ratio = c*d/(w*h)
    errors = [abs(x-a), abs(y-b), abs(x+w-a-c), abs(y+h-b-d)]
    issues = []
    if ratio < .95 or ratio > 1.08: issues.append('OVERLAY_AREA_MISMATCH')
    if max(errors) > 4: issues.append('OVERLAY_BOUNDARY_MISMATCH')
    return dict(area_ratio=ratio, edge_errors_px=errors, issues=issues,
                target_met=.95 <= ratio <= 1.05 and max(errors) <= 2)


def route(region, scope='production'):
    """Return the next preview action, never a final render or model dispatch."""
    cls = region.get('overlay_class', 'UNKNOWN')
    if cls not in CLASSES or cls == 'UNKNOWN':
        return dict(action='REVIEW_REQUIRED', reason='OVERLAY_CLASSIFICATION_REQUIRED')
    if cls == 'PRODUCT_BRANDING':
        return dict(action='KEEP', reason='PRODUCT_BRANDING_PRESERVED')
    if cls == 'MOVING_OVER_TEXTURE' or region.get('moving'):
        # The installed backend supports only STATIC_BOX. Do not invent tracking.
        return dict(action='REVIEW_REQUIRED', reason='MOVING_MASK_REQUIRED')
    if cls in CARDS:
        action = 'KEEP_ORIGINAL_CARD_REPLACE_TEXT' if region.get('card_intact') and not region.get('text_separation_failed_reason') else 'EXACT_CARD_REBUILD'
        return dict(action=action, preview_required=True, reason='PRESERVE_CARD_GEOMETRY_AND_STYLE')
    if cls == 'CORNER_WATERMARK' and region.get('safe_crop') is True and 0 < region.get('crop_loss_ratio', 1) <= .04:
        return dict(action='CROP', preview_required=True, reason='SMALL_SAFE_EDGE_CROP')
    if any(c.get('strategy') == 'EXPERIMENTAL_HARD_INPAINT' and c.get('issues') for c in region.get('candidates', [])):
        return dict(action='REVIEW_REQUIRED', reason='PROPAINTER_VISUAL_FAILED')
    failures = (set(region.get('light_preview_issues', [])) | {i for c in region.get('candidates', []) if c.get('strategy') != 'EXPERIMENTAL_HARD_INPAINT' for i in c.get('issues', [])}) & ARTIFACTS
    complex_region = cls == 'STATIC_OVER_TEXTURE' or region.get('background_complex') is True
    if failures or complex_region:
        if not experimental_allowed(scope):
            return dict(action='REVIEW_REQUIRED', reason='RESEARCH_SCOPE_REQUIRED', issues=sorted(failures))
        return dict(action='EXPERIMENTAL_HARD_INPAINT', preview_required=True,
                    reason='LIGHT_PREVIEW_FAILED' if failures else 'COMPLEX_TEXTURE_PREVIEW_FIRST',
                    compare='SAME_SOURCE_TIME_REGION', issues=sorted(failures))
    return dict(action='PRECISE_BACKGROUND_PATCH', preview_required=True, reason='SIMPLE_BACKGROUND_FIRST')


def region_issues(region, strategy):
    issues = []
    cls = region.get('overlay_class', 'UNKNOWN')
    if cls not in CLASSES or cls == 'UNKNOWN': issues.append('OVERLAY_CLASSIFICATION_REQUIRED')
    if strategy not in METHODS: issues.append('GENERIC_LARGE_COVER_USED' if 'COVER' in strategy else 'UNKNOWN_OVERLAY_STRATEGY')
    if strategy=='KEEP' and cls!='PRODUCT_BRANDING': issues.append('TEXT_RESIDUE')
    if region.get('generic_large_cover'): issues.append('GENERIC_LARGE_COVER_USED')
    if cls == 'PRODUCT_BRANDING' and strategy != 'KEEP': issues.append('PRODUCT_BRANDING_MODIFIED')
    if cls == 'MOVING_OVER_TEXTURE' or region.get('moving'): issues.append('MOVING_MASK_REQUIRED')
    if cls in CARDS:
        if strategy not in {'KEEP_ORIGINAL_CARD_REPLACE_TEXT', 'EXACT_CARD_REBUILD'}:
            issues.append('CARD_STYLE_MISMATCH')
        if region.get('card_intact') and strategy == 'EXACT_CARD_REBUILD' and not region.get('text_separation_failed_reason'):
            issues.append('ORIGINAL_CARD_MUST_BE_PRESERVED')
        try:
            issues.extend(geometry(region['original_card_bbox'], region['replacement_bbox'])['issues'])
            cx, cy, cw, ch = bbox(region['original_card_bbox']); tx, ty, tw, th = bbox(region['original_text_bbox'])
            if not (cx <= tx < tx+tw <= cx+cw and cy <= ty < ty+th <= cy+ch):
                issues.append('OVERLAY_BOUNDARY_MISMATCH')
        except (KeyError, ValueError): issues.append('CARD_GEOMETRY_REQUIRED')
        if region.get('text_fits') is not True: issues.append('TEXT_FIT_REVIEW')
        original, replacement = region.get('original_style', {}), region.get('replacement_style', {})
        for key in ('background_color', 'opacity', 'corner_radius', 'border', 'shadow', 'text_alignment'):
            if key not in original or original.get(key) != replacement.get(key): issues.append('CARD_STYLE_MISMATCH')
        if 'padding' not in original or 'padding' not in replacement: issues.append('CARD_STYLE_MISMATCH')
    elif strategy != 'KEEP' and region.get('adds_background_card'):
        issues.append('GENERIC_LARGE_COVER_USED')
    if strategy in {'PRECISE_BACKGROUND_PATCH', 'PRECISE_INPAINT', 'EXPERIMENTAL_HARD_INPAINT'}:
        try:
            x,y,w,h = bbox(region['original_bbox']); a,b,c,d = bbox(region['mask_bbox'])
            if a < x-4 or b < y-4 or a+c > x+w+4 or b+d > y+h+4:
                issues.append('PROPAINTER_MASK_TOO_LARGE' if strategy == 'EXPERIMENTAL_HARD_INPAINT' else 'GENERIC_LARGE_COVER_USED')
            if not (a <= x and b <= y and a+c >= x+w and b+d >= y+h): issues.append('TEXT_RESIDUE')
        except (KeyError, ValueError): issues.append('PRECISE_MASK_REQUIRED')
    if strategy == 'EXPERIMENTAL_HARD_INPAINT':
        failed=set(region.get('light_preview_issues',[])) & ARTIFACTS
        failed|={i for c in region.get('candidates',[]) if c.get('strategy')!='EXPERIMENTAL_HARD_INPAINT' for i in c.get('issues',[]) if i in ARTIFACTS}
        if not (cls=='STATIC_OVER_TEXTURE' or region.get('background_complex') or failed): issues.append('PROPAINTER_NOT_NEEDED')
        if cls=='CORNER_WATERMARK' and region.get('safe_crop') and region.get('crop_loss_ratio',1)<=.04: issues.append('PROPAINTER_NOT_NEEDED')
    if strategy == 'CROP' and not (region.get('safe_crop') and 0 < region.get('crop_loss_ratio', 1) <= .04):
        issues.append('UNSAFE_CROP')
    return sorted(set(issues))


def evidence_hashes(evidence):
    return {key: dict(path=str(Path(path).resolve(strict=True)), sha256=sha(path)) for key, path in evidence.items()}


def visual_key(candidate):
    return tuple(candidate['visual_scores'][key] for key in QUALITY)


def choose(region, scope):
    """Lock only a visually reviewed, artifact-free candidate; runtime is ignored."""
    acceptable = []
    for c in region.get('candidates', []):
        if c.get('strategy') not in METHODS or region_issues(region, c['strategy']): continue
        if c['strategy'] == 'EXPERIMENTAL_HARD_INPAINT' and not experimental_allowed(scope): continue
        if set(c.get('issues', [])) or c.get('visual_pass') is not True: continue
        if c.get('reviewer') not in {'codex', 'human'} or not c.get('reviewed_at'): continue
        if c.get('temporal_playback_checked') is not True: continue
        scores = c.get('visual_scores', {})
        if any(type(scores.get(k)) not in (int, float) or not 0 <= scores[k] <= 5 for k in QUALITY): continue
        if scores['PRODUCT_PRESERVATION'] < 4 or min(scores.values()) < 3: continue
        window = c.get('preview_interval', [])
        if len(window) != 2 or not 2 <= window[1]-window[0] <= 5: continue
        start, end = region['interval']
        if max(start,window[0]) >= min(end,window[1]): continue
        required = {'before_crop', 'after_crop', 'mask_preview', 'preview_video'}
        if region['overlay_class'] in CARDS: required.add('card_geometry')
        if c['strategy'] == 'EXPERIMENTAL_HARD_INPAINT': required.add('propainter_preview')
        if not required <= c.get('evidence', {}).keys(): continue
        try: c = {**c, 'evidence_hashes': evidence_hashes(c['evidence'])}
        except (OSError, ValueError): continue
        acceptable.append(c)
    if not acceptable:
        return dict(status='REVIEW_REQUIRED', reason='NO_VISUALLY_ACCEPTABLE_PREVIEW', next=route(region,scope))
    # Compare like-for-like; mixing times hides temporal defects.
    if len({tuple(c['preview_interval']) for c in acceptable}) != 1:
        return dict(status='REVIEW_REQUIRED', reason='PREVIEW_COMPARISON_MISMATCH')
    winner = max(acceptable, key=lambda c: (visual_key(c), c['strategy'] != 'EXPERIMENTAL_HARD_INPAINT'))
    backend = {'EXPERIMENTAL_HARD_INPAINT':'propainter', 'KEEP_ORIGINAL_CARD_REPLACE_TEXT':'exact_card_replace',
               'EXACT_CARD_REBUILD':'exact_card_replace'}.get(winner['strategy'],winner['strategy'].lower())
    return dict(status='LOCKED', strategy=winner['strategy'], chosen_backend=backend,
                reason=winner.get('reason', 'Highest reviewed visual quality; runtime excluded'), preview_result=winner)


def plan_payload(plan):
    return dict(source=plan['source'], usage_scope=plan.get('usage_scope','production'),
                overlay_analysis=plan.get('overlay_analysis'), overlays=plan.get('overlays'),
                source_text_masks=plan.get('source_text_masks',[]), title=plan.get('title'),
                crop_override=plan.get('crop_override'), experimental_overlay=plan.get('experimental_overlay'),
                overlay_title_events=plan.get('overlay_title_events'), embedded_text_regions=plan.get('embedded_text_regions'),
                watermark_regions=plan.get('watermark_regions'), video_speed=plan.get('video_speed',1),
                target_duration=plan.get('target_duration'))


def lock_plan(plan):
    if plan.get('overlay_analysis', {}).get('status') != 'COMPLETE' or 'overlays' not in plan:
        raise ValueError('OVERLAY_CLASSIFICATION_REQUIRED')
    if len({r['id'] for r in plan['overlays']})!=len(plan['overlays']): raise ValueError('DUPLICATE_OVERLAY_ID')
    locked = []
    for r in plan['overlays']:
        bbox(r['original_bbox'])
        if any(c.get('source_sha256') != plan['source']['sha256'] or c.get('original_bbox') != r['original_bbox'] for c in r.get('candidates', [])):
            raise ValueError('PREVIEW_SOURCE_OR_REGION_MISMATCH')
        if r['overlay_class'] == 'PRODUCT_BRANDING':
            locked.append(dict(id=r['id'], status='LOCKED', strategy='KEEP', chosen_backend='keep',reason='PRODUCT_BRANDING_PRESERVED'))
            continue
        decision = choose(r, plan.get('usage_scope','production'))
        if decision['status'] != 'LOCKED': raise ValueError(f"{r['id']}: {decision['reason']}")
        if decision['chosen_backend']=='propainter':
            decision.update(backend='propainter',usage_scope='internal_training_research',experimental=True)
        locked.append(dict(id=r['id'], **decision))
    plan['overlay_policy'] = dict(version=POLICY_VERSION,effect_first=True,
                                 allow_experimental_propainter=experimental_allowed(plan.get('usage_scope')),
                                 fingerprint=fingerprint(dict(payload=plan_payload(plan), locked=locked)), regions=locked)
    return plan


def _render_gate(plan):
    issues=[];policy=plan.get('overlay_policy', {})
    if policy.get('version') != POLICY_VERSION or policy.get('effect_first') is not True:
        return ['OVERLAY_STRATEGY_NOT_LOCKED']
    if plan.get('overlay_analysis', {}).get('status') != 'COMPLETE': issues.append('OVERLAY_CLASSIFICATION_REQUIRED')
    if policy.get('fingerprint') != fingerprint(dict(payload=plan_payload(plan), locked=policy.get('regions',[]))): issues.append('OVERLAY_PLAN_CHANGED_AFTER_PREVIEW')
    regions={r['id']:r for r in plan.get('overlays',[])}
    if set(regions) != {r['id'] for r in policy.get('regions',[])}: issues.append('OVERLAY_REGION_MISMATCH')
    covered_masks=set()
    if plan.get('video_speed',1)!=1 and any('output_interval' not in r for r in regions.values()):
        issues.append('OVERLAY_OUTPUT_TIMING_REQUIRED')
    for field, refs in [('embedded_text_regions','legacy_text_indices'),('watermark_regions','legacy_watermark_indices')]:
        if {i for r in regions.values() for i in r.get(refs,[])} != set(range(len(plan.get(field,[])))):
            issues.append('OVERLAY_CLASSIFICATION_REQUIRED')
    if (plan.get('embedded_text_regions') or plan.get('watermark_regions')) and not regions:
        issues.append('OVERLAY_CLASSIFICATION_REQUIRED')
    if plan.get('crop_override',{}):
        crop=plan['crop_override']
        if crop.get('right_edge_percent',100)>4: issues.append('UNSAFE_CROP')
    for lock in policy.get('regions',[]):
        r=regions.get(lock['id'], {})
        issues.extend(region_issues(r,lock['strategy']))
        covered_masks.update(r.get('render_mask_indices',[]))
        for index in r.get('render_mask_indices',[]):
            masks=plan.get('source_text_masks',[])
            if type(index) is not int or not 0 <= index < len(masks):
                issues.append('UNCLASSIFIED_RENDER_MASK'); continue
            mask=masks[index]
            if r.get('coordinate_space') != 'output_pixels':
                issues.append('RENDER_MASK_COORDINATES_UNVERIFIED'); continue
            if [mask.get(k) for k in ('x','y','w','h')] != r.get('mask_bbox'):
                issues.append('RENDER_MASK_GEOMETRY_MISMATCH')
            if lock['strategy'] not in {'PRECISE_BACKGROUND_PATCH','PRECISE_INPAINT'}:
                replaced=plan.get('experimental_overlay',{}).get('replaces_mask_indices',[])
                if index not in replaced: issues.append('UNSELECTED_LIGHTWEIGHT_MASK_STILL_ACTIVE')
        if lock['chosen_backend']=='propainter' and not experimental_allowed(plan.get('usage_scope')): issues.append('RESEARCH_SCOPE_REQUIRED')
        for e in lock.get('preview_result',{}).get('evidence_hashes',{}).values():
            try:
                if sha(e['path']) != e['sha256']: issues.append('OVERLAY_PREVIEW_CHANGED')
            except OSError: issues.append('OVERLAY_PREVIEW_MISSING')
    if any(r.get('chosen_backend')=='propainter' for r in policy.get('regions',[])) and not plan.get('experimental_overlay'):
        issues.append('OVERLAY_BACKEND_NOT_BOUND')
    if plan.get('experimental_overlay'):
        # Existing installed Transport adapter consumes one full primary interval.
        # Never stretch a short-lived mask across that whole interval implicitly.
        selected=[regions[r['id']] for r in policy.get('regions',[]) if r.get('chosen_backend')=='propainter']
        if len(selected)!=1 or selected[0].get('interval') != [0,plan['source'].get('source_end')]:
            issues.append('OVERLAY_INTERVAL_COMPOSITION_REQUIRED')
    # Legacy rank-based card templates are not valid evidence for exact geometry.
    card_ids={r['id'] for r in plan.get('overlays',[]) if r.get('overlay_class') in CARDS}
    events=plan.get('overlay_title_events',[])
    if card_ids - {e.get('overlay_id') for e in events}: issues.append('EXACT_CARD_RENDER_ASSET_REQUIRED')
    for event in events:
        r=regions.get(event.get('overlay_id'))
        if not r: issues.append('UNCLASSIFIED_RENDER_OVERLAY'); continue
        try:
            lock=next(v for v in policy['regions'] if v['id']==r['id'])
            if event.get('sha256') not in lock.get('preview_result',{}).get('render_asset_sha256',[]):
                issues.append('OVERLAY_ASSET_NOT_PREVIEWED')
            if sha(event['path'])!=event['sha256']: issues.append('OVERLAY_RENDER_ASSET_CHANGED')
            if not 0<=event['start']<event['end']<=plan['target_duration']: issues.append('OVERLAY_WINDOW_INVALID')
            from PIL import Image
            with Image.open(event['path']) as im:
                if im.mode!='RGBA' or im.size!=(1080,1920): raise ValueError('OVERLAY_CANVAS_MISMATCH')
                bounds=im.getchannel('A').getbbox()
            if not bounds: raise ValueError('OVERLAY_EMPTY')
            x,y,right,bottom=bounds
            if r['overlay_class'] in CARDS:
                if r.get('coordinate_space')!='output_pixels': raise ValueError('RENDER_OVERLAY_COORDINATES_UNVERIFIED')
                lock=next(v for v in policy['regions'] if v['id']==r['id'])
                if lock['strategy']=='EXACT_CARD_REBUILD':
                    issues.extend(geometry(r['original_card_bbox'],[x,y,right-x,bottom-y])['issues'])
                else:
                    a,b,c,d=bbox(r['original_text_bbox'])
                    if x<a-4 or y<b-4 or right>a+c+4 or bottom>b+d+4: issues.append('ORIGINAL_CARD_MUST_BE_PRESERVED')
        except (OSError,KeyError,ValueError,StopIteration) as exc:
            issues.append(str(exc) if isinstance(exc,ValueError) else 'OVERLAY_RENDER_ASSET_INVALID')
    if covered_masks != set(range(len(plan.get('source_text_masks',[])))): issues.append('UNCLASSIFIED_RENDER_MASK')
    return sorted(set(issues))


def render_gate(plan):
    try:
        return _render_gate(plan)
    except (KeyError,TypeError,ValueError,OSError,IndexError):
        return ['OVERLAY_PLAN_INVALID']


def qa_status(issues):
    if not issues: return 'PASS'
    return 'FAIL' if set(issues) & (QA_RULES - {'MOVING_MASK_REQUIRED','TEXT_FIT_REVIEW'}) else 'REVIEW_REQUIRED'


def sample_times(duration, interval):
    start,end=interval
    return sorted({min(max(t,0), max(0,duration-.001)) for t in
                   [*(r*duration for r in FINAL_SAMPLES),start-.08,start+.08,(start+end)/2,end-.08]})


def final_visual_gate(plan, review):
    issues=render_gate(plan)
    if review.get('reviewer') not in {'codex','human'} or review.get('visual_pass') is not True:
        issues.append('FINAL_VISUAL_REVIEW_REQUIRED')
    issues.extend(review.get('issues',[]))
    required={round(plan['target_duration']*p,3) for p in FINAL_SAMPLES}
    seen={round(t,3) for t in review.get('sample_times',[])}
    if not required <= seen: issues.append('FINAL_VISUAL_SAMPLES_MISSING')
    if set(review.get('overlay_regions_checked',[])) != {r['id'] for r in plan['overlays']}:
        issues.append('OVERLAY_DENSE_REVIEW_REQUIRED')
    for r in plan['overlays']:
        needed={round(t,3) for t in sample_times(plan['target_duration'],r.get('output_interval',r['interval']))}
        actual={round(t,3) for t in review.get('overlay_sample_times',{}).get(r['id'],[])}
        if not needed <= actual: issues.append('OVERLAY_DENSE_REVIEW_REQUIRED')
    if review.get('temporal_playback_checked') is not True: issues.append('TEMPORAL_REVIEW_REQUIRED')
    try:
        if review.get('output_sha256')!=sha(plan['output']): issues.append('FINAL_REVIEW_OUTPUT_MISMATCH')
    except (OSError,KeyError): issues.append('FINAL_REVIEW_OUTPUT_REQUIRED')
    return sorted(set(issues))


def propainter_preview_spec(plan, region, interval, output_path):
    """Call the installed backend unchanged; historical evidence can skip a known-bad light pass."""
    if sha(plan['source']['path'])!=plan['source']['sha256']: raise ValueError('SOURCE_HASH_CHANGED')
    if route(region,plan.get('usage_scope'))['action'] != 'EXPERIMENTAL_HARD_INPAINT':
        raise ValueError('PROPAINTER_NOT_SELECTED')
    if len(interval)!=2 or interval[0]<0 or not 2 <= interval[1]-interval[0] <= 5:
        raise ValueError('LOCAL_PREVIEW_2_TO_5_SECONDS_REQUIRED')
    if max(interval[0],region['interval'][0]) >= min(interval[1],region['interval'][1]):
        raise ValueError('PREVIEW_NOT_REPRESENTATIVE')
    if region.get('coordinate_space','source_pixels') != 'source_pixels':
        raise ValueError('PROPAINTER_REQUIRES_SOURCE_PIXEL_COORDINATES')
    issues=region_issues(region,'EXPERIMENTAL_HARD_INPAINT')
    if issues: raise ValueError(', '.join(issues))
    mask=Path(region['mask_path']).resolve(strict=True);review=region.get('mask_review',{})
    if (review.get('reviewer') not in {'codex','human'} or review.get('status')!='PASS'
            or review.get('coverage_complete') is not True or review.get('subject_safe') is not True
            or review.get('mask_sha256')!=sha(mask) or review.get('source_sha256')!=plan['source']['sha256']
            or not Path(review.get('preview_path','')).is_file()):
        raise ValueError('MASK_EDGE_REVIEW_REQUIRED')
    from PIL import Image
    with Image.open(mask) as im:
        if im.format!='PNG': raise ValueError('MASK_PNG_REQUIRED')
        bounds=im.convert('L').point(lambda p:255 if p>127 else 0).getbbox()
    if not bounds: raise ValueError('MASK_EMPTY')
    a,b,c,d=bounds
    if list(region['mask_bbox'])!=[a,b,c-a,d-b]: raise ValueError('MASK_GEOMETRY_MISMATCH')
    failures=set(region.get('light_preview_issues',[])) & ARTIFACTS
    failures|={i for c in region.get('candidates',[]) if c.get('strategy')!='EXPERIMENTAL_HARD_INPAINT' for i in c.get('issues',[]) if i in ARTIFACTS}
    basis='current_preview'
    if not failures:
        # Reuse a real previous failure; never label an unrun current preview FAIL.
        history=Path(region.get('historical_lightweight_failure',''))
        if not history.is_file(): raise ValueError('HISTORICAL_FAILURE_REFERENCE_REQUIRED_TO_SKIP_LIGHT_PREVIEW')
        record=json.loads(history.read_text())
        if (record.get('reviewer') not in {'codex','human'} or not set(record.get('issues',[])) & ARTIFACTS
                or not region.get('historical_match_reason') or not record.get('evidence')):
            raise ValueError('HISTORICAL_FAILURE_EVIDENCE_INVALID')
        evidence_hashes(record['evidence']);basis='historical_reference'
    mx,my,mw,mh=region['mask_bbox']
    return dict(input_video=plan['source']['path'],output_path=str(output_path),start_time=interval[0],end_time=interval[1],
                bbox=[int(mx),int(my),int(mw),int(mh)],mask_path=str(mask),context_pixels=region.get('context_pixels',48),
                device='mps',backend='propainter',experimental=True,usage_scope='internal_training_research',
                overlay_class='STATIC_OVER_TEXTURE',mask_mode='STATIC_BOX',lightweight_method_failed=True,
                lightweight_failure_basis=basis,light_preview_status='FAILED' if basis=='current_preview' else 'NOT_RUN',
                historical_reference=region.get('historical_lightweight_failure'),mask_review=review)
