"""Additive operational evidence. No raw exceptions, prompts or credentials."""
import contextlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from app.s3.headless_runtime import read, write, locked, fingerprint, union_seconds

VERSION = 'OPERATIONS_V1'
HUMAN = {'WAITING_SCRIPT_APPROVAL', 'WAITING_TITLE_APPROVAL', 'WAITING_SUPPLEMENT_MEDIA'}
FAILURE = {'FAILED', 'REVIEW_REQUIRED', 'INTERRUPTED'}


def diagnostic(error):
    value = getattr(error, 'code', None) or str(error)
    code = value if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_:,-]{1,99}', value) else 'STAGE_FAILED'
    result = dict(code=code, category=type(error).__name__[:64])
    exit_code = getattr(error, 'returncode', None)
    if type(exit_code) is int:result['exit_code'] = exit_code
    if isinstance(error, TimeoutError) or type(error).__name__ == 'TimeoutExpired':result['category'] = 'TIMEOUT'
    return result


def record(work, event):
    path = Path(work) / 'operational-events.json'
    try:
        with locked(path.with_suffix('.lock')):
            data = read(path, dict(version=VERSION, events=[]))
            data['events'].append(dict(at=time.time(), **event))
            write(path, data)
    except (OSError, ValueError):
        # Observability cannot invalidate valid paid work or final evidence.
        return False
    return True


def transition(home, ident, component, old, new, policy):
    if policy != 'PRODUCTION_PREFLIGHT_V1' or old == new:return
    record(Path(home)/'jobs'/ident/'work', dict(kind='STATE', component=component, previous=old, status=new))


def intervention(store, job, item, operation, identity, actor='WEB_OR_AGENT'):
    if job.get('production_preflight_policy')!='PRODUCTION_PREFLIGHT_V1':return
    if item.get('status') not in FAILURE|{'WAITING_SUPPLEMENT_MEDIA'} and not item.get('title_confirmation_recovery'):return
    record(store.home/'jobs'/job['id']/'work',dict(kind='INTERVENTION',item=item['rank'],
        operation=operation,key=fingerprint([operation,identity]),actor=actor))


def install():
    """Wrap the existing engine; resource gates, checkpoints and paid guards stay."""
    from app.s3.headless_runtime import Harness, Profiler
    if getattr(Harness, '_cf_operations', False):return
    Harness._cf_operations = True
    original_execute, original_submit, original_record = Harness.execute, Harness.submit, Profiler.record
    original_span = Profiler.span

    @contextlib.contextmanager
    def span(self, *args, **kwargs):
        with original_span(self, *args, **kwargs) as event:
            try:yield event
            except Exception as error:
                event['diagnostic'] = diagnostic(error)
                raise

    def profiler_record(self, event):
        original_record(self, event)
        record(self.work, dict(kind='SPAN', **event))

    def execute(self, stage, item, inputs, callback, *, paid=False):
        started = time.time(); key = fingerprint([stage, inputs]); failure = {}
        queued = getattr(callback, '_cf_queued_at', started)
        def call():
            try:return callback()
            except Exception as error:
                failure.update(diagnostic(error))
                raise
        try:
            result = original_execute(self, stage, item, inputs, call, paid=paid)
        except Exception as error:
            failure.update(diagnostic(error))
            record(self.work, dict(kind='ATTEMPT', stage=stage, item=str(item), input_fingerprint=key,
                started_at=started, ended_at=time.time(), status='FAILED', diagnostic=failure))
            raise
        cache = bool(result.get('cache_hit') or result.get('prior_cache_hit'))
        failed = bool(result.get('error')) or result.get('status') in {'FAIL', 'FAILED', 'REVIEW_REQUIRED', 'BILLING_UNCERTAIN'}
        code = result.get('error')
        if not failure and isinstance(code, str):failure = diagnostic(ValueError(code))
        record(self.work, dict(kind='ATTEMPT', stage=stage, item=str(item), input_fingerprint=key,
            started_at=started, ended_at=time.time(), queue_wait_seconds=max(0., started-queued),
            status='FAILED' if failed else 'DONE', cache_hit=cache, diagnostic=failure or None,
            billing_uncertain=bool(paid and failed and not cache),
            paid_fingerprint=result.get('fingerprint') if stage=='TTS' else None,
            new_paid_requests=0 if cache else int(bool(stage=='TTS' and result.get('new_requests')))))
        return result

    def submit(self, stage, item, inputs, callback, *, paid=False):
        def queued_callback():return callback()
        queued_callback._cf_queued_at = time.time()
        return original_submit(self, stage, item, inputs, queued_callback, paid=paid)

    Harness.execute, Harness.submit, Profiler.record, Profiler.span = execute, submit, profiler_record, span


def state_seconds(events, now):
    by_component = {}; human = queue = 0.
    for event in events:
        if event.get('kind') != 'STATE':continue
        by_component.setdefault(event['component'], []).append(event)
    for component, rows in by_component.items():
        rows.sort(key=lambda e:e['at'])
        for index, row in enumerate(rows):
            seconds = max(0., (rows[index+1]['at'] if index+1<len(rows) else now)-row['at'])
            if component != 'job' and row['status'] in HUMAN:human += seconds
            if component == 'job' and row['status'] == 'QUEUED':queue += seconds
    return dict(human_wait_item_seconds=human, product_queue_seconds=queue)


