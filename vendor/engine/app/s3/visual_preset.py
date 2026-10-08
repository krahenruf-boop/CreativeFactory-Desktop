"""Headless-only visual layers. No provider, UI, or original-overlay decisions.

New Transport plans call build_visual_plan, save its immutable snapshot, then
visual_inputs. Existing plans/renderers remain reproducible without migration.
"""
import copy
import hashlib
import json
import logging
import math
import unicodedata
from bisect import bisect_right
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from app.cta_assets import CTAAssets
from app.s3.subtitles import words_from_alignment

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / 'presets/tiktok_br_transport_v1.json'
REASONS = {'PRODUCT_OCCLUSION', 'FACE_OCCLUSION', 'SOURCE_TEXT_CONFLICT',
           'CTA_SUBTITLE_COLLISION', 'OTHER'}
LOG = logging.getLogger(__name__)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_preset():
    return json.loads(PRESET.read_text())


def display_text(text):
    # Preserve words/case/accents; punctuation remains in the caller's TTS text.
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('P')
                           or c in '%+−=<>~' else c for c in text).split())


def load_font(style, size, text=''):
    path = Path(style['font_file']).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        if not path.is_file() or path.suffix.lower() not in {'.ttf', '.otf'}:
            raise ValueError('missing font file')
        digest = sha(path)
        if style.get('font_sha256') and digest != style['font_sha256']:
            raise ValueError('font hash changed')
        font = ImageFont.truetype(str(path), size)
        if list(font.getname()) != style['pillow_name']:
            raise ValueError('font identity mismatch')
        missing = font.getmask('\U0010ffff')
        signature = (missing.size, bytes(missing))
        for c in set(text) - set('\n\r\t '):
            glyph = font.getmask(c)
            if not glyph.getbbox() or (glyph.size, bytes(glyph)) == signature:
                raise ValueError(f'missing glyph U+{ord(c):04X}')
        return font, str(path), digest
    except (OSError, ValueError) as exc:
        raise ValueError(f'FONT_LOAD_FAILED: {path}: {exc}') from exc


def fit_text(text, style, canvas, subtitle=False):
    w, h = canvas
    scale = min(w / 1080, h / 1920)
    text = display_text(text) if subtitle else text.strip()
    if not text:
        raise ValueError('EMPTY_DISPLAY_TEXT')
    stroke = max(1, round(style['stroke_width'] * scale))
    words = text.split()
    candidates = [[text]] if '\n' not in text else [text.splitlines()]
    candidates += [[' '.join(words[:i]), ' '.join(words[i:])] for i in range(1, len(words))]
    for base_size in range(style['font_size'], style['font_min'] - 1, -1):
        size = max(1, round(base_size * scale))
        font, path, digest = load_font(style, size, text)
        def width(s):
            b = font.getbbox(s, stroke_width=stroke)
            return b[2] - b[0]
        fits = [lines for lines in candidates if len(lines) <= style['max_lines']
                and all(width(line) <= w * style['max_width_ratio'] for line in lines)]
        if not fits:
            continue
        # Preserve intentional two-line wrapping when it fits, otherwise balance.
        explicit = text.splitlines()
        if len(explicit) > 1 and explicit in fits:
            lines = explicit
        else:
            lines = min(fits, key=lambda ls: (len(ls), max(map(width, ls)) - min(map(width, ls))))
        return dict(text=text, lines=lines, font=font, font_file=path, font_sha256=digest,
                    font_size=size, reference_font_size=base_size, stroke_width=stroke)
    raise ValueError('SUBTITLE_SPLIT_REQUIRED' if subtitle else 'TITLE_SHORTEN_REQUIRED')


def text_layer(text, style, canvas, path, subtitle=False):
    fit = fit_text(text, style, canvas, subtitle)
    font, stroke = fit['font'], fit['stroke_width']
    advance = round(fit['font_size'] * style['line_spacing_ratio'])
    boxes = [font.getbbox(s, stroke_width=stroke) for s in fit['lines']]
    width = max(b[2] - b[0] for b in boxes)
    top = min(b[1] + i * advance for i, b in enumerate(boxes))
    bottom = max(b[3] + i * advance for i, b in enumerate(boxes))
    block = Image.new('RGBA', (width + 4, bottom - top + 4))
    draw = ImageDraw.Draw(block)
    for i, (line, box) in enumerate(zip(fit['lines'], boxes)):
        draw.text((2 + (width - box[2] + box[0]) / 2 - box[0], 2 - top + i * advance),
                  line, font=font, fill=style['fill'], stroke_width=stroke,
                  stroke_fill=style['stroke'])
    block = block.crop(block.getbbox())
    x = round(canvas[0] * style['anchor_x'] - block.width / 2)
    y = round(canvas[1] * style['anchor_y'] - block.height / 2)
    if x < 0 or y < 0 or x + block.width > canvas[0] or y + block.height > canvas[1]:
        raise ValueError('TEXT_OUT_OF_CANVAS')
    image = Image.new('RGBA', canvas)
    image.alpha_composite(block, (x, y))
    image.save(path)
    LOG.info('%s_font_loaded = %s', 'subtitle' if subtitle else 'title', style['font_name'])
    return {k: v for k, v in fit.items() if k != 'font'} | dict(
        bbox=[x, y, x + block.width, y + block.height], line_advance=advance,
        font_loaded=style['font_name'], font_fallback=False, layer=str(path), layer_sha256=sha(path))


