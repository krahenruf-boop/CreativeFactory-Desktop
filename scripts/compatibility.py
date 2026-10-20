"""Small packaging-only bindings; preserve the source engine and frozen style."""
from pathlib import Path
import json
B=Path(__file__).resolve().parents[1];E=B/'staging/engine'
# Existing Brazil copy rules unchanged. Explicit per-job market extension for Mexico.
p=E/'app/s3/deepseek_copy.py';s=p.read_text()
s=s.replace("market: Literal['Brazil']", "market: Literal['Brazil','Mexico']").replace("language: Literal['pt-BR']", "language: Literal['pt-BR','es-MX']")
s=s.replace("if pt < 3 or en > pt or es > pt:","if context.get('language','pt-BR')=='pt-BR' and (pt < 3 or en > pt or es > pt):")
s=s.replace("titles=out['titles']", "if not titles_only and out['localized_script']['language']!=context.get('language','pt-BR'): raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')\n    titles=out['titles']")
needle="prompt=(ROOT/'prompts'/f'{PROMPT_VERSION}.txt').read_text()"
s=s.replace(needle,needle+"\n    if c['language']=='es-MX':\n        prompt += '\\nMarket override for this request: Mexico, natural Mexican Spanish es-MX for ALL scripts and titles. This overrides only language/market, never the factual, cache or JSON rules. Avoid European Spanish. Keep exactly the requested number of titles.'")
# Optional metadata: omission cannot trigger paid repairs or stop auto production.
s=s.replace('Field, ValidationError','Field, ValidationError, field_validator')
s=s.replace('class Localized(Strict):','''class Comparison(Strict):
    text_zh: str = ''
    @field_validator('text_zh', mode='before')
    @classmethod
    def faithful_chinese(cls, value):
        if not isinstance(value,str) or len(value)>24000 or not re.search(r'[\\u3400-\\u9fff]',value): return ''
        return value.strip()


class Localized(Comparison):''')
s=s.replace('class Title(Strict):','class Title(Comparison):')
# Included in prompt_sha256 automatically, shared across BR/MX and every model.
s=s.replace("    if mode=='titles_only':", '''    prompt += '\\nCOPY_PRESENTATION_ZH_V1: In the SAME response, add text_zh to localized_script and EACH title (also every batch item and titles_only). text remains exclusively the target language. text_zh is a complete faithful natural Simplified Chinese translation for human comparison only. Do not add claims, numbers or stronger marketing than the corresponding text. Keep all existing JSON fields and constraints. This extends the earlier JSON schema with text_zh; never place Chinese in text.'
    if mode=='titles_only':''',1)
p.write_text(s)

# Explicit montage black-border repair; original source files remain unchanged.
from parallel_tasks_compatibility import apply as apply_parallel_tasks
apply_parallel_tasks(E)

from montage_frame_compatibility import apply as apply_montage_frame
apply_montage_frame(E)
from source_frame_compatibility import apply as apply_source_frame
apply_source_frame(E)

# Keep approved emoji in title rasters without replacing the frozen word font.
from symbol_font_compatibility import apply as apply_symbol_font
apply_symbol_font(E)
from source_title_overlay_compatibility import apply as apply_source_title_overlay
apply_source_title_overlay(E)

