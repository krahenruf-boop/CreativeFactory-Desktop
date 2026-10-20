"""Optional personal-Mac intake/approval bridge using existing CF services."""
import copy
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..batch_confirmation import batch_view
from ..ownership import item_state
from ..secrets import Keychain
from ..storage import product_name
from . import cards
from .cloud import QuarkCloud, CloudError, share_url, share_passcode
from .storage import Journal, fingerprint
from .transport import FeishuTransport
from . import recovery

ERRORS = {
    'FEISHU_TASK_FORBIDDEN': '只有任务负责人和管理员可以操作',
    'FEISHU_TASK_BIND_INVALID': '绑定码已失效或群已绑定，请在本机设置查看',
    'FEISHU_TASK_NOT_FOUND': '未找到该任务',
    'FEISHU_TASK_DISABLED': '个人 Mac 接单服务未连接',
    'FEISHU_TASK_NOT_ACCEPTING': '正式接单尚未开放，请等待验收完成',
    'FEISHU_TASK_MARKET_REQUIRED': '请选择巴西或墨西哥市场',
    'FEISHU_TASK_COUNT_INVALID': '素材数量请填写 1–30 的整数',
    'FEISHU_TASK_NOT_VERIFIED': '请先完成三条视频的飞书全流程验收',
    'FEISHU_TASK_AUTH_FAILED': '请先配置飞书接单机器人的有效凭据',
    'FEISHU_TASK_SDK_MISSING': '接单运行组件尚未准备好',
    'FEISHU_TASK_STOPPING': '请先关闭机器人连接，等待当前操作结束后再更换应用',
    'FEISHU_TASK_PATH_UNSAFE': '素材文件路径不安全，已停止操作',
    'FEISHU_TASK_LOCAL_MANIFEST_REQUIRED': '原任务素材清单尚未核实，请先核对分享素材',
    'FEISHU_TASK_LOCAL_SOURCE_MISMATCH': '本机素材的文件名、数量或大小与原飞书任务不一致，请检查',
    'FEISHU_TASK_TRANSFER_BUSY': '该任务素材正在接续，请等待当前操作完成',
    'FEISHU_TASK_SCRIPT_REVIEW_REQUIRED': '已有手动修改的口播，请在确认卡中补齐未准备好的文案，已改内容不会覆盖',
    'FEISHU_TASK_ITEM_REVIEW_REQUIRED': '剩余素材需要补充画面或检查具体错误，已完成成片已保留',
    'QUARK_LINK_INVALID': '请提供有效的夸克文件夹分享链接',
    'QUARK_FLAT_FOLDER_REQUIRED': '请把视频直接放在分享文件夹内，避免嵌套子文件夹',
    'QUARK_NOT_INSTALLED': '夸克官方工具尚未安装',
    'QUARK_-104': '夸克需要已授权的 Agent 运行环境，请从 Codex 启动本次接单服务',
    'QUARK_HEALTH_FAILED': '夸克连接检查未通过，接单已暂停，请检查本机运行环境和授权',
    'QUARK_SOURCE_CHANGED': '分享素材已变化，请检查并重新提交',
    'QUARK_VIDEO_UNREADABLE': '有视频无法读取，请检查素材',
    'STALE_ACTION': '内容已变化，请使用最新确认卡片',
    'BATCH_TITLE_REQUIRED': '请为每条视频选择一个标题；可以先保存当前页，再到其它页补齐',
    'TITLE_SHORTEN_REQUIRED': '标题过长，缩到48px仍无法保持两行；请在确认卡中缩短标题后重新确认',
    'BATCH_NOT_READY': '内容仍在准备，请稍后查看最新确认卡片',
    'SCRIPT_DRAFT_INVALID': '口播转换返回的内容不完整，文案已保留，请重试保存',
    'ZH_TRANSLATION_REQUIRED': '中文对照暂未生成，文案已保留，请重试保存',
}


def human(error):
    code = str(error)
    if code.startswith('QUARK_SOURCE_COUNT_'):
        return '实际有效视频数量为 ' + code.rsplit('_', 1)[-1] + '，与填写的数量不符，请补交素材'
    if code.startswith('QUARK_'):
        return ERRORS.get(code, '夸克操作未完成（' + code + '），请检查授权、分享有效期和网盘额度')
    from ..content_confirmation import ERRORS as CONTENT_ERRORS
    return ERRORS.get(code, CONTENT_ERRORS.get(code, '操作未完成（' + (code if re.fullmatch(r'[A-Z0-9_]+', code) else 'INTERNAL_ERROR') + '），原有内容已保留'))


