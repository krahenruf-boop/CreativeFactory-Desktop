# Headless Transport execution V1

新任务使用 `scripts/headless_transport.py` / `app.s3.headless_runtime.Harness`，不再复制旧任务里逐条串行的脚本。现有 Overlay、视觉 Preset、ProPainter 安装和 Provider 保持原样。人工内容/视觉判断仍由 Codex完成，调度器不依赖某个 Codex模型，也不会把技术通过当作视觉通过。

默认设置在 `presets/headless_execution.json`：ASR 2，TTS 3（429后本任务降到2），CPU轻任务2，网络队列2，MPS跨进程1，Final Render 2。当前Mac短片实测后使用 `h264_videotoolbox` 12Mbps、`allow_sw=0`；libx264 fast/CRF18保留为显式软件选择。新设备先做短片编码验收，不假定硬件编码可用。短片无热压力告警不等于完成长时间温度测试。

## 使用顺序

命令中的 WORK 是本次任务的 `work/`，不指向旧任务。所有输入视频只读。

```sh
.venv/bin/python scripts/headless_transport.py scan --work "$WORK"
.venv/bin/python scripts/headless_transport.py asr --work "$WORK" --model "/existing/ggml-base.bin"
.venv/bin/python scripts/headless_transport.py deepseek --work "$WORK" --input unique_contexts.json --execute
```

先完成ASR/画面审核及语义去重。`unique_contexts.json` 是原 Context schema 的数组（`dedupe_reviewed=true`）；每次请求最多5项，默认直接3标题：A痛点、B卖点、C情绪。事实和长度审核后原样交给用户。语言规则仍是 `DEEPSEEK_TIKTOK_BR_V1`，批量封装/三标题协议另有 `DEEPSEEK_TIKTOK_BR_BATCH_V1`，两者SHA进入每项缓存。

Markdown fence、前后说明、字符串以外的尾逗号本地处理。重复JSON键、多对象/截断JSON不猜内容。有效envelope中的缺失/不合格单项最多修一次；其它成功项不重发。整包无法可靠解析或计费不确定时停止该包并Review，不猜测性重发五项。只有连接尚未发出可安全重试一次。Claim错误不盲目重写。旧单组10/5标题入口及其指纹保留用于历史兼容；新批次走batch入口。

DeepSeek真实HTTP总数读取 `batch_summary.http_requests` / Performance事件；不要把“某项曾参与共享请求”的计数相加当作HTTP总数。正常10个不同组首轮2次；内容需要改稿时可能更多，2–4是正常目标而非硬编码假数据。本轮批协议使用Mock验收，没有再次消费已有文案API。

```sh
.venv/bin/python scripts/headless_transport.py approve-titles --work "$WORK" --input record.json --review title_review.json
.venv/bin/python scripts/headless_transport.py tts --work "$WORK" --input tts_jobs.json --catalog verified_catalog.json --existing-results existing_tts_results.json --execute
```

`title_review.json` 需要用户明确确认、原结果SHA、候选ID和原有八项Codex事实/语言/画面审核。已经批准的同一group禁止自动以不同上下文重付费；改稿须显式撤回批准。不得自行改写三个候选再冒充原API结果。

TTS jobs包含原Provider payload及review（`title_status=USER_APPROVED`、`script_status=CODEX_APPROVED`、`tts_text_sha256`是 `fingerprint(prepare_tts_text(text))`）。catalog必须是当前账号已验证目录，含scope。通过现有 `TTS` schema与 `Engine.tts_payload` 校验模型/参数。CLI会只读现有Creative Factory数据库中该scope的TTS结果/尝试记录；可额外传入旧Headless结果数组。采用相同s2-v1 fingerprint；音频/Alignment有效则复制小型音频缓存到work，绝不重新生成。不确定旧请求和成功但损坏的缓存均阻止重新付费。Key只从 `ELEVENLABS_API_KEY` 读取。

TTS成功响应在本地QA前保存，技术QA失败、重启、视觉返工都不会重新付费。429降低尚未启动任务的并发；失败项不自动重试。ASR按source/model/binary SHA缓存，允许导入同source/model SHA的历史transcript。未知旧缓存不能只按文件名冒认命中。

## 阶段记录与恢复

`Harness.submit(stage,item,stage_inputs,callback,paid=...)` 返回Future；不同资源队列互不阻塞。先提交独立工作，再在依赖处等待。不要先等全部MPS任务结束才创建字幕/计划。分析、alignment、EditPlan、视觉审核等步骤用同一个Harness的 `profiler.span(...)` 记录真实执行区间。

