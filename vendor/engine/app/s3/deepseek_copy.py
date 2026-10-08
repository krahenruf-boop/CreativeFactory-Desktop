"""Script-group localization/cache/review gate; no TTS or renderer calls."""
import fcntl
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.s2.deepseek import DeepSeekAdapter, DeepSeekError

ROOT = Path(__file__).resolve().parents[2]
PROMPT_VERSION = 'DEEPSEEK_TIKTOK_BR_V1'
MODEL = 'deepseek-chat'
REVIEW_CHECKS = {'video_facts','selling_points','no_invented_claims','natural_ptbr',
                 'ugc_delivery','speaker_fit','length','hook_fit'}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Context(Strict):
    script_group_id: str = Field(min_length=1, max_length=100)
    dedupe_reviewed: Literal[True]
    source_language: str = Field(min_length=2, max_length=40)
    source_script: str = Field(min_length=1, max_length=20000)
    clean_script: str = Field(min_length=1, max_length=16000)
    video_context: str = Field(min_length=1, max_length=6000)
    product: str = Field(min_length=1, max_length=2000)
    supported_selling_points: list[str] = Field(max_length=40)
    allowed_claims: list[str] = Field(max_length=40)
    blocked_claims: list[str] = Field(max_length=40)
    promotion_context: str | None = Field(default=None, max_length=2000)
    target_duration_seconds: float = Field(gt=0, le=300)
    market: Literal['Brazil'] = 'Brazil'
    language: Literal['pt-BR'] = 'pt-BR'
    title_count: Literal[3, 5, 10] = 10
    title_brief: str | None = Field(default=None, max_length=3000)


class Localized(Strict):
    language: Literal['pt-BR']
    text: str = Field(min_length=15, max_length=16000)
    estimated_style: Literal['ugc_tiktok']
    notes: list[str] = Field(default_factory=list, max_length=20)


class Title(Strict):
    id: int = Field(ge=1, le=10)
    text: str = Field(min_length=1, max_length=150)
    angle: str = Field(min_length=1, max_length=50)


class Titles(Strict):
    titles: list[Title] = Field(min_length=3, max_length=10)


class Result(Titles):
    localized_script: Localized


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalized(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',text.casefold())
                            if not unicodedata.combining(c) and not unicodedata.category(c).startswith('P')).split())


def safe(value):
    """Reject secret-shaped content before any public artifact is written."""
    if re.search(r'\bsk[-_][A-Za-z0-9_-]{12,}',canonical(value)):
        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    return value


def save(path, value):
    safe(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)


def validation(data, context, titles_only=False):
    try:
        out=(Titles if titles_only else Result).model_validate(safe(data)).model_dump()
    except (ValidationError,DeepSeekError):
        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT') from None
    titles=out['titles']
    count=context.get('title_count',10)
    if sorted(t['id'] for t in titles)!=list(range(1,count+1)) or len({normalized(t['text']) for t in titles})!=count:
        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    texts=[t['text'] for t in titles]
    if not titles_only:
        text=out['localized_script']['text']
        texts.append(text)
        words=set(normalized(text).split())
        pt=len(words & set('voce voces seu sua sem com para nao uma um so e no na que isso esse essa olha cinto calca passar prender gancho pode mais conforto fivela dois vem cores'.split()))
        en=len(words & set('the this that your you with without is are and have belt buckle it into'.split()))
        es=len(words & set('usted puedes tienes este estos esto cinturón hebilla ahora también'.split()))
        if pt < 3 or en > pt or es > pt:
            raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    if any(not t.strip() or re.search(r'[\u3400-\u9fff]|```|\*\*|#[\wÀ-ÿ]|^\s*(?:\d+[.)：:]|[-*•])\s|\[[^\]]+\]\(',t) for t in texts):
        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    if any(len(t['text'].split())>14 for t in titles):
        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    combined=' '.join(normalized(t) for t in texts)
    punctuation_kept=' '.join(''.join(c for c in unicodedata.normalize('NFKD',t.casefold())
                                     if not unicodedata.combining(c)) for t in texts)
    for blocked in context['blocked_claims']:
        phrase=normalized(blocked)
        if phrase and re.search(r'(?<!\w)'+re.escape(phrase)+r'(?!\w)',combined):
            raise DeepSeekError('DEEPSEEK_CLAIM_REVIEW_REQUIRED')
    promotion=r'\b(desconto|promocao|oferta|so hoje|ultimas unidades|estoque limitado|frete gratis|primeiros pedidos|carrinho|compre agora|off)\b|\d+\s*%|r\$'
    if not context['promotion_context'] and re.search(promotion,punctuation_kept):
        raise DeepSeekError('DEEPSEEK_CLAIM_REVIEW_REQUIRED')
    if context['promotion_context'] and sum(bool(re.search(promotion,normalized(t['text']))) for t in titles)>3:
        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    for title in titles:
        words=set(normalized(title['text']).split())
        if len(words & {'the','your','you','with','without','belt','buckle','this','these'})>=2:
            raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
    # Claim equivalence and natural pt-BR still require Codex's explicit review.
    return out


