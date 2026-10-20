"""Frozen source framing policies; only the background is cropped."""
import copy
import math
from fractions import Fraction

POLICY = 'WIDTH_FIT_BLUR_V1'
AUTO_POLICY = 'WIDTH_FIT_SLIGHT_NARROW_BLUR_V1'
DEFAULT_POLICY = 'SOURCE_FRAME_SLIGHT_NARROW_AUTO_V1'
HEIGHT_POLICY = 'HEIGHT_FIT_BLUR_EXCEPTION_V1'
AUTO_HEIGHT_POLICY = 'HEIGHT_FIT_SLIGHT_NARROW_BLUR_V1'
MAX_NARROW_DEFICIT = .01


def snapshot(value, data):
    out = copy.deepcopy(value)
    mode = data.get('source_frame_mode')
    if data.get('mode') not in {'Transport', 'Remix'}:
        if mode is not None:
            raise ValueError('INVALID_REQUEST')
        return out
    if mode not in {None, POLICY, AUTO_POLICY}:
        raise ValueError('INVALID_REQUEST')
    mode = AUTO_POLICY
    out.update(source_frame_mode=mode, source_frame_policy=DEFAULT_POLICY)
    out.setdefault('resolved_overrides_json', {}).setdefault('values', {})['source_frame_mode'] = mode
    for item in out['resolved_overrides_json'].get('per_item_overrides', []):
        if 'source_frame_mode' in item.get('values', {}):
            item['values']['source_frame_mode'] = mode
    out['instruction_summary'] = list(dict.fromkeys(out.get('instruction_summary', []) +
        ['搬运和混剪永久默认：完整保留画面；略窄于9:16时垂直铺满，两侧同帧模糊补齐；较宽画面左右贴边居中，上下同帧模糊补齐']))
    return out


def geometry(source, width, height, mode=POLICY):
    from app.s3.headless_media import probe
    from app.s3.headless_runtime import sha
    if mode not in {POLICY, AUTO_POLICY, HEIGHT_POLICY}:
        raise ValueError('SOURCE_FRAME_INVALID')
    if sha(source['path']) != source['sha256']:
        raise ValueError('SOURCE_HASH_CHANGED')
    video = next(s for s in probe(source['path'])['streams'] if s['codec_type'] == 'video')
    sw, sh = video['width'], video['height']
    sar = video.get('sample_aspect_ratio', '1:1')
    ratio = float(Fraction(sar.replace(':', '/'))) if sar not in ('N/A', '0:1') else 1.
    display_width, display_height = sw * ratio, sh
    rotation = float(video.get('tags', {}).get('rotate', 0))
    for side in video.get('side_data_list', []):
        rotation = float(side.get('rotation', rotation))
    if round(rotation) % 180:
        display_width, display_height = display_height, display_width
    aspect = display_width / display_height
    if not math.isfinite(aspect) or aspect <= 0:
        raise ValueError('SOURCE_FRAME_INVALID')
    if abs(aspect - width / height) < 1e-6:
        return None
    if aspect < width / height:
        slight = mode == AUTO_POLICY and aspect >= width / height * (1 - MAX_NARROW_DEFICIT) - 1e-12
        if mode == HEIGHT_POLICY or slight:
            fitted_width = max(2, min(width, math.floor(height * aspect / 2) * 2))
            return dict(foreground=[(width-fitted_width)/2, 0, fitted_width, height],
                        canvas=[width, height], policy=AUTO_HEIGHT_POLICY if slight else HEIGHT_POLICY)
        raise ValueError('SOURCE_FRAME_TOO_TALL')
    fitted_height = round((width / aspect) / 2) * 2
    return dict(foreground=[0, (height-fitted_height)/2, width, fitted_height],
                canvas=[width, height], policy=POLICY)


def filters(source, label, width, height, tag, mode):
    if mode is None:
        return None
    if mode not in {POLICY, AUTO_POLICY, HEIGHT_POLICY}:
        raise ValueError('SOURCE_FRAME_INVALID')
    layout = geometry(source, width, height, mode)
    if layout is None:
        return None
    fw, fh = layout['foreground'][2:]
    return (f'[{label}]scale=iw*sar:ih,setsar=1,split=2[{tag}b][{tag}f];'
            f'[{tag}b]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos:out_range=tv,'
            f'crop={width}:{height},gblur=sigma=20[{tag}bg];'
            f'[{tag}f]scale={fw}:{fh}:flags=lanczos:out_range=tv,setsar=1[{tag}fg];'
            f'[{tag}bg][{tag}fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1[{tag}out]')


def bind(plan):
    mode = (plan.get('task_overrides') or {}).get('source_frame_mode')
    if mode is None:
        return
    if mode not in {POLICY, AUTO_POLICY}:
        raise ValueError('SOURCE_FRAME_INVALID')
    exceptions = plan.get('source_frame_exceptions') or {}
    if any(value != HEIGHT_POLICY for value in exceptions.values()):
        raise ValueError('SOURCE_FRAME_INVALID')
    plan['source_frame_mode'] = exceptions.get(plan['source']['sha256'], mode)
    for source in [plan['source'], *plan.get('montage_segments', [])]:
        selected = exceptions.get(source['sha256'], mode)
        layout = geometry(source, *plan['visual']['canvas'], selected)
        if layout:
            source['source_frame_layout'] = layout
        if plan.get('montage_segments'):
            source['source_frame_mode'] = selected


def canvas_box(plan, region):
    """Map source-coordinate text covers to the preserved foreground."""
    source = plan['source']
    layout = source.get('source_frame_layout')
    if not layout:
        return None
    x, y, w, h = layout['foreground']
    l, t, r, b = region
    return [math.floor(x+l*w), math.floor(y+t*h), math.ceil(x+r*w), math.ceil(y+b*h)]