`work/performance.json` 包含用户要求的全部阶段（另加LAYOUT_PREFLIGHT），未发生的标NOT_OBSERVED。wall_seconds为该阶段区间并集，worker_seconds为任务时长之和，不能把并行阶段的wall相加。active_wall是观测区间并集，elapsed_span包含观测首尾之间的空档。CPU为Python调用线程时间，不冒充FFmpeg/模型CPU。

关闭Harness或执行report会更新RUN_REPORT内Performance Summary区块。每阶段有独立输入指纹checkpoint：ASR_DONE、DEEPSEEK_DONE、TITLE_APPROVED、TTS_DONE、OVERLAY_LOCKED、FINAL_RENDERED、QA_DONE。QA_DONE只代表技术阶段，视觉仍另行审核。完成结果校验artifact SHA；付费IN_FLIGHT在恢复时标BILLING_UNCERTAIN，不重发。视觉参数不要放进ASR/TTS的fingerprint。

## ProPainter预算

```sh
.venv/bin/python scripts/headless_overlay.py --work "$WORK" --region-id stable_region_id --plan preview.json --allow-experimental-propainter
```

region-id来自本任务Overlay分析，整个区域生命周期保持一致；不能换ID重置预算。仅复杂纹理静态区域、精确mask通过审核、研究scope才允许调用。卡片、边角、简单背景、可裁切区域拒绝。现有Overlay render gate仍必须通过。

每区域1次2–5秒Preview；只有失败复核明确为MASK_EDGE才允许另一次，须新mask SHA、上一输出SHA和 `previous_visual_review`。其它失败立即Review。两次后终止，不扩大mask、不降低质量。最终区间需当前mask、当前preview SHA一致的真实视觉批准，最多一次final。预算在推理前持久化，中断也计数；记录RTF与预计final耗时。长区间不会因为希望40分钟完成就降低质量。

所有MPS调用共享本项目的跨进程锁。模型版本、参数、window、精确mask和音频保护沿用原后端，不用加速名义降低修复质量。

## Preflight、正式Render与QA

```sh
.venv/bin/python scripts/headless_transport.py preflight --work "$WORK" --input plans.json
.venv/bin/python scripts/headless_transport.py approve-preflight --work "$WORK" --input one_plan.json --review preflight_review.json
.venv/bin/python scripts/headless_transport.py render --work "$WORK" --input plans.json --execute
.venv/bin/python scripts/headless_transport.py batch-sheets --work "$WORK" --input sheet_inputs.json
```

预检使用正式尺寸的同一字体/PNG/CTA，再等比缩为540×960，避免低分辨率重新fit改变排版。输出前5秒、中间2秒、最后5秒；重叠合并。只对选中时段缩放/合成，保留原时间用于图层时窗，最后才拼接预览。源视频仍需顺序decode，不在低清预览重新推理Overlay。

预检输出技术QA与Contact Sheet。approval必须有预览SHA、Codex实际视觉PASS，以及title/subtitle/cta/overlay/source_text/watermark六项检查；改计划、源/音频/font/CTA/修复片段/渲染实现后旧批准失效。不要机械将六项都写true。预检本身没有批准就不能启动完整成片。

单次正常full render；第二次只接受完整解码损坏、编码错误或全时长才暴露的时序问题。标题/字幕/CTA摆位不能作为完整返工理由。失败MP4保留，重试指定不同输出名；不覆盖失败证据。输入、音频、方案不变时恢复走checkpoint。基于当前已验证folder计划的单主素材和已锁定interval composition执行；Supplement计划必须接回原有Transport渲染callback，当前薄入口明确拒绝而不静默丢片。调用原渲染callback时也必须复用同一preflight批准及render预算。

最终QA一次完整FFmpeg解码同时读取PTS、blackdetect、音频volumedetect及抽样图；ffprobe只读容器，不另解码像素。所有Title/Subtitle/CTA人工视觉检查与Contact Sheet复用这一帧库。Level1技术检查全量；Level2普通素材9点；Level3仅修改Overlay增加出现前/初期/中间/结束前局部时序证据。视觉结果默认PENDING，不能因为QA JSON是PASS就发布。

`sheet_inputs.json` 使用 `qa_files` 数组及可选 `overlays`（id/status/before_crop/after_crop/mask_preview）。产生BATCH_CONTACT_SHEET.jpg及BATCH_OVERLAY_QA.jpg，不重新解码。当前没有自动审美评分或后台付费模型调用。

实测与估算见 [HEADLESS_PERFORMANCE_BENCHMARK.json](HEADLESS_PERFORMANCE_BENCHMARK.json)。完整10条40–50分钟仍是目标，本轮没有重跑整批来宣称达成。