def prepare(context, mode='localize', approved_script=None, title_context=None, model=MODEL):
    c=Context.model_validate(context).model_dump()
    prompt=(ROOT/'prompts'/f'{PROMPT_VERSION}.txt').read_text()
    payload={k:v for k,v in c.items() if k not in {'script_group_id','dedupe_reviewed'}}
    # Per-task title counts do not change the frozen V1 facts/localization rules.
    # Preserve fingerprints of existing default ten-title requests.
    if c['title_count']==10:
        payload.pop('title_count')
    if c['title_brief'] is None:
        payload.pop('title_brief')
    if c['title_count']!=10 or c['title_brief'] is not None:
        prompt += ('\n\nPer-request title contract (overrides ONLY the title count above): '
                   f'Return exactly {c["title_count"]} unique titles, IDs 1 through {c["title_count"]}, '
                   'including in titles_only mode. Use title_brief as the requested creative direction, '
                   'always subordinate to the supported facts and blocked claims. '
                   'This result is for human title approval; do not select or approve a title.')
    if mode=='titles_only':
        if not approved_script or not title_context:
            raise ValueError('APPROVED_SCRIPT_AND_TITLE_CONTEXT_REQUIRED')
        payload.update(task='titles_only',approved_localized_script=approved_script,title_context=title_context)
    elif mode!='localize':
        raise ValueError('INVALID_COPY_MODE')
    spec=dict(provider='deepseek',model=model,prompt_version=PROMPT_VERSION,
              prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),mode=mode,context=payload)
    safe(spec)
    return c,spec,digest(spec),[{'role':'system','content':prompt}, {'role':'user','content':canonical(payload)}]


