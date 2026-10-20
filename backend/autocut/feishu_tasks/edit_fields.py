"""Lossless form parts for manuscripts longer than one Feishu input."""
import re

VERSION = 'EDITOR_V2_PARTS'
MAX_PARTS = 40


def split_text(value, limit):
    """Stay within both character and UTF-16 limits; never split a code point."""
    text = str(value)
    parts, current, units = [], [], 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if current and units + width > limit:
            parts.append(''.join(current)); current, units = [], 0
        current.append(character); units += width
    parts.append(''.join(current))
    return parts


def merge_fields(fields):
    """Merge submitted editor parts, including deliberately emptied inputs."""
    if not isinstance(fields, dict):
        raise ValueError('INVALID_REQUEST')
    text = fields.get('text', '')
    if not isinstance(text, str):
        raise ValueError('INVALID_REQUEST')
    pieces = {1: text}
    for key, value in fields.items():
        if not isinstance(key, str) or not key.startswith('text_part_'):
            continue
        match = re.fullmatch(r'text_part_([2-9]|[1-3][0-9]|40)', key)
        if not match or not isinstance(value, str):
            raise ValueError('INVALID_REQUEST')
        pieces[int(match[1])] = value
    merged = ''.join(pieces.get(index, '') for index in range(1, max(pieces)+1))
    if len(merged) > 12000:
        raise ValueError('SCRIPT_DRAFT_INVALID')
    return {key: value for key, value in fields.items()
            if not isinstance(key, str) or not key.startswith('text_part_')} | {'text': merged}
