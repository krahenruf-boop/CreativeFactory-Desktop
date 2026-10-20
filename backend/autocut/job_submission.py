"""Shared submission rules; callers supply their own local-file authorization."""
import json
import shutil
import uuid
from pathlib import Path

from .file_storage import output_directory
from .references import parse
from .script_language import target_language
from .storage import product_name
from .task_instructions import InstructionService, safe_raw, digest
from .workbench import merge_tags, script_snapshot


class JobSubmission:
    def __init__(self, store, manager, router, feishu):
        self.store, self.manager, self.feishu = store, manager, feishu
        self.instructions = InstructionService(store.home, router)

    def context(self, data, authorize):
        folder = Path(data['input_path']).expanduser().resolve(strict=True)
        authorize(folder)
        if not folder.is_dir():
            raise ValueError('SOURCE_NOT_FOLDER')
        product = self.store.product(data['product_profile_id'])
        if not product:
            raise ValueError('INVALID_REQUEST')
        reference = {}
        if data.get('feishu_source_id'):
            selected = (data.get('script_reference_ids') or data.get('script_reference_id')) if data.get('script_mode') == 'TABLE_SELECTED' else None
            reference = self.feishu.reference(data['feishu_source_id'], data.get('feishu_sheet_id'), data['market'], selected)
        if data.get('script_reference_file'):
            path = Path(data['script_reference_file']).resolve(strict=True)
            authorize(path)
            reference = parse(str(path))
        from app.s3.visual_preset import load_preset
        context = dict(product=product_name(data.get('product_name') or product['product_name']),
                       market=data['market'], mode=data['mode'], preset_id=product['id'],
                       usage_scope=product.get('usage_scope', 'production'),
                       current_preset=load_preset(), reference=reference)
        binding = digest(dict(context=context, folder=str(folder), source_names=data.get('source_names'),
                              raw=safe_raw(data.get('custom_instruction_raw', '')), tags=data.get('custom_tag_ids', [])))
        return context, binding

    def snapshot(self, data, authorize):
        from .title_policy import snapshot
        from .source_frame import snapshot as frame_snapshot
        result = frame_snapshot(snapshot(self._snapshot(data, authorize)), data)
        from .source_title_overlay import snapshot as overlay_snapshot
        result = overlay_snapshot(result, data)
        from .source_library import POLICY
        result['production_preflight_policy'] = POLICY
        return result

    def _snapshot(self, data, authorize):
        if data.get('custom_remix'):
            from .custom_remix import snapshot
            return snapshot(data)
        raw = safe_raw(data.get('custom_instruction_raw', ''))
        if not raw and not data.get('custom_tag_ids'):
            return self.instructions.resolve('', {}, 'auto')
        context, binding = self.context(data, authorize)
        receipt = data.get('instruction_receipt')
        if receipt:
            if not isinstance(receipt, str) or len(receipt) != 32 or any(c not in '0123456789abcdef' for c in receipt):
                raise ValueError('CUSTOM_INSTRUCTION_INVALID')
            path = self.store.home / 'jobs/instruction_drafts' / (receipt + '.json')
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                raise ValueError('CUSTOM_INSTRUCTION_INVALID') from None
            if record['binding'] != binding or record.get('sha256') != digest(record['snapshot']):
                raise ValueError('CUSTOM_INSTRUCTION_INVALID')
            return record['snapshot']
        return merge_tags(self.instructions.resolve(raw, context, self.store.settings().get('ai_driver', 'auto')),
                          data.get('custom_tag_ids', []), self.store, context)

    def resolve(self, data, authorize):
        _, binding = self.context(data, authorize)
        snapshot = self.snapshot(data, authorize)
        receipt = uuid.uuid4().hex
        folder = self.store.home / 'jobs/instruction_drafts'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (receipt + '.json')).write_text(json.dumps(dict(binding=binding, snapshot=snapshot, sha256=digest(snapshot)), ensure_ascii=False))
        return dict(receipt=receipt, summary=snapshot['instruction_summary'], status=snapshot['custom_instruction_status'],
                    issues=snapshot['instruction_issues'], warning='部分要求涉及程序或全局配置，未应用。' if 'CUSTOM_INSTRUCTION_FORBIDDEN_ACTION' in snapshot['instruction_issues'] else '部分要求未应用，已按安全范围处理。' if snapshot['instruction_issues'] else '')

    def submit(self, spec, authorize):
        data = dict(spec)
        request = data.get('request_id')
        if not isinstance(request, str) or not 10 <= len(request) <= 80:
            raise ValueError('INVALID_REQUEST')
        folder = Path(data['input_path']).expanduser().resolve(strict=True)
        authorize(folder)
        if not folder.is_dir():
            raise ValueError('SOURCE_NOT_FOLDER')
        existing = self.store.by_request(request)
        if existing:
            return existing
        product = self.store.product(data['product_profile_id'])
        if not product:
            raise ValueError('INVALID_REQUEST')
        for key, allowed in [('market', {'Brazil', 'Mexico'}), ('mode', {'Transport', 'Remix'}),
                             ('title_approval_mode', {'auto', 'manual'}), ('watermark_mode', {'auto', 'handled'}),
                             ('script_reference_mode', {'smart', 'prefer_original'})]:
            if data.get(key) not in allowed:
                raise ValueError('INVALID_REQUEST')
        name = product_name(data.get('product_name') if data.get('product_name') is not None else product['product_name'] if product['id'] != 'general' else '')
        data.update(input_path=str(folder), product_name=name, product_profile=dict(product, product_name=name))
        data['target_language'] = target_language(data)
        if data['mode'] == 'Transport':
            data['output_count'] = None
        if data.get('feishu_source_id'):
            if data.get('script_reference_file'):
                raise ValueError('INVALID_REQUEST')
            selected = (data.get('script_reference_ids') or data.get('script_reference_id')) if data.get('script_mode') == 'TABLE_SELECTED' else None
            data['reference'] = self.feishu.reference(data['feishu_source_id'], data.get('feishu_sheet_id'), data['market'], selected)
        if data.get('script_reference_file'):
            authorize(Path(data['script_reference_file']).resolve(strict=True))
            data['reference'] = parse(data['script_reference_file'])
        data = script_snapshot(data, self.store)
        if data.get('custom_remix'):
            from .custom_remix import validate, music_snapshot
            data['custom_remix'] = validate(data)
            data['custom_remix_music'] = music_snapshot(self.store.home, data['custom_remix'], authorize)
        snapshot = self.snapshot(data, authorize)
        data.pop('instruction_receipt', None)
        data.update(snapshot)
        if data['mode'] == 'Remix' and snapshot['resolved_overrides_json'].get('output_count'):
            data['output_count'] = snapshot['resolved_overrides_json']['output_count']
        data.update(content_confirmation_required=True, batch_review_required=True, storage_policy='source-v1', original_input_path=str(folder))
        if data.get('output_dir'):
            destination = Path(data['output_dir']).expanduser()
            if not destination.is_absolute():
                raise ValueError('INVALID_REQUEST')
            data['output_dir'] = str(destination.resolve())
            output_directory(data)
        job, created = self.store.create(data)
        if created:
            if data.get('script_reference_file'):
                destination = self.store.home / 'jobs' / job['id'] / 'references' / Path(data['script_reference_file']).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(data['script_reference_file'], destination)
                self.store.update(job['id'], script_reference_file=str(destination))
            self.manager.enqueue(job['id'])
        return self.store.get(job['id'])
