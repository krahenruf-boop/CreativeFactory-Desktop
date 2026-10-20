"""The user's durable default for newly submitted on-screen titles."""
import copy

POLICY = 'TITLE_TWO_LINES_PERMANENT_V1'


def snapshot(value):
    out = copy.deepcopy(value)
    resolved = out.setdefault('resolved_overrides_json', {})
    values = resolved.setdefault('values', {})
    values['title_max_lines'] = 2
    values.setdefault('title_duration', 5.)
    for row in resolved.get('per_item_overrides', []):
        if 'title_max_lines' in row.get('values', {}):
            row['values']['title_max_lines'] = 2
    out['title_layout_policy'] = POLICY
    out['instruction_summary'] = list(dict.fromkeys(out.get('instruction_summary', []) +
        ['标题永久默认两行，可缩小字号，不回退三行']))
    return out