def caption_events(alignment, canvas=(1080, 1920)):
    """Fit alignment words before removing display punctuation; never re-ASR audio."""
    style = load_preset()['subtitle']
    words = words_from_alignment(alignment)
    events, group = [], []
    def emit():
        raw = ' '.join(w['text'] for w in group)
        fit = fit_text(raw, style, canvas, subtitle=True)
        events.append(dict(start=group[0]['start'], end=group[-1]['end'], text=raw,
                           subtitle_display_text=fit['text']))
    for word in words:
        split = bool(group) and (len(group) >= 8 or word['end'] - group[0]['start'] > 2.8
                                or word['start'] - group[-1]['end'] >= .3
                                or (len(group) >= 4 and group[-1]['text'][-1:] in '.!?;:,'))
        if group and not split:
            try:
                fit_text(' '.join(w['text'] for w in group + [word]), style, canvas, subtitle=True)
            except ValueError as exc:
                if str(exc) != 'SUBTITLE_SPLIT_REQUIRED':
                    raise
                split = True
        if split:
            emit()
            group = []
        group.append(word)
    if group:
        emit()
    return events


def normalize_cta(asset, data_root, width, path, width_basis='visible_artwork'):
    """Keep user artwork/frame delays, remove baked-in translation and empty margins."""
    source = CTAAssets(data_root).path(asset)  # Existing asset identity/hash guard.
    frames, delays, bodies = [], [], []
    with Image.open(source) as image:
        for i in range(getattr(image, 'n_frames', 1)):
            image.seek(i)
            frame = image.convert('RGBA')
            box = frame.getchannel('A').getbbox()
            if not box:
                raise ValueError('CTA_EMPTY_FRAME')
            frame = frame.crop(box)
            body = (0, 0, frame.width, frame.height)
            if width_basis == 'red_arrow_body':
                # This user's red artwork: flashes are preserved but do not define
                # its scale/pivot. No replacement drawing or per-frame zoom.
                red, green, blue, alpha = frame.split()
                mask = ImageChops.multiply(red.point(lambda a: 255 if a > 140 else 0),
                                           green.point(lambda a: 255 if a < 100 else 0))
                mask = ImageChops.multiply(mask, blue.point(lambda a: 255 if a < 100 else 0))
                body = ImageChops.multiply(mask, alpha).getbbox()
                if not body:
                    raise ValueError('CTA_RED_BODY_NOT_FOUND')
            frames.append(frame)
            bodies.append(body)
            delays.append(max(10, int(image.info.get('duration', 100) or 100)))
    ratio = width / max(b[2] - b[0] for b in bodies)
    pivots = [((b[0]+b[2])/2, (b[1]+b[3])/2) for b in bodies]
    half_w = math.ceil(max(max(x, f.width-x) for f,(x,y) in zip(frames,pivots))*ratio)+1
    half_h = math.ceil(max(max(y, f.height-y) for f,(x,y) in zip(frames,pivots))*ratio)+1
    sprites, bounds, body_bounds = [], [], []
    for frame, body, pivot in zip(frames, bodies, pivots):
        frame = frame.resize((max(1, round(frame.width * ratio)), max(1, round(frame.height * ratio))),
                             Image.Resampling.LANCZOS)
        sprite = Image.new('RGBA', (2*half_w, 2*half_h))
        x, y = round(half_w-pivot[0]*ratio), round(half_h-pivot[1]*ratio)
        sprite.alpha_composite(frame, (x, y))
        sprites.append(sprite)
        bounds.append(list(sprite.getchannel('A').getbbox()))
        body_bounds.append([x+body[0]*ratio, y+body[1]*ratio,
                            x+body[2]*ratio, y+body[3]*ratio])
    sprites[0].save(path, save_all=len(sprites) > 1, append_images=sprites[1:],
                    duration=delays, loop=0, disposal=0, blend=0)
    return dict(path=str(path), sha256=sha(path), width=sprites[0].width, height=sprites[0].height,
                frame_count=len(sprites), cycle_seconds=sum(delays) / 1000, bounds=bounds,
                body_bounds=body_bounds, delays_ms=delays, width_basis=width_basis,
                source_asset_id=asset['id'], source_sha256=asset['sha256'])


