# Headless Transport：效果优先的 Overlay 标准 V1

生效：2026-09-29。适用于所有后续新 Headless Transport 任务。本标准优先于旧素材编号模板、大矩形delogo和旧教学示例。既有MP4/源素材/历史EditPlan不自动重写。

## 默认开关与职责

- `EFFECT_FIRST_OVERLAY_ROUTER = true`。
- `ALLOW_EXPERIMENTAL_PROPAINTER = true` **仅对明确标记** `training_research` / `internal_training_research` 的任务生效。未知用途按production处理，不自动改为研究用途。
- Codex负责观察、分类、文本本土化、预览视觉评价与最终选择；代码负责路由、证据绑定、几何与渲染前检查。不得把文件存在、程序退出成功、mock测试或技术QA当成视觉PASS。
- 顺序：Analyze → Classify → 局部Preview → Compare → Lock → Final Render → 实际Visual QA。
- 不改动已验收ProPainter安装、torch、weights、Adapter/音频保护或上游源码。只经隔离subprocess后端调用。无UI/网页/Space；不影响ASR/TTS缓存和Transport原镜头顺序。

## 质量原则

原画自然、清除原文字、无拖影、无大片模糊、原排版、保护人物/产品优先，速度最后。
宁可标REVIEW_REQUIRED，也不把明显补丁/拉丝/果冻背景作为合格成片。产品自身Logo、包装印刷、色号与实物文字标PRODUCT_BRANDING并保留。

每个区域的overlay_class只能是：
`PLAIN_TEXT / WHITE_CARD / COLOR_CARD / COMMENT_CARD / POV_CARD / SEMI_TRANSPARENT_CARD / CORNER_WATERMARK / STATIC_WATERMARK / STATIC_OVER_TEXTURE / MOVING_OVER_TEXTURE / PRODUCT_BRANDING / UNKNOWN`。
UNKNOWN先复核，不直接画矩形遮盖。

## 决策

1. 卡片完整：KEEP_ORIGINAL_CARD_REPLACE_TEXT，保留本体，只处理原文字像素。
2. 确有文字/背景无法分离的证据：EXACT_CARD_REBUILD；不能仅为方便整块擦除。
3. 边缘水印且安全裁切损失≤4%：CROP；建议2–4%，保护主体。
4. 裸字+纯色/静止背景：精确mask+background patch或轻量inpaint，先局部预览。
5. 复杂布料/头发/草地/室内纹理，人物、手臂或相机运动：优先ProPainter局部预览，可根据已记录的历史失败直接跳过本条轻量试跑。
6. 轻量出现TEMPORAL_INPAINT_ARTIFACT / SMEARING / TEXT_GHOSTING / TEXT_RESIDUE / BACKGROUND_BLUR / REPEATED_TEXTURE / PATCH_EDGE_VISIBLE：停止该方案并转ProPainter预览，不扩大mask、不加重模糊。
7. ProPainter仍有明显缺陷：REVIEW_REQUIRED，不继续把失败方案做成整片。
8. 已有干净卡片、纯色块、简单静态背景、可安全crop的角落Logo、产品品牌：禁止无意义ProPainter。
9. 移动文字且无可靠逐帧mask：MOVING_MASK_REQUIRED / REVIEW_REQUIRED。当前安装仅支持STATIC_BOX；本轮不新增tracking，也不以巨大固定框覆盖运动轨迹。

ProPainter属于研究后端，不进入商业生产默认路径。商业任务复杂区域无法用合格轻量方法完成时转REVIEW，不自动越过用途限制。

## 卡片几何和文字适配

保存原card_bbox、text_bbox、replacement_bbox，以及背景色、opacity、圆角、border、shadow、padding、text_alignment。记录坐标系与分辨率，不混用source/output像素。

- 原卡片多大，新卡片就多大。面积比目标0.95–1.05；>1.08或<0.95失败。
- 四边偏差优先≤2px，最大4px；超过报OVERLAY_BOUNDARY_MISMATCH。
- 禁止expand20/30px、整行盖住、半屏白块或Generic Large Cover。
- 葡语太长：压缩自然Hook → 换行 → 轻微减字号 → 轻调字距/padding；仍放不下报TEXT_FIT_REVIEW。不得扩框。
- 裸字不能凭空增加白底/黑底/色块。
- 白底、粉底、评论卡、标签卡保留原视觉；不强制统一成大白框+Poppins。
- 原英文/西语先清理，再写pt-BR，不双语叠加。优先原标题区域，不再额外堆一套Hook。

## 局部Preview与精确Mask

卡片重建、复杂去字和ProPainter都先做2–5秒有代表性的局部Preview。输出前保存before_crop.png、after_crop.png、mask_preview.png；卡片加card_geometry.json，ProPainter加propainter_preview.mp4。

ProPainter只处理文字附近window，context32–96px；context不是mask。优先真实字形/Logo轮廓/紧贴联合mask，dilation2–4px。mask运行前实际检查所有字形（包括g等下伸部分）、轮廓、保护脸/眼/产品/手部；记录mask SHA、source_sha256、预览路径、reviewer、覆盖完整及主体安全。仅矩形大框、没有边缘检查的mask不得提交。

直接跳过轻量预览必须记录历史失败证据和相似理由，不能伪造“本条轻量FAIL”。已有Rank11研究A/B案例索引见`tools/overlay-policy/history-rank11.json`。旧Adapter资格字段可引用这个真实历史失败，同时显式记录failure_basis=historical_reference、light_preview_status=NOT_RUN；不修改Adapter。