class TaskBridge:
    def __init__(self, store, submission, review, content, batch, deletion, resources,
                 journal=None, transport=None, cloud=None, media_root=None):
        self.store, self.submission, self.review, self.content = store, submission, review, content
        self.batch, self.deletion = batch, deletion
        self.journal = journal or Journal(store.home)
        self.secrets = Keychain(Path(resources) / 'bin/feishu-tasks-keychain')
        self.media_root = Path(media_root or Path.home() / 'Movies/Creative Factory/Media/Feishu')
        self.stop_event = threading.Event()
        self.transport = transport or FeishuTransport(self.credentials, self.receive, Path(resources) / 'runtime/feishu-sdk')
        self.cloud = cloud or QuarkCloud(Path.home() / '.agents/skills/quarkclouddrive/scripts/quark-drive.cjs', self.journal.root / 'cloud', self.stop_event, notice=self.record_cloud_notice)
        self.pool = None
        self.threads = []
        self.transfers = set()
        self.recovered_jobs = set()
        self.lock = threading.RLock()
        self.bot_open_id = ''
        self.last_callback_at = 0
        self.last_monitor_success_at = None
        self.last_error = ''
        self.cloud_health = dict(ready=False, error='', notice='', checked_at=None)

    def record_cloud_notice(self, message, directory):
        directory = Path(directory).resolve()
        if directory.parent != (self.journal.root / 'cloud').resolve():
            raise ValueError('FEISHU_TASK_PATH_UNSAFE')
        with self.journal.lock:
            task = self.journal.task(directory.name)
            notices = list(task.get('cloud_notices') or [])
            if message not in notices:
                self.journal.update(task['id'], cloud_notices=[*notices, message])

    def credentials(self):
        app_id, secret = self.secrets.get('APP_ID'), self.secrets.get('APP_SECRET')
        if not re.fullmatch(r'cli_[A-Za-z0-9]+', app_id) or not secret:
            raise ValueError('FEISHU_TASK_AUTH_FAILED')
        return app_id, secret

    def status(self):
        c = self.journal.config()
        try:
            configured = Path(self.secrets.helper).is_file() and self.secrets.configured('APP_ID') and self.secrets.configured('APP_SECRET')
        except ValueError:
            configured = False
        error = self.cloud_health['error'] or self.last_error or self.transport.error
        return dict(enabled=c['enabled'], accepting=c['accepting'], configured=bool(configured),
                    connected=self.transport.connected(), group_bound=bool(c['chat_id']),
                    app_id=c.get('app_id', ''), verified=bool(c.get('verified_at')), quark_installed=self.cloud.cli.is_file(),
                    state='BLOCKED' if self.cloud_health['error'] else self.transport.state, error=error,
                    error_message=human(error) if error else '', quark_health=dict(self.cloud_health),
                    last_callback_at=self.last_callback_at, tasks=[{k: t.get(k) for k in ('id', 'state', 'product_name', 'job_id', 'error')} for t in self.journal.tasks()[-20:]])

    def configure(self, data):
        if set(data) - {'app_id', 'app_secret', 'enabled', 'accepting'}:
            raise ValueError('INVALID_REQUEST')
        c = self.journal.config()
        if data.get('accepting') and not c.get('verified_at'):
            raise ValueError('FEISHU_TASK_NOT_VERIFIED')
        if data.get('enabled'):
            self.credentials()
            if isinstance(self.transport, FeishuTransport) and not (self.transport.sdk_path / 'lark_oapi/__init__.py').is_file():
                raise ValueError('FEISHU_TASK_SDK_MISSING')
        if data.get('app_id') or data.get('app_secret'):
            if c['enabled']:
                raise ValueError('FEISHU_TASK_STOPPING')
            app_id = str(data.get('app_id', '')).strip()
            secret = str(data.get('app_secret', '')).strip()
            if not re.fullmatch(r'cli_[A-Za-z0-9]{10,40}', app_id) or not secret or len(secret) > 200:
                raise ValueError('INVALID_REQUEST')
            self.secrets.set('APP_ID', app_id)
            self.secrets.set('APP_SECRET', secret)
            self.journal.configure(app_id=app_id)
        for name in ('enabled', 'accepting'):
            if name in data:
                if type(data[name]) is not bool:
                    raise ValueError('INVALID_REQUEST')
                self.journal.configure(**{name: data[name]})
        if self.journal.config()['enabled']:
            self.start()
        else:
            self.stop()
        return self.status()

    def start(self):
        with self.lock:
            if not self.journal.config()['enabled'] or any(t.is_alive() for t in self.threads):
                return
            self.stop_event.clear()
            # A working Feishu socket does not prove cloud intake is usable.
            # Fail before receiving tasks; never spoof a Codex session for a
            # LaunchAgent or repeatedly retry an authorization failure.
            self.cloud_health = self.cloud.health()
            if not self.cloud_health['ready']:
                return
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='feishu-transfer')
            self.transport.start()
            self.threads = [threading.Thread(target=self.events_loop, daemon=True, name='feishu-actions'),
                            threading.Thread(target=self.monitor_loop, daemon=True, name='feishu-status')]
            for thread in self.threads:
                thread.start()

    def stop(self):
        self.stop_event.set()
        self.transport.stop()
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def authorize(self, task, operator, chat):
        c = self.journal.config()
        if chat != c['chat_id'] or task['chat'] != chat or operator not in {task['owner'], c['admin_open_id']}:
            raise ValueError('FEISHU_TASK_FORBIDDEN')

    def receive(self, raw, kind):
        """Only durable acceptance here; no download, provider or network call."""
        try:
            c = self.journal.config()
            if not c['enabled']:
                raise ValueError('FEISHU_TASK_DISABLED')
            event_id = (raw.get('header') or {}).get('event_id')
            if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
                raise ValueError('INVALID_REQUEST')
            e = raw.get('event') or {}
            if kind == 'message':
                m, sender = e.get('message') or {}, e.get('sender') or {}
                if m.get('chat_type') != 'group' or m.get('message_type') != 'text' or sender.get('sender_type') != 'user':
                    return dict(accepted=False, message='')
                if not self.bot_open_id or not any((v.get('id') or {}).get('open_id') == self.bot_open_id for v in m.get('mentions') or []):
                    return dict(accepted=False, message='')
                operator, chat = (sender.get('sender_id') or {}).get('open_id'), m.get('chat_id')
                value = json.loads(m.get('content') or '{}').get('text', '')
                value = re.sub(r'@_user_\d+', '', value).strip()
                if chat != c['chat_id'] and not (not c['chat_id'] and value.startswith('绑定 ')):
                    raise ValueError('FEISHU_TASK_FORBIDDEN')
                body = dict(kind=kind, operator=operator, chat=chat, text=value)
            else:
                operator, chat = (e.get('operator') or {}).get('open_id'), (e.get('context') or {}).get('open_chat_id')
                action = e.get('action') or {}
                value = action.get('value') or {}
                if not isinstance(value, dict) or chat != c['chat_id']:
                    raise ValueError('FEISHU_TASK_FORBIDDEN')
                if value.get('task_id'):
                    self.authorize(self.journal.task(value['task_id']), operator, chat)
                elif value.get('form_id'):
                    self.journal.get_form(value['form_id'], operator, chat)
                else:
                    raise ValueError('INVALID_REQUEST')
                body = dict(kind=kind, operator=operator, chat=chat, value=value, fields=action.get('form_value') or {},
                            message_id=(e.get('context') or {}).get('open_message_id', ''))
            if not re.fullmatch(r'ou_[A-Za-z0-9]+', operator or '') or not re.fullmatch(r'oc_[A-Za-z0-9]+', chat or ''):
                raise ValueError('FEISHU_TASK_FORBIDDEN')
            self.journal.record_event(event_id, body)
            self.last_callback_at = time.time()
            return dict(accepted=True, message='已接收，正在处理')
        except Exception as error:
            return dict(accepted=False, message=human(error))

    def events_loop(self):
        while not self.stop_event.wait(.2):
            event = self.journal.next_event()
            if not event:
                continue
            event_id, body = event
            try:
                self.dispatch(event_id, body)
                self.journal.event_done(event_id)
            except Exception as error:
                code = str(error) if re.fullmatch(r'[A-Z0-9_]+', str(error)) else 'INTERNAL_ERROR'
                self.journal.event_done(event_id, code)
                try:
                    self.action_error(event_id, body, error)
                except Exception:
                    # A revoked/expired form must not terminate the listener.
                    self.last_error = 'FEISHU_TASK_FEEDBACK_FAILED'

    def action_error(self, event_id, body, error):
        """Keep errors and submitted text in the working card, without chat spam."""
        value = body.get('value') or {}
        task_id = value.get('task_id')
        if task_id:
            task = self.journal.task(task_id)
            self.authorize(task, body['operator'], body['chat'])
            editor = dict(task.get('review_editor') or {})
            if value.get('action') == 'apply_edit' and editor.get('form_id') == value.get('form_id'):
                editor.update(fields=body.get('fields') or {}, error=human(error), busy=False)
                self.journal.update(task_id, review_editor=editor)
            else:
                self.journal.update(task_id, review_notice=human(error))
            self.push_review(self.journal.task(task_id), force=True)
        elif value.get('form_id'):
            fid = value['form_id']
            form = self.journal.get_form(fid, body['operator'], body['chat'])
            payload = cards.new_task(fid, [], dict(form['defaults'], **body.get('fields', {})), form['kind'] == 'media', human(error))
            self.journal.notify('form-error-' + event_id, body['chat'], payload,
                                message_id=body.get('message_id') or form.get('message_id', ''))
        else:
            self.journal.notify('error-' + event_id, body['chat'], cards.card('操作未完成', [cards.text(human(error))]))

    def validate_form(self, fields):
        product = product_name(fields.get('product_name', ''))
        preset = self.store.product(str(fields.get('preset_id', 'general')))
        if not preset:
            raise ValueError('INVALID_REQUEST')
        market = fields.get('market', 'Brazil')
        if market == 'preset':
            market = preset.get('market') if preset['id'] != 'general' else None
        if market not in {'Brazil', 'Mexico'}:
            raise ValueError('FEISHU_TASK_MARKET_REQUIRED')
        mode = fields.get('mode', 'Transport')
        if mode not in {'Transport', 'Remix'}:
            raise ValueError('INVALID_REQUEST')
        count = str(fields.get('expected_count', '10'))
        if not re.fullmatch(r'[1-9][0-9]?', count) or int(count) > 30:
            raise ValueError('FEISHU_TASK_COUNT_INVALID')
        url = share_url(fields.get('share_url', ''))
        requirements, passcode = fields.get('requirements', ''), fields.get('passcode', '')
        if not isinstance(requirements, str) or len(requirements) > 4000 or not isinstance(passcode, str) or len(passcode) > 32:
            raise ValueError('INVALID_REQUEST')
        return dict(product_name=product, preset_id=preset['id'], market=market, mode=mode, expected_count=int(count),
                    share_url=url, passcode=passcode.strip() or share_passcode(fields.get('share_url', '')), requirements=requirements)

    def dispatch(self, event_id, body):
        operator, chat = body['operator'], body['chat']
        if body['kind'] == 'message':
            command = body['text']
            if command.startswith('绑定 '):
                self.journal.bind(chat, operator, command[3:].strip())
                self.journal.notify('bound-' + event_id, chat, cards.card('群绑定完成', [cards.text('个人 Mac 已连接。正式接单暂未开放，管理员可 @机器人 新建任务，完成三条素材验收。')], operator))
            elif command in {'新建任务', '下任务', '新建测试任务'}:
                c = self.journal.config()
                if not c['accepting'] and operator != c['admin_open_id']:
                    raise ValueError('FEISHU_TASK_NOT_ACCEPTING')
                testing = command == '新建测试任务' or not c.get('verified_at')
                defaults = {'test': testing, 'expected_count': 3 if testing else 10}
                form_id = self.journal.form(operator, chat, defaults=defaults)
                self.journal.notify('form-' + event_id, chat, cards.new_task(form_id, self.store.products(), defaults))
            elif command.startswith('查询 '):
                task = self.journal.task(command[3:].strip())
                self.authorize(task, operator, chat)
                self.push_status(task, force=True)
            else:
                self.journal.notify('help-' + event_id, chat, cards.card('Creative Factory', [cards.text('@机器人 新建任务：填写接单表单\n@机器人 查询 CF-任务编号：查看进度\n确认口播与标题时，请使用对应任务卡片。')]))
            return
        value, fields = body['value'], body['fields']
        action = value.get('action')
        if action == 'submit':
            form = self.journal.get_form(value['form_id'], operator, chat)
            message_id = body.get('message_id') or form.get('message_id', '')
            if message_id:
                self.journal.update_form(value['form_id'], message_id=message_id)
            existing = self.journal.submitted_task(value['form_id']) if form['kind'] == 'new' else None
            if existing:
                self.authorize(existing, operator, chat)
                if message_id and not existing.get('status_message_id'):
                    existing = self.journal.update(existing['id'], status_message_id=message_id)
                self.push_status(existing, force=True)
                return
            if form['kind'] == 'media':
                task = self.journal.task(form['task_id'])
                self.authorize(task, operator, chat)
                if task.get('job_id') or form['defaults'].get('revision') != task['revision']:
                    raise ValueError('STALE_ACTION')
                data = self.validate_form(dict(task, **fields))
                revision = task['revision'] + 1
                task = self.journal.update(task['id'], **data, media={}, state='RECEIVED', error='', revision=revision, intake_key=task['id'] + '-R' + str(revision))
            else:
                c = self.journal.config()
                if not c['accepting'] and operator != c['admin_open_id']:
                    raise ValueError('FEISHU_TASK_NOT_ACCEPTING')
                data = self.validate_form(fields)
                testing = bool(form['defaults'].get('test'))
                if testing and data['expected_count'] != 3:
                    raise ValueError('FEISHU_TASK_COUNT_INVALID')
                task, _ = self.journal.create_task(value['form_id'], dict(data, owner=operator, chat=chat, test=testing))
            if message_id and not task.get('status_message_id'):
                task = self.journal.update(task['id'], status_message_id=message_id)
            self.push_status(task, force=True)
            self.schedule(task['id'], 'intake')
            return
        task = self.journal.task(value.get('task_id', ''))
        self.authorize(task, operator, chat)
        if action == 'apply_edit':
            from .edit_fields import merge_fields
            fields = merge_fields(fields)
            body['fields'] = fields  # Error/retry cards retain the full manuscript.
        if action == 'cancel_edit':
            self.journal.update(task['id'], review_editor=None, review_notice='')
            self.push_review(self.journal.task(task['id']), force=True)
            return
        if action == 'accept_delivery':
            if operator != self.journal.config()['admin_open_id']:
                raise ValueError('FEISHU_TASK_FORBIDDEN')
            if task.get('delivery_accepted'):
                return
            if (value.get('revision') != str(task.get('revision', 0)) or task['state'] != 'DONE'
                    or not task.get('test') or task['expected_count'] != 3
                    or len(task.get('delivered_ranks', [])) != 3
                    or not task.get('script_confirmed') or not task.get('title_confirmed')
                    or not task.get('delivery', {}).get('share_url')):
                raise ValueError('FEISHU_TASK_NOT_VERIFIED')
            self.journal.configure(verified_at=time.time(), verified_task_id=task['id'])
            task = self.journal.update(task['id'], delivery_accepted=True, revision=task['revision'] + 1)
            self.push_status(task, force=True)
            return
        if action in {'replace_media', 'retry_intake', 'retry_upload', 'retry_job'}:
            if value.get('revision') != str(task.get('revision', 0)):
                raise ValueError('STALE_ACTION')
            if action == 'replace_media':
                if task.get('job_id') or task['state'] not in {'MEDIA_ERROR', 'SUBMIT_ERROR'}:
                    raise ValueError('STALE_ACTION')
                form_id = self.journal.form(operator, chat, kind='media', task_id=task['id'], defaults=dict(task, revision=task['revision']))
                self.journal.notify('media-form-' + event_id, chat, cards.new_task(form_id, [], task, True))
            elif action == 'retry_intake':
                if task.get('job_id') or task['state'] not in {'MEDIA_ERROR', 'SUBMIT_ERROR'}:
                    raise ValueError('STALE_ACTION')
                self.journal.update(task['id'], state='RECEIVED', error='', revision=task['revision'] + 1)
                self.schedule(task['id'], 'intake')
            elif action == 'retry_upload':
                if task['state'] != 'UPLOAD_ERROR':
                    raise ValueError('STALE_ACTION')
                self.journal.update(task['id'], state='READY_UPLOAD', error='', revision=task['revision'] + 1)
                self.schedule(task['id'], 'upload')
            else:
                if task['state'] != 'NEEDS_ATTENTION':
                    raise ValueError('STALE_ACTION')
                with self.deletion.edit(task['job_id']):
                    if not recovery.resume(self.submission.manager, task['job_id'], task['id']):
                        raise ValueError('FEISHU_TASK_ITEM_REVIEW_REQUIRED')
                self.journal.update(task['id'], state='PROCESSING', error='', revision=task['revision'] + 1)
            return
        if not task['job_id']:
            raise ValueError('STALE_ACTION')
        with self.deletion.edit(task['job_id']):
            view = self.batch.view(task['job_id'])
            if action == 'apply_edit':
                form = self.journal.get_form(value['form_id'], operator, chat)
                if form.get('consumed'):
                    if form.get('applied_digest') != fingerprint(fields):
                        raise ValueError('STALE_ACTION')
                    self.push_review(task, force=True)
                    return
            intent = self.journal.operation(event_id)
            receipt_key = 'feishu-action-' + fingerprint(event_id)[:32]
            receipts = (self.store.get(task['job_id']).get('batch_review') or {}).get('receipts', {})
            if action in {'approve_scripts', 'approve_titles'} and intent and receipt_key in receipts:
                if action == 'approve_scripts':
                    self.batch.scripts(task['job_id'], intent)
                    self.journal.update(task['id'], script_confirmed=True)
                else:
                    self.batch.titles(task['job_id'], intent)
                    self.journal.update(task['id'], title_confirmed=True)
                self.push_review(self.journal.task(task['id']), force=True)
                return
            if action not in {'page', 'apply_edit', 'edit_script', 'edit_title'} and value.get('revision') != view['action_token']:
                raise ValueError('STALE_ACTION')
            if view['phase'] == 'TITLES' and action in {'page', 'save_titles', 'approve_titles'} and fields:
                task = self.save_choices(task, view, fields)
            rank = value.get('rank')
            before_job=self.store.get(task['job_id'])
            before_item=next((x for x in before_job['items'] if x['rank']==rank),None)
            row = next((r for r in view['items'] if r['rank'] == rank and r['in_scope']), None)
            if action == 'page':
                page = value.get('page')
                if type(page) is not int or not 0 <= page < len(cards.review_pages(view)):
                    raise ValueError('INVALID_REQUEST')
                self.journal.update(task['id'], page=page)
            elif action in {'edit_script', 'edit_title'}:
                if not row or view['phase'] != ('SCRIPTS' if action == 'edit_script' else 'TITLES'):
                    raise ValueError('STALE_ACTION')
                self.open_editor(task, view, row, operator, 'script' if action == 'edit_script' else 'title')
            elif action == 'apply_edit':
                form = self.journal.get_form(value['form_id'], operator, chat)
                if form['kind'] != 'edit' or form['task_id'] != task['id'] or form['defaults']['rank'] != rank:
                    raise ValueError('STALE_ACTION')
                if not row or not isinstance(fields.get('text'), str):
                    raise ValueError('INVALID_REQUEST')
                expected = form['defaults'].get('item_signature')
                stale = expected != self.edit_signature(row) if expected else form['defaults']['revision'] != view['action_token']
                if stale:
                    raise ValueError('STALE_ACTION')
                editor = dict(form_id=value['form_id'], rank=rank, kind=value['kind'], revision=view['action_token'], fields=fields, busy=True, error='')
                self.journal.update(task['id'], review_editor=editor, review_notice='')
                self.push_review(self.journal.task(task['id']), force=True)
                if value['kind'] == 'script' and form['defaults']['kind'] == 'edit_script' and view['phase'] == 'SCRIPTS':
                    self.review.draft(task['job_id'], rank, dict(mode='PASTED', text=fields['text'], language=fields.get('language') or 'auto', action_token=row['action_token']))
                elif value['kind'] == 'title' and form['defaults']['kind'] == 'edit_title' and view['phase'] == 'TITLES':
                    self.content.titles(task['job_id'], rank, row['action_token'], fields['text'])
                else:
                    raise ValueError('STALE_ACTION')
                self.invalidate_choice(task, rank)
                self.journal.update_form(value['form_id'], consumed=True, applied_digest=fingerprint(fields))
                self.journal.update(task['id'], review_editor=None, review_notice='Rank ' + rank + ' 已保存')
                if value.get('next_rank'):
                    updated = self.batch.view(task['job_id'])
                    following = next((r for r in updated['items'] if r['in_scope'] and int(r['rank']) > int(rank)), None)
                    if following:
                        self.open_editor(self.journal.task(task['id']), updated, following, operator, value['kind'])
            elif action == 'regenerate_title':
                if not row or view['phase'] != 'TITLES':
                    raise ValueError('STALE_ACTION')
                self.content.titles(task['job_id'], rank, row['action_token'])
                self.invalidate_choice(task, rank)
            elif action == 'reopen_script':
                self.batch.reopen_script(task['job_id'], rank, view['action_token'])
                self.invalidate_choice(task, rank)
                self.journal.update(task['id'], script_confirmed=False, title_confirmed=False)
            elif action == 'select_title':
                if not row or view['phase'] != 'TITLES':
                    raise ValueError('STALE_ACTION')
                task = self.save_choices(task, view, {'title_' + rank: value.get('title_id')})
            elif action == 'save_titles':
                if view['phase'] != 'TITLES':
                    raise ValueError('STALE_ACTION')
            elif action == 'approve_scripts':
                payload = dict(action_token=view['action_token'], request_id=receipt_key)
                self.journal.operation(event_id, payload)
                self.batch.scripts(task['job_id'], payload)
                self.journal.update(task['id'], script_confirmed=True, page=0)
            elif action == 'approve_titles':
                if task.get('selection_revision') != view['action_token']:
                    raise ValueError('STALE_ACTION')
                payload = dict(action_token=view['action_token'], request_id=receipt_key, selections=task.get('selections') or {})
                self.journal.operation(event_id, payload)
                self.batch.titles(task['job_id'], payload)
                self.journal.update(task['id'], title_confirmed=True)
            else:
                raise ValueError('INVALID_REQUEST')
            if before_item and action not in {'page','edit_script','edit_title','select_title','save_titles'}:
                from ..runtime_insights import intervention
                intervention(self.store,before_job,before_item,action,event_id,'FEISHU_OWNER_OR_ADMIN')
        self.push_review(self.journal.task(task['id']), force=True)

    @staticmethod
    def edit_signature(row):
        # Other ranks and transient SCRIPTING/status changes cannot invalidate
        # this editor. A real change to this rank's copy still blocks overwrite.
        return fingerprint([row.get('script'), row.get('title_candidates'), row.get('custom_title')])

    def open_editor(self, task, view, row, operator, kind):
        fid = self.journal.form(operator, task['chat'], kind='edit', task_id=task['id'],
                                defaults=dict(revision=view['action_token'], item_signature=self.edit_signature(row), rank=row['rank'], kind='edit_' + kind))
        source = row.get('script') if kind == 'script' else row.get('custom_title')
        fields = dict(text=(source or {}).get('text', ''), language='auto')
        self.journal.update(task['id'], review_editor=dict(form_id=fid, rank=row['rank'], kind=kind, revision=view['action_token'], fields=fields, error='', busy=False), review_notice='')

    def save_choices(self, task, view, fields):
        choices = dict(task.get('selections') or {}) if task.get('selection_revision') == view['action_token'] else {}
        for key, choice in fields.items():
            if not key.startswith('title_') or choice is None:
                continue
            target = next((r for r in view['items'] if r['rank'] == key[6:] and r['in_scope']), None)
            candidates = (target or {}).get('title_candidates', []) + ([(target or {})['custom_title']] if (target or {}).get('custom_title') else [])
            if not target or str(choice) not in {str(t['id']) for t in candidates}:
                raise ValueError('BATCH_TITLE_REQUIRED')
            choices[target['rank']] = str(choice)
        hashes = {r['rank']: self.choice_hash(r) for r in view['items'] if r['rank'] in choices}
        return self.journal.update(task['id'], selections=choices, selection_hashes=hashes, selection_revision=view['action_token'])

    @staticmethod
    def choice_hash(row):
        return fingerprint([row['script'], row['title_candidates'], row.get('custom_title')])

    def invalidate_choice(self, task, rank):
        latest = self.journal.task(task['id'])
        choices = dict(latest.get('selections') or {})
        hashes = dict(latest.get('selection_hashes') or {})
        choices.pop(rank, None)
        hashes.pop(rank, None)
        self.journal.update(task['id'], selections=choices, selection_hashes=hashes, selection_revision='')

    def resume_local_media(self, task_id, path, revision, source_hashes, authorize):
        """Authenticated local recovery; never impersonates a Feishu approver."""
        from .local_media import source_snapshot
        with self.lock, self.journal.lock:
            task = self.journal.task(task_id)
            config = self.journal.config()
            if not config.get('enabled') or not self.pool or self.stop_event.is_set():
                raise ValueError('FEISHU_TASK_DISABLED')
            if task.get('chat') != config.get('chat_id') or not task.get('owner'):
                raise ValueError('FEISHU_TASK_FORBIDDEN')
            folder = Path(path).expanduser()
            if folder.is_symlink():
                raise ValueError('FEISHU_TASK_PATH_UNSAFE')
            try:
                folder = folder.resolve(strict=True)
            except OSError:
                raise ValueError('SOURCE_FOLDER_MISSING') from None
            authorize(folder)
            previous = task.get('local_media') or {}
            if previous.get('path') == str(folder) and previous.get('base_revision') == revision:
                if source_hashes != {f['name']: f['sha256'] for f in previous['files']}:
                    raise ValueError('STALE_ACTION')
                return dict(task_id=task_id, state=task['state'], job_id=task.get('job_id', ''), replayed=True)
            if task_id in self.transfers:
                raise ValueError('FEISHU_TASK_TRANSFER_BUSY')
            if task.get('job_id') or task['state'] not in {'MEDIA_ERROR', 'SUBMIT_ERROR'} or task['revision'] != revision:
                raise ValueError('STALE_ACTION')
            existing = self.store.by_request(task['request_id'])
            if existing and (existing.get('deleted_at') or existing.get('control_request')
                             or existing.get('state') in {'COMPLETED', 'PAUSED', 'PAUSING', 'STOPPED', 'STOPPING'}
                             or (existing.get('submission_origin') or {}).get('task_id') != task_id):
                raise ValueError('STALE_ACTION')
            snapshot = source_snapshot(task, str(folder), source_hashes, authorize)
            snapshot.update(base_revision=revision, requested_at=time.time())
            self.journal.update(task_id, local_media=snapshot, state='RECEIVED', error='', revision=revision + 1)
            self.schedule(task_id, 'intake')
            return dict(task_id=task_id, state='RECEIVED', job_id='', replayed=False)

    def schedule(self, task_id, kind):
        with self.lock:
            if task_id in self.transfers or not self.pool or self.stop_event.is_set():
                return
            self.transfers.add(task_id)

        def transfer():
            try:
                self.intake(task_id) if kind == 'intake' else self.upload(task_id)
            finally:
                with self.lock:
                    self.transfers.discard(task_id)
        self.pool.submit(transfer)

    def intake(self, task_id):
        task = self.journal.task(task_id)
        if task.get('job_id'):
            return
        try:
            existing = self.store.by_request(task['request_id'])
            if existing:
                if (existing.get('submission_origin') or {}).get('task_id') != task_id:
                    raise ValueError('FEISHU_TASK_FORBIDDEN')
                self.journal.update(task_id, state='PROCESSING', job_id=existing['id'], local_folder=existing['original_input_path'], error='')
                return
            self.journal.update(task_id, state='DOWNLOADING', error='')
            checkpoint = lambda value: self.journal.update(task_id, media=copy.deepcopy(value))
            if task.get('local_media'):
                from .local_media import intake
                folder, media = intake(task, self.media_root, checkpoint, self.stop_event)
            else:
                folder, media = self.cloud.intake(task, self.media_root, checkpoint)
            self.journal.update(task_id, state='SUBMITTING')

            def authorize(path):
                if path != folder.resolve() or path.is_symlink() or not path.is_relative_to(self.media_root.resolve()):
                    raise ValueError('FEISHU_TASK_PATH_UNSAFE')
                for entry in media['manifest']:
                    from ..file_storage import sha256
                    source = Path(entry['path'])
                    if source.parent != folder or source.is_symlink() or source.stat().st_size != entry['size'] or sha256(source) != entry['sha256']:
                        raise ValueError('SOURCE_HASH_CHANGED')
            preset = self.store.product(task['preset_id'])
            spec = dict(request_id=task['request_id'], input_path=str(folder), product_name=task['product_name'], product_profile_id=task['preset_id'], market=task['market'], mode=task['mode'], output_count=task['expected_count'], title_approval_mode='manual', watermark_mode=preset.get('watermark_mode') or self.store.settings().get('watermark', 'auto'), script_mode='AUTO', script_reference_mode='smart', custom_instruction_raw=task['requirements'], custom_tag_ids=[], source_names=[r['name'] for r in media['manifest']], submission_origin=dict(type='feishu', task_id=task_id))
            matches = self.submission.feishu.matches(task['product_name'], task['market'])
            exact = [m for m in matches if m['exact']]
            binding = self.submission.feishu.binding()
            if len(exact) == 1 and binding.get('source_id'):
                spec.update(feishu_source_id=binding['source_id'], feishu_sheet_id=exact[0]['sheet_id'])
            job = self.submission.submit(spec, authorize)
            self.journal.update(task_id, state='PROCESSING', job_id=job['id'], local_folder=str(folder), error='')
        except Exception as error:
            if self.stop_event.is_set():
                return
            state = 'SUBMIT_ERROR' if self.journal.task(task_id)['state'] == 'SUBMITTING' else 'MEDIA_ERROR'
            self.journal.update(task_id, state=state, error=human(error), cloud_notice=getattr(error, 'notice', ''), revision=task.get('revision', 0) + 1)
        self.push_status(self.journal.task(task_id), force=True)

    def upload(self, task_id):
        task = self.journal.task(task_id)
        try:
            self.journal.update(task_id, state='UPLOADING', error='')
            job = self.store.get(task['job_id'])
            view = self.batch.view(job['id'])
            completed = {r['rank'] for r in view['items'] if r['pipeline_status'] == 'COMPLETED'}
            outputs = {x['rank']: x['output'] for x in job['items'] if x['rank'] in completed and x.get('output')}
            if not outputs:
                raise ValueError('OUTPUT_NOT_FOUND')
            delivery = self.cloud.deliver(task, outputs, lambda value: self.journal.update(task_id, delivery=copy.deepcopy(value)))
            total = int(job.get('output_count') or job.get('source_count') or len(job['items']))
            done = len(completed) == total and len(outputs) == total
            self.journal.update(task_id, state='DONE' if done else 'NEEDS_ATTENTION', delivery=delivery, delivered_ranks=sorted(outputs), error='' if done else '部分成片已交付，其余视频需要处理', revision=task.get('revision', 0) + 1)
        except Exception as error:
            if self.stop_event.is_set():
                return
            self.journal.update(task_id, state='UPLOAD_ERROR', error=human(error), cloud_notice=getattr(error, 'notice', ''), revision=task.get('revision', 0) + 1)
        self.push_status(self.journal.task(task_id), force=True)

    def notify_slot(self, task, slot, payload, revision, mention=''):
        signature = fingerprint(revision)
        with self.journal.lock:
            task = self.journal.task(task['id'])
            latest = dict(task.get('latest_notifications') or {})
            versions = dict(task.get('notification_versions') or {})
            previous = versions.get(slot) or {}
            if previous.get('signature') == signature:
                return
            # A -> B -> A must update the same card again; an old delivered
            # outbox key cannot represent this new transition.
            sequence = int(previous.get('sequence', 0)) + 1
            key = slot + '-' + task['id'] + '-' + fingerprint([signature, sequence])[:24]
            versions[slot] = dict(signature=signature, sequence=sequence)
            latest[slot] = key
            self.journal.update(task['id'], latest_notifications=latest, notification_versions=versions)
            message_id = task.get(slot + '_message_id', '')
            self.journal.notify(key, task['chat'], payload, task['id'], message_id=message_id, mention=mention)

    def push_review(self, task, force=False):
        if not task.get('job_id'):
            return
        view = self.batch.view(task['job_id'])
        editor = task.get('review_editor')
        if editor and view['phase'] in {'SCRIPTS', 'TITLES'}:
            payload = cards.edit(editor['form_id'], task, editor['rank'], editor['kind'], editor['revision'],
                                 editor.get('fields'), editor.get('error', ''), editor.get('busy', False))
            from .edit_fields import VERSION as editor_version
            self.notify_slot(task, 'review', payload, [editor_version, editor])
            return
        if view['phase'] not in {'SCRIPTS', 'TITLES'}:
            if task.get('review_message_id'):
                self.notify_slot(task, 'review', cards.card('内容确认已完成', [cards.text('已进入剪辑阶段，请查看该任务状态卡。')]), ['CLOSED', view['action_token']])
            return
        job = self.store.get(task['job_id'])
        ready = view['all_scripts_ready'] if view['phase'] == 'SCRIPTS' else view['all_titles_ready']
        if not ready and (job['state'] in {'RUNNING', 'QUEUED'} or any(x.get('operation_inflight') for x in job['items'])):
            return
        if task.get('selection_revision') != view['action_token']:
            hashes = {r['rank']: self.choice_hash(r) for r in view['items'] if r['in_scope']}
            choices = {r: value for r, value in task.get('selections', {}).items() if task.get('selection_hashes', {}).get(r) == hashes.get(r)}
            task = self.journal.update(task['id'], selections=choices, selection_hashes={r: hashes[r] for r in choices}, selection_revision=view['action_token'])
        key = ['INLINE_EDITOR_V4', view['phase'], view['action_token'], task.get('page', 0), task.get('selections', {}), task.get('review_notice', '')]
        mention = task['owner'] if task.get('notified_phase') != view['phase'] else ''
        self.notify_slot(task, 'review', cards.review(task, view), key, mention)
        if mention:
            self.journal.update(task['id'], notified_phase=view['phase'])

    def push_status(self, task, force=False):
        state = task['state']
        labels = dict(RECEIVED='已受理', DOWNLOADING='下载素材', SUBMITTING='检查素材', PROCESSING='处理中',
                      MEDIA_ERROR='素材需要处理', SUBMIT_ERROR='任务创建未完成', READY_UPLOAD='等待上传',
                      UPLOADING='上传成片', UPLOAD_ERROR='剪辑完成，上传失败', DONE='已完成', NEEDS_ATTENTION='需要处理')
        label, completed, total, detail = labels.get(state, state), len(task.get('delivered_ranks', [])), task['expected_count'], ''
        if state == 'DOWNLOADING' and task.get('local_media'):
            label = '接续本机素材'
        if task.get('job_id') and state not in {'DONE', 'UPLOAD_ERROR', 'UPLOADING', 'READY_UPLOAD'}:
            view = self.batch.view(task['job_id'])
            label = view['label']
            total = view['expected'] or view['total'] or total
            completed = sum(r['pipeline_status'] == 'COMPLETED' for r in view['items'])
            if state == 'NEEDS_ATTENTION':
                missing = [r['rank'] for r in view['items'] if r['pipeline_status'] != 'COMPLETED']
                detail = '需要处理：' + '、'.join('Rank ' + rank for rank in missing)
            if view['phase'] in {'SCRIPTS', 'TITLES'}:
                is_script = view['phase'] == 'SCRIPTS'
                ready_key = 'script_ready' if is_script else 'title_ready'
                completed = sum(bool(r[ready_key]) for r in view['items'] if r['in_scope'])
                missing = [r['rank'] for r in view['items'] if r['in_scope'] and not r[ready_key]]
                label = ('待确认口播' if is_script else '待确认标题') if not missing else ('口播待补齐' if is_script else '标题待补齐')
                detail = ('待补齐：' + '、'.join('Rank ' + r for r in missing) + '\n' if missing else '') + '在同一张确认卡内修改、翻页和批量确认。'
            job = self.store.get(task['job_id'])
            waiting = getattr(self.submission.manager, 'confirmation_wait', None)
            projection = waiting.view(job) if waiting else {}
            if projection.get('label'):
                label = projection['label']
                detail += '\n等待期间不占制作线程；收到负责人确认后排在确认时正在执行的任务之后。'
            if job.get('control_request') or job['state'] in {'STOPPED', 'STOPPING'}:
                label, detail = '已停止', '任务已按你的操作停止，未自动续跑。'
        elif state == 'DONE':
            completed = total = len(task.get('delivered_ranks') or [])
        notices = list(task.get('cloud_notices') or [])
        if task.get('cloud_notice') and task['cloud_notice'] not in notices:
            notices.append(task['cloud_notice'])
        detail = '\n\n'.join(filter(None, [detail, *notices]))
        signature = fingerprint([state, label, completed, total, task.get('error'), task.get('delivery', {}).get('share_url'), task.get('revision', 0), notices])
        delivery_revision = ['DELIVERY_V1', task.get('delivery', {}).get('share_url'), task.get('delivered_ranks'), bool(task.get('delivery_accepted'))]
        delivery_due = state == 'DONE' and (task.get('notification_versions', {}).get('delivery') or {}).get('signature') != fingerprint(delivery_revision)
        if not force and not delivery_due and task.get('last_status_signature') == signature:
            return
        if not force and not delivery_due and task.get('last_status_label') == label and time.time() - task.get('last_status_at', 0) < 30:
            return
        attention = state in {'MEDIA_ERROR', 'SUBMIT_ERROR', 'UPLOAD_ERROR', 'DONE', 'NEEDS_ATTENTION'}
        mention = task['owner'] if attention and task.get('last_status_signature') != signature else ''
        payload = cards.status(task, label, completed, total, detail)
        revision = [signature, time.time() if force else 0]
        # Updating the original status card leaves it buried above the review
        # conversation. Deliver the actual result (and acceptance action) once
        # at the bottom of the chat, rather than a generic attention notice.
        self.notify_slot(task, 'status', payload, revision, '' if state == 'DONE' else mention)
        if state == 'DONE':
            self.notify_slot(task, 'delivery', payload, delivery_revision,
                             task['owner'] if not task.get('delivery_message_id') else '')
        self.journal.update(task['id'], last_status_signature=signature, last_status_label=label, last_status_at=time.time())

    def flush_notifications(self):
        for row in self.journal.pending_notifications():
            data = row['data']
            try:
                if row['k'].startswith('reviewwait-'):
                    ident = row['k'].split('-', 2)[1]
                    if not self.confirmation_task(ident):
                        self.journal.suppress(row['k'], 'LOCAL_JOB_NO_FEISHU')
                        continue
                waiting = getattr(self.submission.manager, 'confirmation_wait', None)
                if row['k'].startswith('reviewwait-') and (not waiting or not waiting.can_send(row['k'])):
                    self.journal.delivered(row['k'])
                    continue
                task = self.journal.task(data['task_id']) if data['task_id'] else None
                slot = row['k'].split('-', 1)[0] if task else ''
                if task and task.get('latest_notifications', {}).get(slot) != row['k']:
                    self.journal.delivered(row['k'])
                    continue
                payload = copy.deepcopy(data['card'])
                if data['mention']:
                    payload['elements'].insert(0, {'tag': 'markdown', 'content': '<at id="' + data['mention'] + '"></at>'})
                message_id = (task or {}).get(slot + '_message_id') or data['message_id']
                if message_id:
                    self.transport.update(message_id, payload)
                else:
                    message_id = self.transport.send(data['chat'], payload, row['k'])
                if task:
                    self.journal.update(task['id'], **{slot + '_message_id': message_id})
                elif not data.get('message_id'):
                    for element in payload.get('elements', []):
                        if element.get('tag') == 'form':
                            for field in element.get('elements', []):
                                fid = field.get('value', {}).get('form_id')
                                if fid:
                                    self.journal.update_form(fid, message_id=message_id)
                                    break
                self.journal.delivered(row['k'])
            except Exception as error:
                self.journal.defer(row['k'], row['attempts'] + 1, str(error) if re.fullmatch(r'[A-Z0-9_]+', str(error)) else 'MESSAGE_FAILED')

    def confirmation_task(self, ident):
        """Only a real task binding authorizes task messages in Feishu."""
        return next((t for t in self.journal.tasks() if t.get('job_id') == ident), None)

    def queue_confirmation_reminder(self, job, wait):
        """One normal @ reminder per durable wait, with existing bot identity."""
        config = self.journal.config()
        if not config.get('enabled') or not config.get('chat_id'):
            return False
        task = self.confirmation_task(job['id'])
        if not task:
            return False
        owner = task.get('owner')
        chat = task.get('chat')
        if not owner or chat != config['chat_id']:
            return False
        label = {'SCRIPTS': '口播', 'TITLES': '标题', 'WAITING_SUPPLEMENT_MEDIA': '补充画面',
                 'WAITING_FINAL_REVIEW': '成片', 'WAITING_OVERLAY_APPROVAL': '原文字处理'}.get(wait['phase'], '内容')
        name = job.get('job_name') or job['product_name']
        location = '请在原确认卡核对并确认。'
        payload = cards.card('待确认任务催办', [cards.text(name + '\n' + label + '已等待确认超过10分钟。' + location +
            '\n满20分钟将自动挂起，让其他任务先执行；确认后保留原进度并排在当时正在执行的任务之后。')])
        # A standalone reminder for the existing bound task only.
        self.journal.notify('reviewwait-' + job['id'] + '-' + wait['cycle'], chat,
                            payload, mention=owner)
        return True

    def monitor_once(self):
        if not self.bot_open_id:
            info = self.transport.request('GET', '/bot/v3/info/')
            self.bot_open_id = (info.get('bot') or {}).get('open_id', '')
        for task in self.journal.tasks():
            state = task['state']
            if state in {'RECEIVED', 'DOWNLOADING', 'SUBMITTING'} and not task.get('job_id'):
                self.schedule(task['id'], 'intake')
            if task.get('job_id') and state in {'PROCESSING', 'NEEDS_ATTENTION'}:
                try:
                    job = self.store.get(task['job_id'])
                except KeyError:
                    # A retained Feishu record can outlive a deleted local job.
                    # Respect that deletion and keep monitoring later tasks.
                    continue
                if job.get('deleted_at') or job.get('control_request'):
                    continue
                if job['state'] == 'INTERRUPTED' and job['id'] not in self.recovered_jobs:
                    self.recovered_jobs.add(job['id'])
                    self.journal.update(task['id'], recovery_requested_at=time.time())
                    try:
                        recovery.resume(self.submission.manager, job['id'], task['id'], automatic=True)
                    except ValueError as error:
                        self.journal.update(task['id'], state='NEEDS_ATTENTION', error=human(error))
                    job = self.store.get(job['id'])
                view = self.batch.view(job['id'])
                self.push_review(task)
                completed = {r['rank'] for r in view['items'] if r['pipeline_status'] == 'COMPLETED'}
                busy = recovery.busy(self.submission.manager, job) or job['state'] in {'RUNNING', 'QUEUED', 'STOPPING'} or any(x.get('operation_inflight') for x in job['items'])
                if busy and task['state'] == 'NEEDS_ATTENTION':
                    self.journal.update(task['id'], state='PROCESSING', error='')
                if view['phase'] in {'SCRIPTS', 'TITLES'} and view['all_scripts_ready' if view['phase'] == 'SCRIPTS' else 'all_titles_ready'] and task['state'] == 'NEEDS_ATTENTION':
                    self.journal.update(task['id'], state='PROCESSING', error='')
                if view['phase'] == 'RUNNING' and not busy and completed - set(task.get('delivered_ranks', [])):
                    self.journal.update(task['id'], state='READY_UPLOAD')
                    self.schedule(task['id'], 'upload')
                elif view['phase'] == 'RUNNING' and not busy and len(completed) != len(view['items']):
                    self.journal.update(task['id'], state='NEEDS_ATTENTION', error='部分视频需要处理，请重试未完成项')
                elif view['phase'] in {'SCRIPTS', 'TITLES'} and not busy and not view['all_scripts_ready' if view['phase'] == 'SCRIPTS' else 'all_titles_ready']:
                    self.journal.update(task['id'], state='NEEDS_ATTENTION', error='内容准备未完成，可在确认卡补齐文案或重试未完成项')
            if state in {'READY_UPLOAD', 'UPLOADING'}:
                self.schedule(task['id'], 'upload')
            self.push_status(self.journal.task(task['id']))
        self.flush_notifications()

    def monitor_loop(self):
        while not self.stop_event.wait(2):
            try:
                self.monitor_once()
                self.last_monitor_success_at = time.time()
                self.last_error = ''
            except Exception as error:
                self.last_error = str(error) if re.fullmatch(r'[A-Z0-9_]+', str(error)) else 'FEISHU_TASK_MONITOR_FAILED'
            self.stop_event.wait(3)