def metrics(store):
    rows = []; paid = set(); total_attempts = cache_hits = 0; processing = []; queue = human = resource_wait = 0.
    eligible = completed = first_pass = waits = 0
    uncertain_paid=False
    interventions={};human_actions=set();unknown_actor=set()
    for job in store.jobs():
        if job.get('production_preflight_policy') != 'PRODUCTION_PREFLIGHT_V1':continue
        eligible += len(job['items'])
        data = read(store.home/'jobs'/job['id']/'work/operational-events.json', {})
        events = data.get('events', []); timing = state_seconds(events, time.time())
        for event in events:
            if event.get('kind')=='INTERVENTION':
                interventions[event['key']]=event
                (human_actions if event['actor']=='FEISHU_OWNER_OR_ADMIN' else unknown_actor).add(event['key'])
        queue += timing['product_queue_seconds'];human += timing['human_wait_item_seconds']
        attempts = [e for e in events if e.get('kind')=='ATTEMPT'];total_attempts += len(attempts)
        uncertain_paid=uncertain_paid or any(e.get('billing_uncertain') for e in attempts)
        cache_hits += sum(bool(e.get('cache_hit')) for e in attempts)
        resource_wait += sum(e.get('queue_wait_seconds', 0) for e in attempts)
        for e in attempts:
            if e.get('new_paid_requests') and e.get('paid_fingerprint'):paid.add(e['paid_fingerprint'])
        spans = [dict(e,started_at=min(e['ended_at'],e['started_at']+e.get('queue_wait_seconds',0)))
                 for e in events if e.get('kind')=='SPAN' and 'started_at' in e]
        processing.extend(spans)
        for item in job['items']:
            states = [e for e in events if e.get('kind')=='STATE' and e.get('component')==item['rank']]
            exceptional = [e for e in states if e['status'] in FAILURE or e['status']=='WAITING_SUPPLEMENT_MEDIA']
            waits += len(exceptional)
            begun = next((e['at'] for e in states if e['status'] in {'VOICE','EDITING','DURATION_FIT'}), None)
            if item['status']=='PASS' and begun is not None:
                completed += 1
                bad = any(e['at']>=begun and e['status'] in FAILURE|{'WAITING_SUPPLEMENT_MEDIA'} for e in states)
                bad = bad or any(e.get('failed') for e in spans if str(e.get('item')) in {item['rank'],'preflight-'+item['rank'],'final-'+item['rank']} and e.get('started_at',0)>=begun)
                if not bad:first_pass += 1
        rows.append(dict(id=job['id'], items=len(job['items']), observed=bool(events)))
    return dict(version=VERSION, historical_scope='新规则任务；旧任务不回填', jobs=rows,
        eligible_items=eligible, completed_observed_items=completed,
        first_pass_items=first_pass, first_pass_rate=first_pass/completed if completed else None,
        exception_interventions=len(interventions) if rows else None, exception_waits=waits if rows else None,
        verified_human_interventions=len(human_actions) if rows and not unknown_actor else None,
        intervention_actor_unknown=len(unknown_actor) if rows else None,
        machine_wall_seconds=union_seconds(processing) if processing else None,
        product_queue_seconds=queue if rows else None, human_wait_item_seconds=human if rows else None,
        resource_wait_worker_seconds=resource_wait if total_attempts else None,
        cache_hit_rate=cache_hits/total_attempts if total_attempts else None,
        independent_paid_tts_requests=len(paid) if rows and not uncertain_paid else None,
        confirmed_paid_tts_requests=len(paid) if rows else None, billing_uncertain=uncertain_paid,
        notes=['首次通过率只统计有完整制作起点且已完成的条目；正常两道人审不算异常介入',
               '机器时长为区间并集；人审为条目等待之和；资源等待为工作线程之和；缺证据为未知'])


def health(store, manager, bridge, concurrency):
    import tomllib
    monitor = read(store.home/'runtime/creative-factory-monitor-state.json', {})
    schedule = dict(status='UNKNOWN', configured_interval_minutes=None, target_interval_minutes=5)
    path = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))/'automations/automation/automation.toml'
    try:
        with path.open('rb') as stream:data=tomllib.load(stream)
        rule = data.get('rrule', ''); found = re.search(r'(?:^|;)INTERVAL=(\d+)', rule)
        interval = int(found.group(1)) if found and 'FREQ=MINUTELY' in rule else None
        schedule.update(status=data.get('status', 'UNKNOWN'), configured_interval_minutes=interval,
                        frequency_matches_target=interval==5)
    except (OSError, ValueError, TypeError):pass
    timestamps = [monitor.get(k) for k in ('at','checked_at','last_checked_at','updated_at','last_run_at') if monitor.get(k) is not None]
    observations=read(store.home/'runtime/monitor-observations.json',{}).get('events',[])
    scheduled=[e['at'] for e in observations if e.get('origin')=='heartbeat']
    intervals=[b-a for a,b in zip(scheduled,scheduled[1:])]
    state = bridge.status()
    return dict(version=VERSION, service=dict(status='RUNNING', pid=os.getpid()),
        feishu={k:state.get(k) for k in ('connected','accepting','state','error')},
        quark_ready=bool((state.get('quark_health') or {}).get('ready')),
        queue=concurrency, bridge_last_success_at=getattr(bridge,'last_monitor_success_at',None),
        monitoring=dict(schedule=schedule, last_check=observations[-1]['at'] if observations else timestamps[0] if timestamps else None,
                        observed_intervals=intervals[-20:] or None,
                        note='配置频率与实际触发间隔分别记录；未观测到的时间不推断'))
