"""Explicit per-job coverage, repeated manuscripts and music-only outputs."""
import copy
import math
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .task_instructions import digest, safe_raw


class ScriptGroup(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    ranks: list[int] = Field(min_length=1, max_length=30)
    angle: Literal['LOVE_GIFT_DAILY_COMPANION', 'LOVE_GIFT_SYMBOL', 'LOVE_GIFT_THOUGHTFUL_GESTURE', 'HARD_SELL_PROMOTION', 'VERIFIED_PRODUCT']


class CustomRemix(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    coverage: Literal['ALL_SOURCES_BALANCED']
    script_groups: list[ScriptGroup] = Field(min_length=1, max_length=30)
    music_only_ranks: list[int] = Field(default_factory=list, max_length=30)
    music_paths: dict[str, str] = Field(default_factory=dict)
    title_lines: Literal[2] = 2
    cut_style: Literal['SOURCE_BALANCED', 'FAST_INTERLEAVED'] = 'SOURCE_BALANCED'
    promotion_offer: str = Field(default='', max_length=500)
    source_folder_priority: list[str] = Field(default_factory=list, max_length=10)


ANGLES = {
    'LOVE_GIFT_DAILY_COMPANION': 'Generate a NEW Brazilian Portuguese manuscript from the verified selling points of THIS product. Romantic gift angle: a small daily reminder of the person you love, accompanying car rides or other uses only when supported. Open with a relatable everyday scene, show a concrete visible benefit, then a gentle shopping CTA. Warm, colloquial and concise; never invent a personal testimonial. Do not translate/reuse source narration.',
    'LOVE_GIFT_SYMBOL': 'Generate a DIFFERENT NEW Brazilian Portuguese manuscript from verified facts about THIS product. Romantic gift angle: the supported paired design or visual symbolism expresses togetherness and affectionate commitment. Turn visible design/motion into a meaningful everyday benefit, using a warm discovery hook and gentle CTA. Do not invent product features, guaranteed emotional effects or a personal testimonial. Do not translate/reuse source narration.',
    'LOVE_GIFT_THOUGHTFUL_GESTURE': 'Generate a THIRD DISTINCT NEW Brazilian Portuguese manuscript from verified facts about THIS product. Romantic gift angle: choosing a thoughtful small gift that fits the loved one\'s everyday routine. Lead with the gift-giver\'s real choice or wish, demonstrate a supported product detail/use, then explain the thoughtful gesture and end with a gentle CTA. No invented occasion, price, offer, guarantee or personal testimonial. Do not translate/reuse source narration.',
}


def validate(data):
    value = data.get('custom_remix')
    if not value:
        return None
    obj = CustomRemix.model_validate(value).model_dump()
    folders = obj['source_folder_priority']
    if (len(set(folders)) != len(folders) or any(not name or len(name) > 64 or name.startswith('.')
            or any(c in name for c in '/\\') or any(ord(c) < 32 for c in name) for name in folders)):
        raise ValueError('CUSTOM_REMIX_INVALID')
    generic = any(g['angle'] == 'VERIFIED_PRODUCT' for g in obj['script_groups'])
    if generic and (obj['music_only_ranks'] or any(g['angle'] != 'VERIFIED_PRODUCT' for g in obj['script_groups'])):
        raise ValueError('CUSTOM_REMIX_INVALID')
    if folders and not generic:
        raise ValueError('CUSTOM_REMIX_INVALID')
    count = data.get('output_count', 10)
    ranks = [r for g in obj['script_groups'] for r in g['ranks']] + obj['music_only_ranks']
    if data.get('mode') != 'Remix' or sorted(ranks) != list(range(1, count + 1)):
        raise ValueError('CUSTOM_REMIX_INVALID')
    if set(obj['music_paths']) != {f'{r:02}' for r in obj['music_only_ranks']}:
        raise ValueError('CUSTOM_REMIX_MUSIC_REQUIRED')
    promotional = any(g['angle'] == 'HARD_SELL_PROMOTION' for g in obj['script_groups'])
    if promotional:
        if (not obj['promotion_offer'].strip() or obj['music_only_ranks']
                or any(g['angle'] != 'HARD_SELL_PROMOTION' for g in obj['script_groups'])):
            raise ValueError('CUSTOM_REMIX_INVALID')
        obj['promotion_offer'] = safe_raw(obj['promotion_offer'])
    elif obj['promotion_offer'] or obj['cut_style'] != 'SOURCE_BALANCED':
        raise ValueError('CUSTOM_REMIX_INVALID')
    return obj


def snapshot(data):
    obj = validate(data)
    values = dict(copy_style='emotional', localization_style='local_colloquial',
                  title_direction=['emotion', 'benefit'], title_enabled=True,
                  title_duration=5., title_max_lines=2, cta_enabled=True,
                  cta_position_preference='left', allow_propainter=False)
    promotional = obj['script_groups'][0]['angle'] == 'HARD_SELL_PROMOTION'
    generic = obj['script_groups'][0]['angle'] == 'VERIFIED_PRODUCT'
    summaries = [f"{len(obj['script_groups'])}套爱情礼物口播按指定编号复用", f"{len(obj['music_only_ranks'])}条纯音乐"]
    if generic:
        values.update(copy_style='natural', title_direction=['pain', 'benefit'])
        summaries = ['基于本任务已核实的素材与卖点生成本地口播，保留真实口播及标题确认']
        if obj['source_folder_priority']:
            summaries.append('开场素材优先级：' + ' → '.join(obj['source_folder_priority']))
    if promotional:
        values.update(copy_style='hard_sell', promotion_intensity='hard_sell',
                      title_direction=['pain', 'benefit', 'promotion'])
        summaries = [f"{len(obj['script_groups'])}套促销口播及标题，沿用用户提供的优惠内容"]
        if obj['cut_style'] == 'FAST_INTERLEAVED':
            values.update(video_pace='slightly_fast', video_speed_preference=1.15,
                          preserve_source_structure=False)
            summaries.append('源片段可打断，约3秒短镜头交替拼接')
    return dict(custom_instruction_raw=safe_raw(data.get('custom_instruction_raw', '')),
                custom_instruction_status='APPLIED', resolved_overrides_json=dict(values=values, output_count=data['output_count'], per_item_overrides=[], notes=[]),
                instruction_summary=['全部素材均衡分配到成片', *summaries, '标题两行、前5秒；CTA最后5秒左侧'],
                instruction_issues=[], instruction_version='CUSTOM_REMIX_V1',
                instruction_driver='USER_STRUCTURED', instruction_model=None,
                instruction_parse_count=0, task_tag_snapshot=[])


def priority_paths(folder, names):
    """Only explicitly named direct child folders contribute source videos."""
    root = Path(folder).resolve(strict=True)
    paths = []
    for name in names:
        child = root / name
        if child.is_symlink() or not child.is_dir() or child.resolve().parent != root:
            raise ValueError('CUSTOM_REMIX_INVALID')
        rows = [p for p in child.iterdir() if p.is_file() and not p.is_symlink()
                and not p.name.startswith('.') and p.suffix.lower() in {'.mp4', '.mov', '.m4v', '.webm'}
                and not any(x in p.stem.lower() for x in ('.partial', '.temp'))]
        if not rows:
            raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
        paths.extend(sorted(rows, key=lambda p: p.name.casefold()))
    return paths


def source_priorities(sources, folder, names):
    root = Path(folder).resolve(strict=True)
    result = {}
    for source in sources:
        path = Path(source['path']).resolve(strict=True)
        if path.parent.parent != root or path.parent.name not in names:
            raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
        result[source['rank']] = names.index(path.parent.name)
    return result


def music_snapshot(home, obj, authorize):
    """Copy authorized local music before job creation; hash-check every copy."""
    from .review_media import inspect_media
    from .file_storage import copy_verified
    result = {}
    for rank, name in obj['music_paths'].items():
        source = Path(name).expanduser().resolve(strict=True)
        authorize(source)
        if source.suffix.lower() not in {'.m4a', '.mp3', '.wav'}:
            raise ValueError('SUPPLEMENT_MEDIA_INVALID')
        info = inspect_media(source, 'audio')
        folder = Path(home) / 'cache/custom-remix-music'
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (info['sha256'] + source.suffix.lower())
        copy_verified(source, destination)
        saved = inspect_media(destination, 'audio')
        if saved['sha256'] != info['sha256'] or inspect_media(source, 'audio')['sha256'] != info['sha256']:
            raise ValueError('SOURCE_HASH_CHANGED')
        result[rank] = dict(saved, source_file=str(source), download_status='READY')
    return result


def selection(sources, analyses, count, fast_interleaved=False, priorities=None):
    """Use every source once; longest-first allocation bounds duration spread."""
    if len(sources) < 2 * count or any(s['rank'] not in analyses for s in sources):
        raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
    bins = [[] for _ in range(count)]; lengths = [0.] * count
    if priorities is not None and set(priorities) != {s['rank'] for s in sources}:
        raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
    for source in sorted(sources, key=lambda s: ((priorities[s['rank']],) if priorities is not None else ()) + (-s['duration'], s['rank'])):
        choices = [n for n, rows in enumerate(bins) if len(rows) < 12]
        if not choices:
            raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
        n = min(choices, key=lambda n: (lengths[n], n))
        bins[n].append(source); lengths[n] += source['duration']
    timelines = [[dict(source_rank=s['rank'], start=0., end=s['duration'], reason='用户要求全部素材纳入，均衡分配') for s in rows] for rows in bins]
    needs_balance = any(len(clips) == 1 for clips in timelines)
    # Exchange equal-duration fragments if a long source occupies a whole bin.
    # Every original interval and each balanced output duration remain intact.
    for n, clips in enumerate(timelines):
        if len(clips) != 1:
            continue
        donors = [i for i, rows in enumerate(timelines) if i != n and 2 <= len(rows) < 12]
        if not donors:
            raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
        donor = min(donors, key=lambda i: (lengths[i], i))
        other = max(timelines[donor], key=lambda c: c['end']-c['start'])
        solo = clips[0]
        amount = min((other['end']-other['start'])/2, (solo['end']-solo['start'])/3)
        old_end, old_start = solo['end'], other['start']
        solo['end'] -= amount; other['start'] += amount
        clips.append(dict(other, start=old_start, end=old_start+amount))
        timelines[donor].append(dict(solo, start=solo['end'], end=old_end))
    if needs_balance:
        for _ in range(count * 12):
            hi = max(range(count), key=lambda i: lengths[i])
            receivers = [i for i, clips in enumerate(timelines) if i != hi and len(clips) < 12]
            if not receivers:
                raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
            lo = min(receivers, key=lambda i: lengths[i])
            if lengths[hi] - lengths[lo] <= 1.:
                break
            clip = max(timelines[hi], key=lambda c: c['end']-c['start'])
            amount = min((lengths[hi]-lengths[lo])/2, clip['end']-clip['start']-1.)
            if amount < .5:
                raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
            old_end = clip['end']; clip['end'] -= amount
            timelines[lo].append(dict(clip, start=clip['end'], end=old_end))
            lengths[hi] -= amount; lengths[lo] += amount
    outputs = []; by_rank = {s['rank']: s for s in sources}
    for n, clips in enumerate(timelines, 1):
        clips.sort(key=lambda c: ((priorities[c['source_rank']],) if priorities is not None else ()) + (c['source_rank'], c['start']))
        if fast_interleaved:
            # Preserve every assigned source, choosing bounded windows across
            # each original and alternating them without overlapping ranges.
            groups = []
            for clip in clips:
                length = clip['end'] - clip['start']; keep = min(length, 36. / len(clips))
                slots = min(12 // len(clips), max(1, math.ceil(keep / 3.)))
                width = keep / slots
                if width < .5:raise ValueError('CUSTOM_REMIX_SOURCE_COVERAGE_REQUIRED')
                groups.append([dict(clip, start=clip['start'] + (length-width)*i/max(1,slots-1),
                                    end=min(clip['end'],clip['start'] + (length-width)*i/max(1,slots-1) + width),
                                    reason='全部源素材覆盖；拆分短镜头交替拼接') for i in range(slots)])
            clips = [group[i] for i in range(max(map(len,groups))) for group in groups if i < len(group)]
        rows = [by_rank[c['source_rank']] for c in clips]
        primary = next((s for s in rows if analyses[s['rank']].get('clean_script', '').strip()), rows[0])
        angle = '按文件夹优先级开场，同任务全部素材均衡组合' if priorities is not None else '快节奏促销：同任务全素材覆盖' if fast_interleaved else '爱情礼物：同任务全素材均衡组合'
        outputs.append(dict(rank=n, primary_source_rank=primary['rank'], angle=angle,
                            clips=clips))
    return dict(outputs=outputs)


def contexts(job, items, analyses, product, language, brief):
    obj = job['custom_remix']; result = []; mapping = {}; by_rank = {s['rank']: s for s in items}
    for group in obj['script_groups']:
        key = digest(['CUSTOM_REMIX_V1', group, [(r, by_rank[r]['remix_clips']) for r in group['ranks']]])
        rows = [analyses[r] for r in group['ranks']]
        points = list(dict.fromkeys(p for a in rows for p in a['selling_points']))
        claims = list(dict.fromkeys(p for a in rows for p in a['allowed_claims']))
        promotional = group['angle'] == 'HARD_SELL_PROMOTION'
        generic = group['angle'] == 'VERIFIED_PRODUCT'
        if promotional:
            key = digest([key, job['market'], language, obj['promotion_offer']])
            basis = ('Generate a NEW distinct '+language+' sales manuscript for THIS verified product. '
                     'For es-MX use colloquial Mexican Spanish. Open with a strong attention hook in the first two seconds, '
                     'show an evidence-supported travel problem and visible benefit, then use the supplied merchant promotion '
                     'and a direct shopping CTA. Keep brisk delivery and one coherent idea. Do not translate source narration, '
                     'invent a testimonial, product feature, price, stock count, exact deadline or additional offer. '
                     'Vary the hook and wording from other output ranks. Aim for the supplied footage duration.')
            promotion = 'USER_PROVIDED_MERCHANT_OFFER: '+obj['promotion_offer']
            claims = [obj['promotion_offer'], *claims]
            title_note = ' Concise hard-sell hooks may use this supplied offer: '+obj['promotion_offer']
        elif generic:
            key = digest([key, job['market'], language])
            basis = ('Create a natural ' + language + ' product narration for THIS product using only the verified footage, '
                     'supported selling points and reliable original narration. Start with an evidence-supported need or benefit, '
                     'follow with visible product details and end with a natural shopping CTA. Vary wording for this output. '
                     'Do not invent a testimonial, product feature, certification, price, offer or stock claim. '
                     'Aim for the supplied footage duration with natural pauses.')
            promotion = 'Natural shopping CTA. Do not invent prices, stock or discounts.'
            title_note = ' Use concise product hooks grounded in the verified footage.'
        else:
            basis = ANGLES[group['angle']] + ' Build from audience need, an evidence-supported benefit and a fitting use scene. Keep one distinct gift idea per manuscript; do not list every feature. Aim for the supplied duration with natural speech and pauses.'
            promotion = 'Natural shopping CTA. Do not invent prices, stock or discounts.'
            title_note = ' Prefer concise romantic gift hooks.'
        result.append(dict(script_group_id=key, dedupe_reviewed=True, source_language='unknown', source_script=basis, clean_script=basis,
                           video_context=(basis + '\nVerified footage: ' + '\n'.join(a['context'] for a in rows))[:6000], product=product['product_name'],
                           supported_selling_points=points[:40], allowed_claims=claims[:40], blocked_claims=[], promotion_context=promotion,
                           target_duration_seconds=min(by_rank[r]['duration'] for r in group['ranks']), market=job['market'], language=language, title_count=3,
                           title_brief=brief + ' Titles must fit exactly two lines at 48–68px. Never three lines.'+title_note))
        for rank in group['ranks']:
            mapping[rank] = key
    # Music rows reuse target-language title candidates; never request their TTS.
    for rank in obj['music_only_ranks']:
        mapping[rank] = result[0]['script_group_id']
    return result, mapping, {c['script_group_id']: copy.deepcopy(c) for c in result}
