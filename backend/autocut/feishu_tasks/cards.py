"""Feishu cards contain review text and opaque actions, never local credentials."""

def plain(text):
    return {'tag': 'plain_text', 'content': str(text)}


def text(value):
    return {'tag': 'div', 'text': plain(value)}


def button(label, action, primary=False, disabled=False, submit=False):
    value = dict(tag='button', text=plain(label), type='primary' if primary else 'default',
                 value=action, disabled=disabled)
    if submit:
        value.update(action_type='form_submit', name=str(action.get('action', 'submit')) + str(action.get('page', '')))
    return value


def actions(*buttons):
    return {'tag': 'action', 'actions': list(buttons)}


def card(title, elements, owner='', color='blue'):
    if owner:
        elements = [{'tag': 'markdown', 'content': '<at id="' + owner + '"></at>'}] + elements
    return dict(config=dict(wide_screen_mode=True, update_multi=True),
                header=dict(title=plain(title), template=color), elements=elements)


def select(name, label, options, selected=None, required=False):
    item = dict(tag='select_static', name=name, placeholder=plain(label), width='fill',
                options=[dict(text=plain(label), value=str(value)) for value, label in options], required=required)
    if selected is not None:
        item['initial_option'] = str(selected)
    return item


def input_field(name, label, default='', limit=1000, required=False, multiline=False):
    return dict(tag='input', name=name, label=plain(label), default_value=str(default), width='fill',
                max_length=min(limit, 1000), required=required, input_type='multiline_text' if multiline else 'text', placeholder=plain(label))


def new_task(form_id, products, defaults=None, media_only=False, error=''):
    d = defaults or {}
    fields = [] if media_only else [
        input_field('product_name', '产品名称', d.get('product_name', ''), 100, True),
        select('market', '目标市场', [('Brazil', '巴西 · pt-BR'), ('Mexico', '墨西哥 · es-MX')], d.get('market', 'Brazil'), True),
        select('mode', '剪辑模式', [('Transport', '搬运'), ('Remix', '混剪')], d.get('mode', 'Transport'), True),
    ]
    fields += [input_field('expected_count', '预计素材数量', d.get('expected_count', '10'), 3, True),
               input_field('share_url', '夸克素材文件夹分享链接', d.get('share_url', ''), 600, True),
               input_field('passcode', '提取码（如有）', d.get('passcode', ''), 32)]
    if not media_only:
        fields += [input_field('requirements', '本任务要求（可选）', d.get('requirements', ''), 4000, multiline=True)]
    for field in fields:
        field['width'] = 'fill'
    fields.append(button('提交素材' if media_only else '提交任务', dict(action='submit', form_id=form_id), True, submit=True))
    return card('补交素材' if media_only else '新建剪辑任务', ([text(error)] if error else []) + [text('每个任务提供一个文件夹分享链接，也可粘贴夸克整段分享文案。收到任务编号即受理成功，请勿重复提交。'),
                dict(tag='form', name='task_form', elements=fields)])


def review_pages(view):
    sections = []
    for row in view['items']:
        if not row['in_scope']:
            continue
        if view['phase'] == 'SCRIPTS':
            spoken = row.get('script') or {}
            value = 'Rank ' + row['rank'] + '\n' + (spoken.get('text') or row.get('script_error') or '无口播') + '\n\n中文对照\n' + (spoken.get('text_zh') or '暂无')
            check=row.get('production_preflight') or {}
            if check.get('message'):value+='\n\n画面预检查：'+check['message']
            if check.get('duration_lower') is not None:value+='\n口播预计 %.1f–%.1f 秒；现有画面 %.1f 秒'%(check['duration_lower'],check['duration_upper'],check['available_seconds'])
            if (row.get('source_health') or {}).get('frame_issue'):value+='\n画幅需要负责人处理，完整前景不会自动裁切'
        else:
            values = row['title_candidates'] + ([row['custom_title']] if row.get('custom_title') else [])
            value = 'Rank ' + row['rank'] + '\n' + '\n\n'.join(str(t['id']) + ' · ' + t['text'] + '\n' + t.get('text_zh', '') + ('\n排版：'+t['preflight']['message'] if (t.get('preflight') or {}).get('message') else '') for t in values)
        # Split exceptional long copy without silently dropping its full text.
        chunks = [value[i:i + 3200] for i in range(0, len(value), 3200)] or ['']
        sections.extend((row, chunk, n + 1, len(chunks)) for n, chunk in enumerate(chunks))
    pages, page, size = [], [], 0
    for section in sections:
        amount = len(section[1].encode('utf-8')) + 1700
        if page and (len(page) == 3 or size + amount > 20000):
            pages.append(page)
            page, size = [], 0
        page.append(section)
        size += amount
    if page:
        pages.append(page)
    return pages or [[]]