def generate_group(context, work, adapter_factory=DeepSeekAdapter, model=MODEL,
                   mode='localize', approved_script=None, title_context=None):
    """One result per canonical unique group/context, including cross-process lock.

    Successful, failed and uncertain attempts are durable: refresh/restart never
    repeats a paid request. Only a pre-send connection failure may retry once.
    """
    c,spec,key,messages=prepare(context,mode,approved_script,title_context,model)
    directory=Path(work)/'scripts/deepseek';directory.mkdir(parents=True,exist_ok=True)
    cache=directory/(key+'.json');ledger=directory/(key+'.attempt.json')
    with (directory/(key+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if cache.exists():
            record=json.loads(cache.read_text())
            if record['fingerprint']!=key or record['request_context']!=spec or digest(record['response'])!=record['response_sha256']:
                return dict(status='REVIEW_REQUIRED',error='CACHE_INTEGRITY_FAILED',fingerprint=key,new_requests=0,cache_hit=False)
            validation(record['response'],c,mode=='titles_only')
            return dict(record,status='REVIEW',new_requests=0,cache_hit=True)
        if ledger.exists():
            prior=json.loads(ledger.read_text())
            return dict(status='REVIEW_REQUIRED',error=prior.get('error') or 'BILLING_UNCERTAIN',
                        fingerprint=key,new_requests=0,cache_hit=False,prior_attempt=prior)
        try:
            adapter=adapter_factory()
        except DeepSeekError as exc:
            return dict(status='REVIEW_REQUIRED',error=exc.code,fingerprint=key,new_requests=0,cache_hit=False)
        state=dict(fingerprint=key,status='IN_FLIGHT',requests=0,network_retries=0,json_repairs=0,
                   cost_state='UNKNOWN',attempts=[])
        try:
            while True:
                state['requests']+=1;save(ledger,state)
                try:
                    response=adapter.complete(model,messages)
                    state['attempts'].append({k:response[k] for k in ['usage','request_id','model','latency_seconds']})
                    result=validation(response['data'],c,mode=='titles_only')
                    record=dict(fingerprint=key,request_context=spec,script_group_id=c['script_group_id'],
                                response=result,response_sha256=digest(result),provider_metadata=state['attempts'],
                                status='REVIEW',requests=state['requests'],network_retries=state['network_retries'],
                                json_repairs=state['json_repairs'],cost_state='UNKNOWN')
                    save(cache,record)
                    state['status']='SUCCEEDED';save(ledger,state)
                    return dict(record,cache_hit=False,new_requests=state['requests'])
                except DeepSeekError as exc:
                    if exc.retry_safe and state['network_retries']<1:
                        state['network_retries']+=1
                        continue
                    if exc.code in {'DEEPSEEK_INVALID_JSON','INVALID_DEEPSEEK_OUTPUT'} and state['json_repairs']<1:
                        state['json_repairs']+=1
                        # Never feed exception bodies or unsafe output into the repair prompt.
                        messages=messages+[{'role':'user','content':'Correction: previous output failed '+exc.code+f'. Return valid JSON satisfying all original constraints, exactly {c["title_count"]} unique titles, no Markdown. No explanation.'}]
                        continue
                    state.update(status='BILLING_UNCERTAIN' if exc.uncertain else 'REVIEW_REQUIRED',error=exc.code)
                    save(ledger,state)
                    return dict(status='REVIEW_REQUIRED',error=exc.code,uncertain=exc.uncertain,
                                fingerprint=key,new_requests=state['requests'],cache_hit=False,
                                network_retries=state['network_retries'],json_repairs=state['json_repairs'])
        finally:
            adapter.close()


def approve(record, review, directory):
    """Only Codex's explicit content review unlocks final_script, never API success."""
    if (review.get('reviewer')!='Codex' or review.get('response_sha256')!=record['response_sha256']
        or set(review.get('checks',{}))!=REVIEW_CHECKS or not all(v is True for v in review['checks'].values())
        or not review.get('selection_reason','').strip()):
        raise ValueError('CODEX_REVIEW_REQUIRED')
    if digest(record['response'])!=record['response_sha256']:
        raise ValueError('RESPONSE_CHANGED_REVIEW_REQUIRED')
    title=next((t for t in record['response']['titles'] if t['id']==review.get('selected_title_id')),None)
    if not title:
        raise ValueError('SELECTED_TITLE_NOT_IN_CANDIDATES')
    if record['request_context']['mode']=='titles_only':
        text=record['request_context']['context']['approved_localized_script']
    else:
        text=record['response']['localized_script']['text']
    result=dict(final_ptbr_script=text,title_candidates=record['response']['titles'],selected_title=title['text'],
                selected_title_id=title['id'],selection_reason=review['selection_reason'],
                review=review,status='CODEX_APPROVED',user_quality_confirmation='PENDING')
    save(Path(directory)/'review.json',result)
    (Path(directory)/'final_script.txt').write_text(text+'\n')
    save(Path(directory)/'titles.json',dict(candidates=result['title_candidates'],selected_title=title,
                                           selection_reason=review['selection_reason']))
    return result


def regenerate_titles(record, video_context, feedback, work, adapter_factory=DeepSeekAdapter):
    """One title regeneration per original result and video context, ever.

    Spoken text is pinned from the successful localization and cannot be changed.
    A different video context can request its own titles without translating again.
    """
    if digest(record['response'])!=record['response_sha256'] or 'localized_script' not in record['response']:
        raise ValueError('LOCALIZATION_RESULT_REQUIRED')
    context=record['request_context']['context'] | dict(script_group_id=record['script_group_id'],dedupe_reviewed=True)
    tag=digest([record['fingerprint'],video_context])
    gate=Path(work)/'scripts/deepseek'/f'{tag}.title_regeneration.json'
    with gate.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if gate.exists():
            return json.loads(gate.read_text()) | dict(new_requests=0,cache_hit=True)
        save(gate,dict(status='REVIEW_REQUIRED',error='BILLING_UNCERTAIN',new_requests=0))
        result=generate_group(context,work,adapter_factory,model=record['request_context']['model'],
            mode='titles_only',approved_script=record['response']['localized_script']['text'],
            title_context=dict(video_context=video_context,feedback=feedback))
        save(gate,result)
        return result
