"""At most five deduped groups per HTTP call; item-level cache and repair.

No TTS/approval. A/B/C are retained verbatim for editorial review then the user.
Existing successful single-group caches keep their original keys and behavior.
"""
from pathlib import Path
from .deepseek_copy import ROOT, MODEL, prepare, validation, safe
from .headless_runtime import read, write, locked, fingerprint, sha, Profiler
from app.s2.deepseek import DeepSeekAdapter, DeepSeekError

BATCH_VERSION = 'DEEPSEEK_TIKTOK_BR_BATCH_V1'


def generate_batch(contexts, work, adapter_factory=DeepSeekAdapter):
    directory = Path(work) / 'scripts/deepseek'; directory.mkdir(parents=True, exist_ok=True)
    profiler = Profiler(work)
    contract = (ROOT / 'prompts' / (BATCH_VERSION+'.txt')).read_text()
    groups = {}; ids = []; outcomes = {}; counters = dict(http_requests=0, cache_hits=0, item_repairs=0, network_retries=0)
    for index,c in enumerate(contexts):
        try:c, spec, _, messages = prepare(dict(c, title_count=3))
        except (ValueError,TypeError,DeepSeekError):
            key='invalid-'+str(index);ids.append((c.get('script_group_id',key) if isinstance(c,dict) else key,key))
            outcomes[key]=dict(status='REVIEW_REQUIRED',error='INVALID_DEEPSEEK_CONTEXT');continue
        spec.update(batch_prompt_version=BATCH_VERSION, batch_prompt_sha256=fingerprint(contract))
        key = fingerprint(spec); ids.append((c['script_group_id'], key))
        groups[key] = (c, spec, messages)
    # Whole batch lock avoids partially overlapping paid requests across processes.
    with locked(directory / '.batch.lock'):
        pending = []
        for key, (c, spec, _) in groups.items():
            cache = directory / (key+'.json'); old = read(cache)
            approved=read(Path(work)/'title_approvals'/(fingerprint(c['script_group_id'])+'.json'))
            if approved and approved.get('fingerprint')!=key:
                outcomes[key]=dict(status='REVIEW_REQUIRED',error='APPROVED_INPUT_CHANGED_EXPLICIT_REVISION_REQUIRED')
                continue
            if old:
                try:
                    if old['request_context'] != spec or old['response_sha256'] != fingerprint(old['response']):
                        raise ValueError()
                    validation(old['response'], c)
                    outcomes[key] = old | dict(cache_hit=True, new_requests=0)
                    if approved:outcomes[key].update(title_status='USER_APPROVED',selected_title=approved['selected_title'])
                    counters['cache_hits'] += 1
                    with profiler.span('DEEPSEEK', c['script_group_id'], cache_hit=True): pass
                except (ValueError, KeyError, DeepSeekError):
                    outcomes[key] = dict(status='REVIEW_REQUIRED', error='CACHE_INTEGRITY_FAILED')
            elif (directory / (key+'.attempt.json')).exists():
                outcomes[key] = dict(status='REVIEW_REQUIRED', error='PRIOR_ATTEMPT_REVIEW_REQUIRED')
            else:
                pending.append(key)
        if not pending:
            return dict(items=[dict(outcomes[k], script_group_id=i) for i,k in ids], **counters)
        try:
            adapter = adapter_factory()
        except DeepSeekError as e:
            for key in pending: outcomes[key] = dict(status='REVIEW_REQUIRED', error=e.code)
            return dict(items=[dict(outcomes[k], script_group_id=i) for i,k in ids], **counters)

        def request(keys, repair=False):
            system = groups[keys[0]][2][0]['content'] + '\n' + contract
            payload = {'items': [dict(id=k, context=groups[k][1]['context']) for k in keys]}
            messages = [dict(role='system', content=system), dict(role='user', content=__import__('json').dumps(payload, ensure_ascii=False))]
            for k in keys:
                write(directory / (k+'.attempt.json'), dict(state='IN_FLIGHT', repair=repair))
            retry = 0
            while True:
                try:
                    with profiler.span('DEEPSEEK', ','.join(groups[k][0]['script_group_id'] for k in keys), retry_count=int(repair)+retry):
                        counters['http_requests'] += 1
                        response = adapter.complete(MODEL, messages, max_tokens=min(8192, 2200*len(keys)))
                    safe(response)
                    break
                except DeepSeekError as e:
                    if e.retry_safe and retry == 0:
                        retry = 1; counters['network_retries'] += 1; continue
                    for k in keys:
                        outcomes[k] = dict(status='REVIEW_REQUIRED', error=e.code,
                                           uncertain=e.uncertain, cache_hit=False)
                        write(directory/(k+'.attempt.json'), dict(state='REVIEW_REQUIRED', error=e.code))
                    return []  # Broken/uncertain whole envelope: no paid speculative replay.
            rows = response.get('data', {}).get('items', [])
            invalid = []
            for k in keys:
                matches = [r for r in rows if isinstance(r, dict) and r.get('id') == k] if isinstance(rows, list) else []
                try:
                    if len(matches) != 1: raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
                    result = validation(matches[0].get('result'), groups[k][0])
                    if [t['angle'] for t in result['titles']] != ['pain_hook','product_benefit','emotion']:
                        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
                    if any(len(t['text'].split()) > 10 for t in result['titles']):
                        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')
                    record = dict(fingerprint=k, script_group_id=groups[k][0]['script_group_id'], request_context=groups[k][1], response=result,
                                  response_sha256=fingerprint(result), status='REVIEW', cache_hit=False,
                                  provider_metadata={x: response.get(x) for x in ('usage','request_id','model','latency_seconds')},
                                  new_requests=1+int(repair), item_repaired=repair)
                    write(directory/(k+'.json'), record)
                    write(directory/(k+'.attempt.json'), dict(state='SUCCEEDED'))
                    write(Path(work)/'checkpoints'/('deepseek_'+k+'.json'),dict(state='DONE',checkpoint='DEEPSEEK_DONE',
                         response_path=str(directory/(k+'.json')),response_sha256=fingerprint(result)))
                    outcomes[k] = record
                except DeepSeekError as e:
                    outcomes[k] = dict(status='REVIEW_REQUIRED', error=e.code, cache_hit=False)
                    write(directory/(k+'.attempt.json'), dict(state='REVIEW_REQUIRED', error=e.code))
                    # Claims need editorial review, not another blind paid rewrite.
                    if e.code == 'INVALID_DEEPSEEK_OUTPUT' and not repair: invalid.append(k)
            return invalid
        try:
            for offset in range(0, len(pending), 5):
                bad = request(pending[offset:offset+5])
                for k in bad:
                    counters['item_repairs'] += 1
                    request([k], repair=True)
        finally:
            adapter.close()
    summary = dict(items=[dict(outcomes[k], script_group_id=i) for i,k in ids], **counters)
    write(directory / 'batch_summary.json', summary)
    profiler.report(Path(work).parent/'RUN_REPORT.md')
    return summary


def approve_title(record, review, work):
    """Codex factual/fit review plus explicit user selection, no title rewriting."""
    from .deepseek_copy import REVIEW_CHECKS
    from datetime import datetime, timezone
    if (review.get('title_status')!='USER_APPROVED' or not review.get('user_confirmation')
        or review.get('response_sha256')!=record['response_sha256']
        or fingerprint(record['response'])!=record['response_sha256']
        or set(review.get('checks',{}))!=REVIEW_CHECKS or not all(x is True for x in review['checks'].values())):
        raise ValueError('TITLE_FACT_AND_USER_REVIEW_REQUIRED')
    title=next((t for t in record['response']['titles'] if t['id']==review.get('selected_title_id')),None)
    if not title:raise ValueError('TITLE_NOT_IN_CANDIDATES')
    result=dict(fingerprint=record['fingerprint'],checkpoint='TITLE_APPROVED',title_status='USER_APPROVED',
                selected_title=title['text'],selected_title_id=title['id'],title_candidates=record['response']['titles'],
                approved_at=datetime.now(timezone.utc).isoformat(),review=review)
    write(Path(work)/'title_approvals'/(fingerprint(record['script_group_id'])+'.json'),result)
    return result