def build_visual_plan(duration, title, captions, asset=None, canvas=(1080, 1920), overrides=None):
    """Captions already carry alignment-derived start/end; this never changes timing."""
    p = load_preset()
    if asset is None:
        from app.s2.engine import Store
        data = Path.home() / 'Library/Application Support/Creative Factory'
        asset = Store(data / 'creative_factory.sqlite3').get('cta_asset', p['cta']['default_asset_id'])
        if not asset or asset['sha256'] != p['cta']['default_asset_sha256']:
            raise ValueError('DEFAULT_CTA_MISSING_OR_CHANGED')
        CTAAssets(data).path(asset)
    w, h = canvas
    if duration <= 0 or min(canvas) < 180 or abs(w / h - 9 / 16) > .002:
        raise ValueError('INVALID_VISUAL_CANVAS_OR_DURATION')
    plan = dict(visual_preset=p['id'], preset_sha256=sha(PRESET), preset_snapshot=p,
                canvas=list(canvas), duration=duration, title=copy.deepcopy(p['title']),
                subtitle=copy.deepcopy(p['subtitle']), cta=copy.deepcopy(p['cta']),
                visual_preset_override=False, overrides=[])
    plan['title'].update(text=title, start=0, end=min(5., duration))
    plan['subtitle']['captions'] = copy.deepcopy(captions)
    plan['cta'].update(asset=copy.deepcopy(asset), start=max(0, duration - 5), end=duration)
    selected_overrides = list(overrides or [])
    asset_adjustment = p['cta'].get('asset_layout_override')
    if (asset_adjustment and asset['id'] == p['cta']['default_asset_id']
            and not any(o['layer'] == 'cta' for o in selected_overrides)):
        selected_overrides.append(asset_adjustment)
    for override in selected_overrides:
        layer = override['layer']
        if layer not in {'title', 'subtitle', 'cta'} or override.get('override_reason') not in REASONS:
            raise ValueError('INVALID_VISUAL_OVERRIDE_REASON')
        chosen = override['chosen_position']
        limits = p['override_limits'][layer]
        if not limits['y'][0] <= chosen[1] <= limits['y'][1] or not any(
                lo <= chosen[0] <= hi for lo, hi in limits.get('x_ranges', [limits.get('x')])):
            raise ValueError('VISUAL_OVERRIDE_OUT_OF_RANGE')
        original = [plan[layer]['anchor_x'], plan[layer]['anchor_y']]
        plan['overrides'].append(dict(layer=layer, override_reason=override['override_reason'],
                                      default_position=original, chosen_position=chosen))
        if override.get('detail'):
            plan['overrides'][-1]['detail'] = override['detail']
        plan[layer].update(anchor_x=chosen[0], anchor_y=chosen[1])
        plan['visual_preset_override'] = True
    return plan


def validate_visual_plan(plan):
    """Geometry/style checks are technical checks, never an aesthetic approval."""
    p = plan['preset_snapshot']
    errors = []
    if type(plan['cta'].get('enabled', True)) is not bool:
        errors.append('CTA_ENABLED_INVALID')
    if (plan['cta'].get('enabled', True) is False
            and plan.get('user_revision', {}).get('omit_added_cta') is not True):
        errors.append('CTA_USER_EXCEPTION_MISSING')
    for layer, code in [('title', 'TITLE_PRESET_DEVIATION'), ('subtitle', 'SUBTITLE_PRESET_DEVIATION'),
                        ('cta', 'CTA_PRESET_DEVIATION')]:
        s = plan[layer]
        exemptions = set()
        for o in plan.get('overrides', []):
            if o.get('layer') == layer and o.get('override_reason') in REASONS and plan.get('visual_preset_override'):
                limits = p['override_limits'][layer]
                x, y = o['chosen_position']
                if (o['default_position'] == [p[layer]['anchor_x'], p[layer]['anchor_y']]
                    and [s['anchor_x'], s['anchor_y']] == [x, y]
                    and limits['y'][0] <= y <= limits['y'][1]
                    and any(lo <= x <= hi for lo, hi in limits.get('x_ranges', [limits.get('x')]))):
                    exemptions.update({'anchor_x', 'anchor_y'})
        if any(s.get(k) != v for k, v in p[layer].items() if k not in exemptions):
            errors.append(code)
    d = plan['duration']
    if (plan['title']['start'], plan['title']['end']) != (0, min(5., d)):
        errors.append('TITLE_PRESET_DEVIATION')
    if (plan['cta']['start'], plan['cta']['end']) != (max(0, d - 5), d):
        errors.append('CTA_PRESET_DEVIATION')
    for c in plan['subtitle']['captions']:
        if not 0 <= c['start'] < c['end'] <= d:
            errors.append('SUBTITLE_OUT_OF_RANGE')
    return sorted(set(errors))


