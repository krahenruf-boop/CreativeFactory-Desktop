"""Bounded local subtitle font catalog. No downloads or user-supplied search paths."""
import hashlib
import os
from functools import lru_cache
from pathlib import Path
from PIL import ImageFont
from ..media import FONT

FONT_ROOTS = (Path.home()/'Library/Fonts', Path('/Library/Fonts'), Path('/System/Library/Fonts'))
FONTS = {
    'Arial': ('/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'),
    'Helvetica': ('/System/Library/Fonts/Helvetica.ttc', '/System/Library/Fonts/Helvetica.ttc'),
    'Heiti': (FONT, FONT),
}

def readable_font(path):
    return (path.suffix.lower() in {'.ttf', '.otf'} and not path.is_symlink()
            and path.is_file() and os.access(path, os.R_OK)
            and any(path.resolve().is_relative_to(root.resolve()) for root in FONT_ROOTS))

@lru_cache(maxsize=1)
def local_fonts():
    rows = []
    for root in FONT_ROOTS:  # User fonts take precedence.
        if not root.is_dir(): continue
        for path in sorted(root.rglob('*')):
            if path.stem.lower().replace('-', '').replace(' ', '') != 'poppinsbold': continue
            if not readable_font(path): continue
            try:
                family, style = ImageFont.truetype(str(path), 16).getname()
            except OSError:
                continue
            if (family, style) != ('Poppins', 'Bold'): continue
            path = path.resolve()
            rows.append(dict(id='local-'+hashlib.sha256(str(path).encode()).hexdigest()[:16],
                             family=family, style=style, label='Poppins Bold', path=str(path),
                             path_summary=('用户字体' if root == FONT_ROOTS[0] else '系统字体')+' / '+path.name))
    return rows[:1]

def catalog():
    return [{k:v for k,v in row.items() if k != 'path'} for row in local_fonts()] + [
        dict(id=family, family=family, style='Regular / Bold', label=family, path_summary='系统字体')
        for family in FONTS]

def selected_font(preset, font_id=None):
    """Resolve a catalog ID; validate persisted file paths even without an ID."""
    if font_id:
        row = next((f for f in local_fonts() if f['id'] == font_id), None)
        if row:
            preset = preset.model_copy(update={'font_family':row['family'], 'font_file':row['path'], 'font_weight':700})
        elif font_id in FONTS:
            preset = preset.model_copy(update={'font_family':font_id, 'font_file':None})
        else:
            raise ValueError('请选择本地字体目录中的有效字体')
    if preset.font_file:
        row = next((f for f in local_fonts() if f['path'] == preset.font_file), None)
        if not row or not readable_font(Path(preset.font_file)) or preset.font_family != row['family']:
            raise ValueError('字体文件不在允许的本地字体目录清单中')
        if ImageFont.truetype(preset.font_file, 16).getname() != (row['family'], row['style']):
            raise ValueError('本地字体文件已变化，请重新选择字体')
    return preset

def font_path(preset):
    selected_font(preset)
    if preset.font_file: return preset.font_file
    if preset.font_family == 'Poppins':
        row = next(iter(local_fonts()), None)
        if row:
            selected_font(preset.model_copy(update={'font_file':row['path']}))
            return row['path']
    return FONTS.get(preset.font_family, FONTS['Arial'])[int(preset.font_weight == 700)]

def default_preset():
    from .types import SubtitlePresetV1
    row = next(iter(local_fonts()), None)
    return selected_font(SubtitlePresetV1(), row['id']) if row else SubtitlePresetV1()
