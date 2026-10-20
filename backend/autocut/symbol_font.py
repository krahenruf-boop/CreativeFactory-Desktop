"""Keep the frozen Latin font, using macOS glyphs only for missing emoji."""
import hashlib
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

EMOJI_FONT = Path('/System/Library/Fonts/Apple Color Emoji.ttc')
TEXT_SYMBOL_FONT = Path('/System/Library/Fonts/Apple Symbols.ttf')
VERSION = 'APPROVED_SYMBOL_GLYPHS_V1'


@lru_cache(maxsize=24)
def file_hash(path, modified, size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(path):
    info = path.stat()
    return file_hash(str(path), info.st_mtime_ns, info.st_size)


def emoji(character):
    value = ord(character)
    return 0x1F000 <= value <= 0x1FAFF or 0x2600 <= value <= 0x27BF


def runs(text):
    """Split symbol clusters, keeping variations, modifiers and joined emoji intact."""
    result = []
    for character in text:
        attach = character in '\ufe0f\ufe0e\u200d' or 0x1F3FB <= ord(character) <= 0x1F3FF
        joined = result and result[-1][1] and result[-1][0].endswith('\u200d')
        if result and result[-1][1] and (attach or joined):
            value, kind = result[-1]; result[-1] = (value + character, kind)
        elif emoji(character):
            result.append((character, True))
        elif result and not result[-1][1]:
            value, kind = result[-1]; result[-1] = (value + character, kind)
        else:
            result.append((character, False))
    return result


@lru_cache(maxsize=128)
def fonts(path, size):
    return ImageFont.truetype(path, size)


def supported(font, character):
    mask = font.getmask(character)
    missing = font.getmask('\U0010ffff')
    return bool(mask.getbbox()) and (mask.size, bytes(mask)) != (missing.size, bytes(missing))


class SymbolFont(ImageFont.ImageFont):
    """Pillow mask font: original typeface for words, measured emoji alpha."""
    def __init__(self, primary, size, symbols):
        self.primary, self.size, self.symbols = primary, size, symbols
        self.color = fonts(str(EMOJI_FONT), 64)
        self.symbol_evidence = dict(policy=VERSION, font_file=str(EMOJI_FONT),
            font_sha256=digest(EMOJI_FONT), glyphs=sorted(set(symbols)),
            primary_font_unchanged=True)

    def getname(self):
        return self.primary.getname()

    def getmetrics(self):
        return self.primary.getmetrics()

    @lru_cache(maxsize=128)
    def symbol_mask(self, text):
        selected = self.color
        if not supported(selected, text):
            # Some Unicode text symbols have no bitmap in Apple Color Emoji.
            # Keep the exact approved codepoints; use the system symbol face.
            selected = fonts(str(TEXT_SYMBOL_FONT), 64)
            if not supported(selected, text):raise ValueError('SYMBOL_GLYPH_UNAVAILABLE')
            self.symbol_evidence.setdefault('text_symbol_font', dict(font_file=str(TEXT_SYMBOL_FONT),font_sha256=digest(TEXT_SYMBOL_FONT)))
        box = selected.getbbox(text)
        mask = Image.new('L', (max(1, box[2]-box[0]), max(1, box[3]-box[1])))
        ImageDraw.Draw(mask).text((-box[0], -box[1]), text, font=selected, fill=255)
        width = max(1, round(selected.getlength(text) * self.size / 64))
        return mask.resize((width, self.size), Image.Resampling.LANCZOS)

    def positioned(self, text, stroke=0):
        cursor = 0.0
        ascent = self.primary.getmetrics()[0]
        result = []
        for value, symbol in runs(text):
            if symbol:
                image = self.symbol_mask(value)
                box = (0, ascent-self.size, image.width, ascent)
                advance = image.width
            else:
                box = self.primary.getbbox(value)
                advance = self.primary.getlength(value)
            result.append((value, symbol, cursor, box))
            cursor += advance
        return result

    def getlength(self, text, *args, **kwargs):
        return sum(self.symbol_mask(value).width if symbol else self.primary.getlength(value)
                   for value, symbol in runs(text))

    def getbbox(self, text, *args, stroke_width=0, **kwargs):
        parts = self.positioned(text)
        if not parts:
            return (0, 0, 0, 0)
        left = min(x + box[0] for _, _, x, box in parts)
        top = min(box[1] for _, _, _, box in parts)
        right = max(x + box[2] for _, _, x, box in parts)
        bottom = max(box[3] for _, _, _, box in parts)
        return (math.floor(left)-stroke_width, top-stroke_width,
                math.ceil(right)+stroke_width, bottom+stroke_width)

    def getmask(self, text, mode='', *args, **kwargs):
        return self.getmask2(text, mode, *args, **kwargs)[0]

    def getmask2(self, text, mode='', *args, stroke_width=0, **kwargs):
        box = self.getbbox(text, stroke_width=stroke_width)
        layer = Image.new('L', (max(1, box[2]-box[0]), max(1, box[3]-box[1])))
        draw = ImageDraw.Draw(layer)
        for value, symbol, x, part in self.positioned(text):
            if symbol:
                glyph = self.symbol_mask(value)
                if stroke_width:
                    padded = Image.new('L', (glyph.width+2*stroke_width, glyph.height+2*stroke_width))
                    padded.paste(glyph, (stroke_width, stroke_width))
                    glyph = padded.filter(ImageFilter.MaxFilter(2*stroke_width+1))
                layer.paste(glyph, (round(x-box[0])-stroke_width, part[1]-box[1]-stroke_width))
            else:
                draw.text((x-box[0], -box[1]), value, font=self.primary,
                          fill=255, stroke_width=stroke_width, stroke_fill=255)
        return layer.im, (box[0], box[1])


def compatible_font(style, size, text, root):
    """Return None for the ordinary font path; never accept missing letters."""
    if not any(emoji(c) for c in text):
        return None
    path = Path(style['font_file']).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file() or path.suffix.lower() not in {'.ttf', '.otf'}:
        raise ValueError('FONT_LOAD_FAILED: missing font file')
    hashed = digest(path)
    if style.get('font_sha256') and hashed != style['font_sha256']:
        raise ValueError('FONT_LOAD_FAILED: font hash changed')
    primary = fonts(str(path), size)
    if list(primary.getname()) != style['pillow_name']:
        raise ValueError('FONT_LOAD_FAILED: font identity mismatch')
    missing = [c for c in set(text)-set('\n\r\t \ufe0f\ufe0e\u200d') if not supported(primary, c)]
    if not missing:
        return None
    if any(not emoji(c) for c in missing):
        raise ValueError('FONT_LOAD_FAILED: unsupported non-emoji glyph')
    if not EMOJI_FONT.is_file():
        raise ValueError('SYMBOL_FONT_UNAVAILABLE')
    fallback = fonts(str(EMOJI_FONT), 64)
    for value, symbol in runs(text):
        if symbol and not supported(fallback, value):
            if not TEXT_SYMBOL_FONT.is_file() or not supported(fonts(str(TEXT_SYMBOL_FONT),64),value):
                raise ValueError('SYMBOL_GLYPH_UNAVAILABLE')
    return SymbolFont(primary, size, missing), str(path), hashed


def recover(manager, ident, ranks):
    """Resume only the diagnosed glyph failure, reusing approved text/audio."""
    import json
    from .item_review import get_item, audit
    from .production_policy import approved_content
    from .script_language import script, title, check, target_language
    store = manager.store
    with store.lock, manager.lock:
        job = store.get(ident)
        if (job.get('deleted_at') or job.get('control_request') or manager.control.busy(ident)
                or (job.get('batch_review') or {}).get('phase') != 'RUNNING'):
            raise ValueError('STATE_TRANSITION_REJECTED')
        if not isinstance(ranks, list) or not ranks:
            raise ValueError('INVALID_REQUEST')
        normalized = [f'{int(rank):02}' for rank in ranks]
        if len(set(normalized)) != len(normalized):
            raise ValueError('INVALID_REQUEST')
        report = store.home / 'jobs' / ident / 'work/RUN_REPORT.jsonl'
        errors = {}
        for line in report.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') == 'ITEM_RECOVERY_ERROR':
                errors[str(event.get('rank')).zfill(2)] = str(event.get('error', ''))
        rows = []
        for rank in normalized:
            _, item = get_item(store, ident, rank)
            if item['status'] == 'PASS':
                continue
            legacy = (item.get('issues') == ['JOB_REVIEW_REQUIRED']
                      and errors.get(rank, '').startswith('FONT_LOAD_FAILED:')
                      and 'missing glyph U+' in errors[rank])
            if item['status'] != 'FAILED' or not (legacy or item.get('issues') == ['SYMBOL_GLYPH_UNAVAILABLE']):
                raise ValueError('STATE_TRANSITION_REJECTED')
            if not approved_content(item):
                raise ValueError('SCRIPT_CONFIRMATION_REQUIRED')
            spoken = check(script(item), target_language(job))
            heading = check(title(item), target_language(job), 'TITLE')
            if not any(emoji(c) for c in spoken['text'] + heading['text']):
                raise ValueError('STATE_TRANSITION_REJECTED')
            audio = item.get('audio_result') or {}
            source = item.get('source_snapshot') or {}
            if (not item.get('analysis') or not item.get('decision') or item.get('plan')
                    or item.get('output') or audio.get('script_text') != spoken['text']):
                raise ValueError('RECOVERY_CACHE_INVALID')
            for cached in (audio, source):
                path = Path(cached.get('path', ''))
                if not path.is_file() or digest(path) != cached.get('sha256'):
                    raise ValueError('RECOVERY_CACHE_INVALID')
            rows.append(rank)
        for rank in rows:
            store.item(ident, rank, status='QUEUED', issues=[], resume_pending=True,
                       resume_phase='SCRIPT', symbol_font_recovery=VERSION)
            audit(store, ident, rank, 'SYMBOL_FONT_RECOVERY', script_preserved=True,
                  title_preserved=True, audio_reused=True)
        if rows:
            store.update(ident, state='RUNNING', stage='剪辑')
            manager.enqueue_batch(ident, [(rank, 'SCRIPT') for rank in rows])
