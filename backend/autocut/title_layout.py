"""Fit approved titles without rewriting them or changing the engine preset."""
import copy
from functools import lru_cache
from itertools import combinations
from app.s3.visual_preset import fit_text, load_font

VERSION = 'TITLE_THREE_LINES_V2'
# Reference pixels on a 1080x1920 canvas. Approved sentence-style hooks can
# need 54–55 px; the former 56 px floor rejected them before rendering.
# Keep a readable lower bound, matching the smaller subtitle preset.
MIN_FONT_SIZE = 48


def apply(visual, approved_title, max_lines=None):
    if max_lines == 2:
        return two_lines(visual, approved_title)
    style = visual['title']
    try:
        # Preserve the established layout for titles that already fit.
        fit = fit_text(approved_title, style, visual['canvas'])
        return fit
    except ValueError as error:
        if str(error) != 'TITLE_SHORTEN_REQUIRED':
            raise
    original = copy.deepcopy(style)
    adjusted = dict(style, max_lines=3, font_min=min(style['font_min'], MIN_FONT_SIZE))
    words = approved_title.split()
    w, h = visual['canvas']; scale = min(w / 1080, h / 1920)
    stroke = max(1, round(style['stroke_width'] * scale))
    for size in range(adjusted['font_size'], adjusted['font_min'] - 1, -1):
        font, _, _ = load_font(adjusted, max(1, round(size * scale)), approved_title)
        @lru_cache(None)
        def width(lo, hi):
            box = font.getbbox(' '.join(words[lo:hi]), stroke_width=stroke)
            return box[2] - box[0]
        for count in range(1, min(3, len(words)) + 1):
            choices = []
            for cuts in combinations(range(1, len(words)), count - 1):
                edges = (0, *cuts, len(words))
                spans = list(zip(edges, edges[1:]))
                widths = [width(a, b) for a, b in spans]
                if max(widths) <= w * style['max_width_ratio']:
                    choices.append((max(widths) - min(widths), spans))
            if not choices:
                continue
            _, spans = min(choices)
            wrapped = '\n'.join(' '.join(words[a:b]) for a, b in spans)
            # Existing renderer accepts explicit three-line text. Freeze the
            # approved policy in this plan only so its style QA remains strict.
            fit = fit_text(wrapped, adjusted, visual['canvas'])
            visual['title'] = dict(adjusted, text=wrapped)
            visual['preset_snapshot']['title'].update(max_lines=3, font_min=adjusted['font_min'])
            visual['title_layout'] = dict(policy=VERSION, approved_text=approved_title,
                lines=fit['lines'], reference_font_size=fit['reference_font_size'],
                original_max_lines=original['max_lines'], original_font_min=original['font_min'])
            return fit
    raise ValueError('TITLE_SHORTEN_REQUIRED')


def two_lines(visual, approved_title):
    """An explicit job limit cannot fall back to the legacy third line."""
    original = copy.deepcopy(visual['title'])
    adjusted = dict(original, max_lines=2, font_min=min(original['font_min'], MIN_FONT_SIZE))
    words = approved_title.split()
    w, h = visual['canvas']; scale = min(w / 1080, h / 1920)
    stroke = max(1, round(original['stroke_width'] * scale))
    for size in range(adjusted['font_size'], adjusted['font_min'] - 1, -1):
        font, _, _ = load_font(adjusted, max(1, round(size * scale)), approved_title)
        choices = []
        for cut in range(1, len(words)):
            lines = [' '.join(words[:cut]), ' '.join(words[cut:])]
            widths = [font.getbbox(line, stroke_width=stroke) for line in lines]
            widths = [b[2] - b[0] for b in widths]
            if max(widths) <= w * adjusted['max_width_ratio']:
                choices.append((max(widths) - min(widths), lines))
        if not choices:
            continue
        _, lines = min(choices)
        adjusted['font_size'] = size
        fit = fit_text('\n'.join(lines), adjusted, visual['canvas'])
        if len(fit['lines']) != 2:
            continue
        visual['title'] = dict(adjusted, text='\n'.join(lines))
        visual['preset_snapshot']['title'].update(max_lines=2, font_min=adjusted['font_min'], font_size=size)
        visual['title_layout'] = dict(policy='TITLE_TWO_LINES_V1', approved_text=approved_title, lines=fit['lines'],
                                     reference_font_size=fit['reference_font_size'], original_max_lines=original['max_lines'], original_font_min=original['font_min'])
        return fit
    raise ValueError('TITLE_SHORTEN_REQUIRED')