def review(task, view):
    pages = review_pages(view)
    index = max(0, min(int(task.get('page', 0)), len(pages) - 1))
    revision = view['action_token']
    base = dict(task_id=task['id'], revision=revision)
    elements = [text(task['id'] + ' · ' + task['product_name'] + ' · 第 ' + str(index + 1) + '/' + str(len(pages)) + ' 页')]
    if task.get('review_notice'):
        elements.append(text(task['review_notice']))
    ready = view['all_scripts_ready'] if view['phase'] == 'SCRIPTS' else view['all_titles_ready']
    if ready:
        elements.append(actions(button('确认全部口播' if view['phase'] == 'SCRIPTS' else '确认全部标题并开始剪辑',
                                       dict(base, action='approve_scripts' if view['phase'] == 'SCRIPTS' else 'approve_titles'), True)))
    if len(pages) > 1:
        top_nav = []
        if index > 0:
            top_nav.append(button('上一页', dict(base, action='page', page=index - 1)))
        if index < len(pages) - 1:
            top_nav.append(button('下一页', dict(base, action='page', page=index + 1)))
        elements.append(actions(*top_nav))
    chosen = task.get('selections', {})
    for row, value, part, parts in pages[index]:
        elements += [dict(tag='hr'), text(value + ('\n全文分段 ' + str(part) + '/' + str(parts) if parts > 1 else ''))]
        rank = row['rank']
        if view['phase'] == 'SCRIPTS':
            elements.append(actions(button('替换 Rank ' + rank + ' 口播', dict(base, action='edit_script', rank=rank))))
        else:
            titles = row['title_candidates'] + ([row['custom_title']] if row.get('custom_title') else [])
            elements.append(text('Rank ' + rank + ' · ' + ('已选标题 ' + str(chosen[rank]) if rank in chosen else '尚未选择标题')))
            elements.append(actions(*[button(('✓ 已选 ' if str(chosen.get(rank)) == str(t['id']) else '选用标题 ') + str(t['id']), dict(base, action='select_title', rank=rank, title_id=str(t['id'])), primary=str(chosen.get(rank)) == str(t['id']),disabled=(t.get('preflight') or {}).get('status')=='BLOCKED') for t in titles]))
            elements.append(actions(button('重生标题', dict(base, action='regenerate_title', rank=rank)),
                                    button('自行填写', dict(base, action='edit_title', rank=rank)),
                                    button('返回修改口播', dict(base, action='reopen_script', rank=rank))))
    navigation = []
    if index > 0:
        navigation.append(button('上一页', dict(base, action='page', page=index - 1)))
    if index < len(pages) - 1:
        navigation.append(button('下一页', dict(base, action='page', page=index + 1)))
    if view['phase'] == 'SCRIPTS':
        if navigation:
            elements.append(actions(*navigation))
        elements.append(text(str(view['script_ready_count']) + '/' + str(view['expected']) + ' 条口播已准备'))
        if view['all_scripts_ready']:
            elements.append(actions(button('确认全部口播', dict(base, action='approve_scripts'), True)))
    else:
        if navigation:
            elements.append(actions(*navigation))
        elements.append(text(str(len(chosen)) + '/' + str(len(view['ranks'])) + ' 条标题已选择 · 点击“选用标题”即保存，翻页不丢失'))
        if view['all_titles_ready']:
            elements.append(actions(button('确认全部标题并开始剪辑', dict(base, action='approve_titles'), True)))
        if not view['all_titles_ready']:
            elements.append(text('请检查尚未准备好的标题；过长或缺字的标题需编辑或另选。'))
    return card('确认口播' if view['phase'] == 'SCRIPTS' else '确认标题', elements)


