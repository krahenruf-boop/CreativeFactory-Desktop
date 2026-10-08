# 0.1.17 — Supplement Recovery

- Validate, hash and bind supplement media to the selected item; deduplicate repeated local intake.
- Plan against the actual remaining speech duration, then invalidate only affected downstream checkpoints.
- Commit the supplement plan and durable resume request together. Existing approved content and TTS are preserved.
- Show the remaining footage gap, and automatically continue when the validated plan fits.
- Schema 3 and the 0.1.16 mandatory CTA contract are unchanged.

# Creative Factory 0.1.16

Required CTA Arrow & Output Verification

- 用户未明确关闭 CTA 时，所有模式默认添加箭头；AI 建议、空设置与旧 omit_added_cta 标记不能关闭。
- 复用当前冻结箭头素材、16% 红色箭身宽度、垂直 Bounce 与边界避让位置，覆盖最终时间轴最后 5 秒。
- 原箭头只有在实际保留的结尾画面通过像素验证后才能免去新增层。
- 完整解码最终 5 秒，匹配箭头轮廓及垂直运动；验证绑定 MP4 SHA，缺失不可 PASS。
- 缺失最多执行一次 CTA-only 补加；保留旧 MP4，复制原音轨，不再调用文案、配音或字幕生成。
- Schema 保持 3。现有成片通过明确的 CTA_REPAIR 分支修复并更新有效输出。

## Creative Factory 0.1.15

Automatic Production Flow · Batch Confirmation Stabilization · Duration Auto Fit

- 用户统一确认全部口播，再确认全部标题；随后自动执行至成片。
- 事务内持久化恢复意图，统一恢复队列；重复确认不重复派发，重启可恢复已批准批次。
- 修复 CTA_SUBTITLE_COLLISION 被误识别为标题审批，以及旧数字/补零素材编号的 checkpoint 覆盖问题。
- 普通视觉 QA 为 WARNING；严重问题最多一次明确修复，失败保留原因，不再循环人工 Review。
- 完整脚本锁定，采用本地 atempo 保持音高；配音/画面速度上限 1.50x，优先自然范围。
- 时长超出适配上限才进入 DURATION_FIT_FAILED；其它素材独立继续。
- Schema 保持 3；Bundle、数据目录、Keychain、Logo、软删除不变。
- Codex CLI 为实际分析/决策实现，AUTO 优先已登录 Codex，其次已配置 OpenAI API；调度与媒体执行由 Creative Factory 负责。

验收采用隔离数据库、Mock Provider 和一条 6 秒短素材，无付费 API 请求、无 ProPainter。

## Company Mac deployment preparation (0.1.17 base; acceptance pending)

- Pin the mature headless Engine source and runtime hashes for independent company builds.
- Add isolated runtime smoke, protected installation, approved-tag updates and schema-aware rollback.
- Keep Bundle ID, AutoCut data directory, SQLite schema 3 and provider Keychain service unchanged.
- Exclude noncommercial ProPainter and user-deferred Mathilde font from company distribution.
- Company provider/Golden E2E and update drill are required before publishing a production channel.