A/B必须同源、同时间、同区域。评价：CLEAN_REMOVAL、BACKGROUND_CONTINUITY、EDGE_MATCH、TEXT_RESIDUE（5表示无残留）、TEMPORAL_STABILITY、LAYOUT_MATCH、PRODUCT_PRESERVATION。0–5分由实际观察者填写；程序不测算审美。必须另外visual_pass=true、无失败issue、连续播放检查和审阅者/时间。分数用于已通过候选间比较，耗时不进入排序。并列不强制使用AI。

除非新问题需要修正，不重复ASR/TTS，不先整片渲染差方案。Preview PASS后才处理完整对应区间；不得把仅出现在0–5秒的遮罩套到整条30秒素材。现有单Primary Adapter接入只覆盖整个被消费区间，短时Overlay不能直接走该整段入口：需由任务级Harness先完成精确区间合成，否则报OVERLAY_INTERVAL_COMPOSITION_REQUIRED，不擅自扩大时间窗口。

## EditPlan与证据

新增统一字段：`usage_scope`、`overlay_analysis.status=COMPLETE`、`overlays[]`。
旧embedded_text_regions / watermark_regions分别通过legacy_text_indices / legacy_watermark_indices逐项对应分类，不能漏项。
每个region必须包含：id、overlay_class、original_bbox、interval（源时间）、coordinate_space、strategy候选及原因。变速任务另存output_interval供最终抽帧；不得把源时间直接当输出时间。
卡片额外记录上述几何/样式；修复额外记录mask_bbox、mask_path、mask_review；复杂区域记录context、轻量失败或历史证据。

每个候选记录：strategy、source_sha256、original_bbox、preview_interval、visual_pass、visual_scores、issues、reviewer、reviewed_at、temporal_playback_checked、evidence。evidence包含所有上述实际文件路径。

Lock后`overlay_policy.regions[]`保存strategy、chosen_backend、reason、preview_result及证据SHA；plan fingerprint绑定源、区域、候选、标题、mask操作、crop、目标时长/视频速度和实验处理参数。任何相关修改或证据变动必须重新检查，不能沿用旧PASS。

- Card backend：exact_card_replace。
- ProPainter：backend=propainter / usage_scope=internal_training_research / experimental=true。
- 最终渲染用预览中已确认的实际Overlay PNG：`overlay_title_events[]`含overlay_id、path、sha256、start、end、text、kind；候选的render_asset_sha256必须引用同一PNG。现有1080×1920 Harness使用RGBA画布，card必须output_pixels。重建卡片的真实alpha边界也必须符合原bbox；保留原卡片时alpha只能落在原文字范围±4px。
- `render_mask_indices`必须覆盖全部旧source_text_masks，实际坐标与mask_bbox一致。否则拒绝，不能静默回退编号大框。
- 缺分类、预览或renderer绑定时返回REVIEW_REQUIRED；历史文件不删除，旧结果不自动重剪。

## 无UI入口

```sh
.venv/bin/python scripts/headless_overlay_policy.py route --plan /absolute/edit_plan.json
.venv/bin/python scripts/headless_overlay_policy.py propainter-preview --plan /absolute/edit_plan.json --region-id hook --start 0 --end 3 --out /absolute/work/overlays/hook/propainter_preview.mp4
.venv/bin/python scripts/headless_overlay_policy.py lock --plan /absolute/edit_plan.json --out /absolute/locked_plan.json
.venv/bin/python scripts/headless_overlay_policy.py check --plan /absolute/locked_plan.json
.venv/bin/python scripts/headless_overlay_policy.py final-qa --plan /absolute/locked_plan.json --review /absolute/visual_review.json
```

route/check/lock不运行模型；只有显式propainter-preview提交本地模型调用。返回建议不等于预览已完成。模型失败保留原片/研究证据，REVIEW_REQUIRED。旧`headless_overlay.py`保留为底层兼容入口；后续默认通过本标准入口，不绕过mask审核。

## 最终QA

技术QA之后仍需实际视觉QA，不能自动宣布业务PASS。抽0/10/20/30/45/60/75/88/96%；每个Overlay再看出现前、初期、中间、结束前，检查残留、拖影、白边、色差、尺寸/布局、人物/产品损伤与连续播放闪烁。

final review绑定最终MP4 SHA；保存sample_times、overlay_sample_times（含全局+局部采样）、overlay_regions_checked、temporal_playback_checked、reviewer、visual_pass、issues。技术成功未完成实际看片时保持REVIEW_REQUIRED。

QA：OVERLAY_AREA_MISMATCH / OVERLAY_BOUNDARY_MISMATCH / GENERIC_LARGE_COVER_USED / TEMPORAL_INPAINT_ARTIFACT / BACKGROUND_BLUR / TEXT_GHOSTING / TEXT_RESIDUE / CARD_STYLE_MISMATCH / PROPAINTER_MASK_TOO_LARGE / PROPAINTER_SUBJECT_DAMAGE / MOVING_MASK_REQUIRED。任何Generic Large Cover直接FAIL。审美类问题来自实际视觉记录，不能用几何程序冒充检测模型。

一次性计划、脚本去重、ASR/TTS缓存、局部Preview、有限FFmpeg并发继续保留；视觉质量高于PASS数量和处理速度。