# Evidence-bound title blur cover: a packaging raster adapter, not an overlay
# algorithm rewrite. The normal visual preset and mother engine remain intact.
p=E/'app/s3/headless_media.py';s=p.read_text()
s=s.replace("    validate_montage(plan)\n    failures = render_gate(plan)", "    validate_montage(plan)\n    if plan.get('cta_policy'):\n        from autocut.cta_policy import check_plan\n        check_plan(plan)\n    failures = render_gate(plan)")
s=s.replace("    timeline = text_timeline(visual,e,folder)", "    if plan.get('original_text_cleanup'):\n        from autocut.original_text_cover import apply_layers\n    else:\n        from autocut.title_blur_cover import apply_layers\n    apply_layers(plan,e,folder)\n    timeline = text_timeline(visual,e,folder)")
s=s.replace("    files = [plan['source']['path'], plan['audio']['audio_path']]", "    files = [plan['source']['path'], plan['audio']['audio_path']]\n    files.extend(e['path'] for e in plan.get('title_blur_white_cover',{}).get('region',{}).get('evidence',[]))\n    files.extend(e['path'] for e in plan.get('original_text_cleanup',{}).get('spec',{}).get('evidence',[]))")
p.write_text(s)
p=E/'app/s3/overlay_policy.py';s=p.read_text()
s=s.replace("target_duration=plan.get('target_duration'))", "target_duration=plan.get('target_duration'), **({'title_blur_white_cover':plan['title_blur_white_cover']} if plan.get('title_blur_white_cover') else {}), **({'original_text_cleanup':plan['original_text_cleanup']} if plan.get('original_text_cleanup') else {}), **({'cta_policy':plan['cta_policy'], 'cta':plan['visual']['cta'], 'cta_retained_evidence':plan.get('cta_retained_evidence')} if plan.get('cta_policy') else {}))")
p.write_text(s)
# Production language contract: copied adapter only; the mother engine is untouched.
p=E/'app/s3/deepseek_copy.py';s=p.read_text()
s=s.replace('class Title(Comparison):','class Title(Comparison):\n    language: str = Field(min_length=2, max_length=40)')
s=s.replace("    titles=out['titles']", """    from autocut.script_language import check
    try:
        if not titles_only:check(out['localized_script'],context['language'],require_metadata=True)
        for title in out['titles']:check(title,context['language'],'TITLE',True)
    except ValueError:raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT') from None
    titles=out['titles']""")
s=s.replace("    if mode=='titles_only':", """    from autocut.script_language import NAMES
    prompt += '\\nFINAL_LANGUAGE_V1 OUTPUT LANGUAGE: '+NAMES.get(c['language'],c['language'])+'. Every localized_script and title MUST include language equal to context.language. Never use source_language as the output language. Source English/Chinese is evidence only, never final copy. text_zh remains Chinese review metadata.'
    if mode=='titles_only':""",1)
p.write_text(s)
p=E/'prompts/DEEPSEEK_TIKTOK_BR_BATCH_V1.txt'
p.write_text(p.read_text()+'\nFINAL_LANGUAGE_V1: EACH result.localized_script and result.titles entry requires language matching its own context.language. es-MX means Mexican Spanish; pt-BR means Brazilian Portuguese. Never copy an English source into localized text.\n')
# Forward the immutable per-job model through the existing batch/cache contract.
# prepare() already fingerprints model; no prompt or validation rule changes.
p=E/'app/s3/deepseek_batch.py';s=p.read_text()
s=s.replace('def generate_batch(contexts, work, adapter_factory=DeepSeekAdapter):','def generate_batch(contexts, work, adapter_factory=DeepSeekAdapter, model=MODEL):')
s=s.replace('prepare(dict(c, title_count=3))','prepare(dict(c, title_count=3), model=model)')
s=s.replace('adapter.complete(MODEL, messages,','adapter.complete(model, messages,')
p.write_text(s)
# Current Flash/Pro endpoints default to thinking. Copy-only requests use their
# documented non-thinking mode, retaining the existing bounded JSON response.
p=E/'app/s2/deepseek.py';s=p.read_text()
s=s.replace("'max_tokens':max_tokens, 'stream':False, 'temperature':.7}","'max_tokens':max_tokens, 'stream':False, 'temperature':.7,\n                      **({'thinking':{'type':'disabled'}} if model in {'deepseek-flash','deepseek-v4-pro'} else {})}")
p.write_text(s)
# Runtime path resolution, not upstream model changes.
p=E/'app/s3/experimental_propainter.py';s=p.read_text()
s=s.replace("self.python = self.root / '.venv-propainter/bin/python'", "self.python = Path(os.environ['AUTOCUT_RESOURCES']) / 'bin/propainter-python'")
s=s.replace("self.cli = self.root / '.venv-propainter/bin/propainter-delogo'", "self.cli = Path(os.environ['AUTOCUT_RESOURCES']) / 'bin/propainter-delogo'")
s=s.replace("self.upstream = self.root / 'tools/third_party/propainter-delogo/third_party/ProPainter'", "self.upstream = Path(os.environ['AUTOCUT_RESOURCES']) / 'third_party/propainter-delogo/third_party/ProPainter'")
# Keep historical stage names readable; the removed engine cannot run.
if "raise ValueError('PROPAINTER_REMOVED')" not in s:
 s=s.replace('    if spec.get(\'usage_scope\') not in SCOPES:',
             "    raise ValueError('PROPAINTER_REMOVED')\n    if spec.get('usage_scope') not in SCOPES:")