def visual_inputs(plan, directory, data_root, base_label='base', first_index=1):
    """Return FFmpeg inputs/filter graph for the saved plan, plus measured evidence.

    Caller applies its ordinary video/audio processing first, then this graph;
    no source selection, overlay cleanup, TTS, or audio mutation happens here.
    """
    failures = validate_visual_plan(plan)
    if failures:
        raise ValueError(', '.join(failures))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    canvas = tuple(plan['canvas'])
    args, filters, evidence = [], [], {'font_fallback': False}
    previous, index = base_label, first_index
    events = [('title', plan['title'], plan['title'])]
    events += [('subtitle', c, plan['subtitle']) for c in plan['subtitle']['captions']]
    evidence['text_layers'] = []
    for kind, event, style in events:
        if not event.get('text'):
            continue
        path = directory / f'{kind}_{index}.png'
        m = text_layer(event['text'], style, canvas, path, kind == 'subtitle')
        m.update(kind=kind, start=event['start'], end=event['end'])
        evidence['text_layers'].append(m)
        args += ['-loop', '1', '-framerate', '30', '-i', str(path)]
        label = f'vp{index}'
        filters.append(f'[{previous}][{index}:v]overlay=0:0:enable=\'gte(t,{event["start"]})*lt(t,{event["end"]})\'[{label}]')
        previous, index = label, index + 1
    c = plan['cta']
    if c.get('enabled', True) is False:
        evidence['cta'] = dict(enabled=False, reason='USER_REQUESTED_NO_ADDED_CTA')
        filters.append(f'[{previous}]null[visual_out]')
        return args, filters, evidence
    sprite = normalize_cta(c['asset'], data_root, round(canvas[0] * c['width_ratio']), directory / 'cta.png',
                           c.get('width_basis', 'visible_artwork'))
    x = canvas[0] * c['anchor_x'] - sprite['width'] / 2
    y = canvas[1] * c['anchor_y'] - sprite['height'] / 2
    amplitude = canvas[1] * c['bounce_amplitude_ratio']
    if x < 0 or x + sprite['width'] > canvas[0] or y - amplitude < 0 or y + sprite['height'] + amplitude > canvas[1]:
        raise ValueError('CTA_OUT_OF_CANVAS')
    bounds = sprite['body_bounds']
    centers = [(b[0] + b[2]) / 2 for b in bounds]
    if max(centers) - min(centers) > 1:
        raise ValueError('CTA_HORIZONTAL_DRIFT')
    evidence['cta'] = sprite | dict(x=x, base_y=y, anchor=[c['anchor_x'], c['anchor_y']],
                                    amplitude_px=amplitude, horizontal_drift_px=max(centers) - min(centers),
                                    start=c['start'], end=c['end'], period=c['bounce_period_seconds'])
    args += (['-ignore_loop', '0'] if sprite['frame_count'] > 1 else ['-loop', '1']) + ['-i', sprite['path']]
    filters.append(f'[{index}:v]setpts=PTS-STARTPTS+{c["start"]}/TB[vpcta]')
    filters.append(f'[{previous}][vpcta]overlay=x={x}:y=\'{y}+{amplitude}*sin(2*PI*(t-{c["start"]})/{c["bounce_period_seconds"]})\':'
                   f'enable=\'gte(t,{c["start"]})*lt(t,{c["end"]})\':eof_action=pass[visual_out]')
    # Real alpha intersection: empty padding/side flashes must not be mistaken
    # for a solid rectangle. Check the actual loop and bounce for every CTA frame.
    alpha_frames = []
    with Image.open(sprite['path']) as image:
        for i in range(getattr(image, 'n_frames', 1)):
            image.seek(i)
            alpha_frames.append(image.convert('RGBA').getchannel('A'))
    ends=[];total=0
    for delay in sprite['delays_ms']:
        total += delay / 1000
        ends.append(total)
    for m in evidence['text_layers']:
        with Image.open(m['layer']) as image:
            text_alpha = image.getchannel('A')
        for frame in range(math.ceil(max(c['start'], m['start'])*30), math.ceil(min(c['end'],m['end'])*30)):
            t = frame/30 - c['start']
            index = min(bisect_right(ends,t%total),len(alpha_frames)-1)
            left = round(x);top = round(y+amplitude*math.sin(2*math.pi*t/c['bounce_period_seconds']))
            crop = text_alpha.crop((left,top,left+sprite['width'],top+sprite['height']))
            if ImageChops.multiply(crop,alpha_frames[index]).getbbox():
                raise ValueError('CTA_SUBTITLE_COLLISION' if m['kind'] == 'subtitle' else 'CTA_TITLE_COLLISION')
    return args, filters, evidence