def edit(form_id, task, rank, kind, revision, fields=None, error='', busy=False):
    from .edit_fields import split_text, merge_fields
    label = '替换口播' if kind == 'script' else '填写标题'
    values = merge_fields(fields or {})
    limit = 1000 if kind == 'script' else 300
    parts = split_text(values.get('text', ''), limit)
    fields = [input_field('text' if index == 1 else 'text_part_' + str(index),
        label + ' · Rank ' + rank + (f'（第 {index}/{len(parts)} 段）' if len(parts) > 1 else ''),
        part, limit, len(parts) == 1, True) for index, part in enumerate(parts, 1)]
    if kind == 'script':
        fields.append(select('language', '文案语言', [('auto', '自动识别文案语言'), ('pt-BR', '巴西葡萄牙语'), ('es-MX', '墨西哥西班牙语'), ('en', '英文'), ('zh-CN', '中文')], values.get('language', 'auto'), True))
    base = dict(action='apply_edit', form_id=form_id, task_id=task['id'], rank=rank, kind=kind, revision=revision)
    fields.append(button('正在保存…' if busy else '保存并返回确认', base, True, disabled=busy, submit=True))
    if int(rank) < int(task['expected_count']):
        fields.append(button('保存并修改下一条', dict(base, next_rank=True), disabled=busy, submit=True))
        fields[-1]['name'] = 'save_next'
    elements = [text(task['id'] + ' · Rank ' + rank)]
    if error:
        elements.append(text(error))
    if len(parts) > 1:
        elements.append(text('完整文案已分段显示，保存时自动合并；可清空不需要的段落。'))
    elements += [dict(tag='form', name='edit_form', elements=fields), text('自动识别原文语言，转换为任务市场语言并补充中文对照。保存后仍需批量确认。'),
                 actions(button('返回确认列表', dict(action='cancel_edit', task_id=task['id'], revision=revision), disabled=busy))]
    return card(label, elements)


def status(task, label, completed=0, total=0, detail=''):
    elements = [text(task['id'] + ' · ' + task['product_name']), text(label + (' · ' + str(completed) + '/' + str(total) if total else ''))]
    if task.get('local_media'):
        elements.append(text('已接续本机素材，口播、标题确认与成片交付仍由本群机器人推送，无需另建网页任务。'))
    if detail:
        elements.append(text(detail))
    delivery = task.get('delivery') or {}
    if delivery.get('share_url'):
        elements += [{'tag': 'markdown', 'content': '[打开成片文件夹](' + delivery['share_url'] + ')'}, text('提取码：' + delivery.get('passcode', '') + ' · 链接有效期七天')]
    if task.get('error'):
        elements.append(text(task['error']))
    if task['state'] in {'MEDIA_ERROR', 'SUBMIT_ERROR'}:
        elements.append(actions(button('重试当前素材', dict(action='retry_intake', task_id=task['id'], revision=str(task.get('revision', 0)))), button('补交素材', dict(action='replace_media', task_id=task['id'], revision=str(task.get('revision', 0))))))
    if task['state'] == 'UPLOAD_ERROR':
        elements.append(actions(button('重试上传成片', dict(action='retry_upload', task_id=task['id'], revision=str(task.get('revision', 0))), True)))
    if task['state'] == 'NEEDS_ATTENTION' and task.get('job_id'):
        elements.append(actions(button('重试未完成项', dict(action='retry_job', task_id=task['id'], revision=str(task.get('revision', 0))))))
    if task['state'] == 'DONE' and task.get('test'):
        if task.get('delivery_accepted'):
            elements.append(text('交付验收已确认，可在个人 Mac 设置中开启正式接单。'))
        else:
            elements.append(text('请管理员打开成片链接，核对三条视频后确认交付。正式接单仍保持关闭。'))
            elements.append(actions(button('确认三条成片交付', dict(action='accept_delivery', task_id=task['id'], revision=str(task.get('revision', 0))), True)))
    return card('Creative Factory · ' + label, elements, color='green' if task['state'] == 'DONE' else 'orange' if task.get('error') else 'blue')
