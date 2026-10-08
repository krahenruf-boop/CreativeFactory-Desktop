import hashlib, json, re
from pathlib import Path
from PIL import Image, ImageColor, ImageDraw, ImageFont
from ..media import W, H
from .types import Caption, SubtitleTrackV1, SubtitlePresetV1
from .fonts import font_path

def font(p):
    return ImageFont.truetype(font_path(p), p.font_size)

def scaled_alignment(alignment, speed, duration):
    chars = alignment.get('characters', [])
    starts = alignment.get('character_start_times_seconds', [])
    ends = alignment.get('character_end_times_seconds', [])
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        raise ValueError('ALIGNMENT_NOT_READY：字符与时间数组不完整')
    previous_start = previous_end = 0
    for start, end in zip(starts, ends):
        if not (0 <= start <= end and start >= previous_start and end >= previous_end and end / speed <= duration + .08):
            raise ValueError('ALIGNMENT_NOT_READY：时间不单调或越界')
        previous_start, previous_end = start, end
    return dict(characters=chars, character_start_times_seconds=[min(duration, t / speed) for t in starts],
                character_end_times_seconds=[min(duration, t / speed) for t in ends])

def words_from_alignment(a):
    words = []; text = ''; start = end = 0
    for char, s, e in zip(a['characters'], a['character_start_times_seconds'], a['character_end_times_seconds']):
        if char.isspace():
            if text: words.append(dict(text=text, start=start, end=end)); text = ''
        else:
            if not text: start = s
            text += char; end = e
    if text: words.append(dict(text=text, start=start, end=end))
    return words

def wrap_words(words, preset):
    # Pixel measurement is shared by the UI raster and final overlays.
    f = font(preset); width = min(900, int(2 * min(preset.position_x, 1 - preset.position_x) * W - 64))
    lines = ['']
    for word in words:
        text = (lines[-1] + ' ' + word).strip()
        if lines[-1] and (len(text) > 32 or f.getlength(text) > width): lines.append(word)
        else: lines[-1] = text
        if f.getlength(word) > width: raise ValueError('字幕单词超出安全区域，请减小字号或调整位置')
    return lines

def make_track(text, alignment, duration, preset):
    if ''.join(alignment['characters']) != text: raise ValueError('ALIGNMENT_TEXT_MISMATCH：字幕时间与已确认 TTS 文本不一致')
    words = words_from_alignment(alignment); groups = []; group = []
    for w in words:
        if group:
            proposed = group + [w]
            stop = (len(proposed) > 8 or w['end'] - group[0]['start'] > 2.8 or
                    len(wrap_words([x['text'] for x in proposed], preset)) > preset.max_lines or
                    w['start'] - group[-1]['end'] >= .3 or
                    (len(group) >= 4 and re.search(r'[.!?;,:]$', group[-1]['text'])))
            if stop: groups.append(group); group = []
        group.append(w)
    if group: groups.append(group)
    captions = []
    for i, g in enumerate(groups):
        start = g[0]['start']; end = g[-1]['end']
        limit = groups[i+1][0]['start'] if i+1 < len(groups) else duration
        # Extend a brief phrase only into available silence; never overlap.
        end = min(limit, duration, max(end, min(start + .6, limit)))
        if end <= start: raise ValueError('字幕时间长度无效')
        words = [w['text'] for w in g]
        captions.append(Caption(start=start, end=end, text=' '.join(words), lines=wrap_words(words, preset)))
    return SubtitleTrackV1(text=text, captions=captions, preset=preset)

def caption_layer(caption, preset, cache):
    identity = [caption.lines, preset.model_dump(), 'subtitle-raster-v1']
    if preset.font_file or preset.font_family == 'Poppins':
        identity.append(hashlib.sha256(Path(font_path(preset)).read_bytes()).hexdigest())
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = Path(cache) / (key + '.png')
    if path.exists(): return path
    f = font(preset); text = '\n'.join(caption.lines); stroke = preset.stroke_width if preset.stroke_enabled else 0
    canvas = Image.new('RGBA', (W, H)); d = ImageDraw.Draw(canvas)
    box = d.multiline_textbbox((0, 0), text, font=f, spacing=preset.line_spacing, stroke_width=stroke, align='center')
    width, height = box[2]-box[0], box[3]-box[1]
    left = preset.position_x * W - width/2; top = preset.position_y * H - height/2
    if left < 24 or left + width > W-24 or top < 20 or top + height > H-180:
        raise ValueError('字幕超出安全区域，请调整预设')
    if preset.background_enabled:
        fill = ImageColor.getrgb(preset.background_color) + (round(255*preset.background_opacity),)
        d.rounded_rectangle((left-18, top-12, left+width+18, top+height+12), radius=preset.background_radius, fill=fill)
    xy = (left-box[0], top-box[1])
    if preset.shadow_enabled: d.multiline_text((xy[0]+3, xy[1]+4), text, font=f, fill=(0,0,0,180), spacing=preset.line_spacing, stroke_width=stroke+1, align='center')
    d.multiline_text(xy, text, font=f, fill=preset.text_color, stroke_width=stroke, stroke_fill=preset.stroke_color, spacing=preset.line_spacing, align='center')
    path.parent.mkdir(parents=True, exist_ok=True); canvas.save(path)
    return path
