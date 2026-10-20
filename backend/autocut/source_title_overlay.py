"""Explicit current-task white caption replacement and sticker avoidance.

The original card geometry is an upper bound, never permission to paint a new
strip over footage. All title wording still comes from the real approval gate.
"""
import copy
import json
from functools import lru_cache
from pathlib import Path

POLICY = 'WHITE_CARD_TITLE_AVOID_STICKERS_V1'


def enabled(values):
    return values.get('source_title_overlay_policy') == POLICY


def snapshot(result, data):
    if not enabled(data):return result
    result = copy.deepcopy(result)
    result.setdefault('resolved_overrides_json', {}).setdefault('values', {})['source_title_overlay_policy'] = POLICY
    result['instruction_summary'] += ['本任务：白底原字幕按出现区间持续用白底标题覆盖；普通标题前5秒，避开原贴图']
    return result


def overlap(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def map_regions(plan, kinds):
    from .original_text_cover import map_interval, canvas_box
    result = []
    for region in plan.get('subject_protection', {}).get('protected_regions', []):
        if region['kind'] not in kinds:continue
        start,end=region['start'],region['end']
        # Analysis also returns individual observed frames. A point is one
        # frame of evidence, not an invalid interval or an invented lifetime.
        if start==end:
            start=max(0,min(start,plan['source']['duration']-1/30))
            end=min(plan['source']['duration'],start+1/30)
        for lo, hi in map_interval(plan, start, end):
            result.append(dict(kind=region['kind'], bbox=canvas_box(plan, region['bbox']), start=lo, end=hi))
    return result


def clear(layer, regions):
    return all(max(layer['start'], r['start']) >= min(layer['end'], r['end'])
               or not overlap(layer['bbox'], r['bbox']) for r in regions)


def ordinary_intervals(plan):
    ranges = [[plan['visual']['title']['start'], plan['visual']['title']['end']]]
    if plan.get('title_cover_ordinary_layout'):return ranges
    for a, b in plan.get('title_blur_white_cover', {}).get('intervals', []):
        ranges = [[x, y] for lo, hi in ranges for x, y in [(lo, min(hi, a)), (max(lo, b), hi)] if x < y]
    return ranges


def bind(plan, folder):
    if not enabled(plan.get('task_overrides', {})):return
    from app.s3.visual_preset import text_layer, load_preset
    from .title_blur_cover import raster
    folder = Path(folder) / 'source-title-layout' / plan['rank'];folder.mkdir(parents=True, exist_ok=True)
    graphics = map_regions(plan, {'sticker'})
    cover = plan.get('title_blur_white_cover')
    if cover:
        plan.setdefault('title_cover_warnings', []).extend(w for w in cover.get('warnings', [])
            if w not in plan.get('title_cover_warnings', []))
        layer = raster(plan, folder)
        if layer.get('mask_only'):
            plan['title_cover_ordinary_layout'] = True
            plan.setdefault('title_cover_warnings', []).append(layer['warning'])
        if any(not clear(dict(layer, start=a, end=b), graphics) for a, b in cover['intervals']):
            raise ValueError('TITLE_STICKER_LAYOUT_REQUIRED')
    ranges = ordinary_intervals(plan)
    style = plan['visual']['title'];original = style['anchor_y']
    subjects = map_regions(plan, {'sticker', 'face', 'hands', 'product'})
    if ranges and plan.get('title_enabled', True):
        # Exhaust a bounded set of vertical positions; do not hide the title,
        # shorten its five seconds, crop the source, or move a sticker.
        positions = sorted({original, *[n / 100 for n in range(10, 66, 2)]}, key=lambda y: abs(y-original))
        alternatives=[]
        for y in positions:
            style['anchor_y'] = y
            layer = text_layer(style['text'], style, plan['visual']['canvas'], folder/'ordinary-title.png')
            if all(clear(dict(layer, start=a, end=b), subjects) for a, b in ranges):break
            if all(clear(dict(layer,start=a,end=b),graphics) for a,b in ranges):
                l,t,r,bottom=layer['bbox']
                score=sum(max(0,min(r,s['bbox'][2])-max(l,s['bbox'][0]))*
                    max(0,min(bottom,s['bbox'][3])-max(t,s['bbox'][1]))*
                    max(0,min(hi,s['end'])-max(lo,s['start'])) for lo,hi in ranges for s in subjects)
                alternatives.append((score,abs(y-original),y))
        else:
            if not alternatives:
                style['anchor_y'] = original
                raise ValueError('TITLE_STICKER_LAYOUT_REQUIRED')
            style['anchor_y']=min(alternatives)[2]
            plan.setdefault('title_cover_warnings',[]).append('TITLE_SUBJECT_LAYOUT_REQUIRES_VISUAL_QA')
        if style['anchor_y'] != original:
            default = load_preset()['title']
            plan['visual']['visual_preset_override'] = True
            plan['visual']['source_title_overlay_policy'] = POLICY
            plan['visual']['overrides'] = [o for o in plan['visual']['overrides'] if o['layer'] != 'title']
            plan['visual']['overrides'].append(dict(layer='title', override_reason='SOURCE_TEXT_CONFLICT',
                default_position=[default['anchor_x'], default['anchor_y']], chosen_position=[style['anchor_x'], style['anchor_y']]))
    plan['overlay_analysis']['source_title_overlay'] = dict(policy=POLICY, graphics=graphics,
        ordinary_intervals=ranges, title_anchor_y=style['anchor_y'], sticker_check='PASS',
        warnings=list(plan.get('title_cover_warnings', [])))
    if plan.get('resolved_parameters'):
        plan['resolved_parameters']['title_y'] = dict(value=style['anchor_y'], source=POLICY)


def title_limits(visual, layer, limits):
    if layer == 'title' and enabled(visual):return dict(limits, y=[.10, .65])
    return limits


@lru_cache(maxsize=4096)
def _project_title(text, source_json, region_json, task_json, language):
    from app.s3.visual_preset import load_preset
    from .source_frame import bind as frame_bind
    from .title_blur_cover import bind as cover_bind, raster
    from .title_layout import apply
    source=json.loads(source_json);duration=source['duration'];source['source_end']=duration
    preset=load_preset();visual=dict(canvas=[1080,1920],title=copy.deepcopy(preset['title']),preset_snapshot=preset)
    apply(visual,text,max_lines=2)
    plan=dict(source=source,source_ranges=[[0,duration]],video_speed=1.,target_duration=duration,
              title=text,final_title_language=language,target_language=language,visual=visual,
              task_overrides=json.loads(task_json))
    frame_bind(plan)
    if not cover_bind(plan,json.loads(region_json)):
        return dict(status='WARNING',warning='TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED')
    return raster(plan,None)


def project_title(job, item, text, task):
    region=(item.get('analysis') or {}).get('title_blur_region')
    if not enabled(task) or not region:return None
    source=item.get('source_snapshot')
    if not source:return dict(status='WARNING',warning='TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED')
    from .script_language import target_language
    try:
        return copy.deepcopy(_project_title(text,json.dumps(source,sort_keys=True),
            json.dumps(region,sort_keys=True),json.dumps(task,sort_keys=True),target_language(job)))
    except (ValueError,KeyError,TypeError) as error:
        return dict(status='WARNING',warning=str(error) if isinstance(error,ValueError) else 'TITLE_SOURCE_OVERLAY_REVIEW_REQUIRED')


def verify_layers(plan, evidence):
    if not enabled(plan.get('task_overrides', {})):return
    graphics = map_regions(plan, {'sticker'})
    guard = plan['overlay_analysis'].get('source_title_overlay')
    if not guard or guard['graphics'] != graphics:raise ValueError('TITLE_STICKER_EVIDENCE_REQUIRED')
    for row in evidence['text_layers']:
        if row['kind'] not in {'title', 'TITLE_BLUR_WHITE_COVER'}:continue
        if not clear(row, graphics):raise ValueError('TITLE_STICKER_LAYOUT_REQUIRED')
    evidence['source_title_overlay'] = copy.deepcopy(guard)