p.write_text(s)
p=E/'app/s3/overlay_policy.py';s=p.read_text()
s=s.replace('ALLOW_EXPERIMENTAL_PROPAINTER = True', 'ALLOW_EXPERIMENTAL_PROPAINTER = False')
p.write_text(s)
# Distinct driver labels carry identical approval semantics; legacy engine cache IDs remain intact.
print('Packaging path/locale compatibility applied to staging only')
# Task-scoped visual exceptions are bounded and recorded; frozen preset stays untouched.
p=E/'app/s3/visual_preset.py';s=p.read_text()
s=s.replace('def caption_events(alignment, canvas=(1080, 1920)):', 'def caption_events(alignment, canvas=(1080, 1920), style=None):')
s=s.replace("    style = load_preset()['subtitle']\n    words = words_from_alignment(alignment)","    style = style or load_preset()['subtitle']\n    words = words_from_alignment(alignment)")
s=s.replace("    p = plan['preset_snapshot']\n    errors = []", """    p = plan['preset_snapshot']
    errors = []
    task = plan.get('task_rules', {})
    if task:
        if (set(task) != {'title_enabled','title_duration','subtitle_font_size'}
            or type(task['title_enabled']) is not bool
            or type(task['title_duration']) not in (float,int)
            or not 1 <= task['title_duration'] <= 5
            or type(task['subtitle_font_size']) is not int
            or not 48 <= task['subtitle_font_size'] <= 56):
            return ['CUSTOM_INSTRUCTION_INVALID']
        if not task['title_enabled'] and (plan['title']['text'] or not plan.get('user_revision',{}).get('omit_added_title')):
            errors.append('TITLE_USER_EXCEPTION_MISSING')""")
s=s.replace("        exemptions = set()", "        exemptions = set()\n        if task and layer == 'subtitle' and s.get('font_size') == task['subtitle_font_size']:\n            exemptions.add('font_size')")
s=s.replace("!= (0, min(5., d)):","!= (0, min(task.get('title_duration',5.), d)):")
p.write_text(s)

# Extend the copied batch envelope last, including mixed-market batches.
p=E/'prompts/DEEPSEEK_TIKTOK_BR_BATCH_V1.txt';s=p.read_text()
s += '\nCOPY_PRESENTATION_ZH_V1: The result.localized_script and every result.titles entry also require text_zh, a faithful complete Simplified Chinese translation. text must use that item context.language (pt-BR, es-MX), overriding the Brazil examples above. Chinese is metadata only; never add it to text. No extra or stronger claims in translation.\n'
p.write_text(s)

# Subtitles remain above the arrow, including when their pixels overlap.
from cta_text_layers import apply as apply_cta_text_layers
apply_cta_text_layers(E)

# User-trained hooks: no word quota or forced angle slots. Only the copied
# engine is patched; source snapshots and existing approved titles stay intact.
from title_style_compatibility import apply as apply_title_style
apply_title_style(E)

# 0.1.15 automatic QA: technical integrity remains mandatory; ordinary visual
# warnings are explicit policy evidence instead of fabricated all-true checks.
p=E/'app/s3/headless_media.py';s=p.read_text()
s=s.replace("review.get('review_contract')!='AUTOCUT_REVIEW_V1'", "review.get('review_contract') not in {'AUTOCUT_REVIEW_V1','AUTOCUT_AUTO_QA_V2'}")
s=s.replace("not all(v is True for v in review['checks'].values())", "not (all(v is True for v in review['checks'].values()) or review.get('review_contract')=='AUTOCUT_AUTO_QA_V2' and review.get('policy_status') in {'PASS','WARNING'} and review.get('policy_evidence',{}).get('policy')=='AUTO_PRODUCTION_V1' and review.get('policy_evidence',{}).get('status')==review.get('policy_status'))")
p.write_text(s)
