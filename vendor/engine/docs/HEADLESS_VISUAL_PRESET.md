# TikTok BR Transport Visual Preset V1

Default candidate for **new Headless Transport plans**: `presets/tiktok_br_transport_v1.json`.
ID `TIKTOK_BR_TRANSPORT_VISUAL_V1`; user visual confirmation is still pending. Do not claim frozen until the user approves the preview. Existing plans, task styles, UI presets and rendered videos are not migrated.

At 1080×1920:

| Layer | Font / width | Center | Appearance | Time |
|---|---|---|---|---|
| New Hook | Mathilde Black Italic, 76px (68–84) | 50%, 17% | white, black 6px stroke, ≤2 lines, 0.94× line advance, width ≤88% | first 5s |
| Captions | Poppins Bold, 52px (48–56) | 50%, 78% | white, black 5px stroke, ≤2 lines, 1× line advance, width ≤76%, no box | alignment-derived |
| CTA | user's current red `箭头.gif`, red body width 16% | 15%, 65% | fixed X, vertical bounce ±2.5% H / 0.8s | last 5s |

The new GIF is persisted through the existing CTA library, original bytes unchanged. Its ID/hash are pinned in the preset. No deleted asset or previous hand GIF is used. Frame delays/alpha/artwork are preserved. Transparent margins and baked-in frame translations are normalized; all frames keep a common scale. After the user requested reference-size enlargement, 16% measures the **red arrow body**, not the intermittent side flashes. One common scale preserves all original artwork/frame delays. Encoded body is about173×255px, close to the reference165×243px. Because this GIF's flashes extend beyond its body, the pinned asset has an explicit reasoned layout override to16.6%,62% to stay inside the canvas and clear of captions; the base preset remains15%,65%. Real per-frame alpha intersection validates collisions. No per-frame zoom or horizontal movement.

The renderer explicitly opens the font files with Pillow/FreeType and overlays those raster pixels with FFmpeg. No system font lookup or fallback is used. Mathilde SFNT name IDs 1/2/4/6 are Mathilde Black / Italic / Mathilde Black Italic / Mathilde-BlackItalic; FreeType's typographic name IDs 16/17 are Mathilde / Black Italic. Both identify the same supplied file. Metadata and original SHA256 are under `assets/fonts/`. Poppins stays in the existing local font directory.

## Thin renderer contract

1. New task code calls `app.s3.visual_preset.build_visual_plan(duration, hook, captions)` and saves the returned snapshot **inside its EditPlan**. Omit asset to use the pinned, current, hash-validated CTA. Existing original title/card replacements remain governed by `HEADLESS_OVERLAY_STANDARD.md`; suppress the additional Hook where the original localized Hook already fills that role.
2. Captions retain actual alignment-derived timestamps and original punctuated text. `caption_events(alignment)` can group 4–8 words using punctuation/gaps before removing display punctuation. Excessively long unbreakable text fails `SUBTITLE_SPLIT_REQUIRED`; long Hooks fail `TITLE_SHORTEN_REQUIRED`, never shrink indefinitely. No TTS/alignment is rewritten.
3. `visual_inputs(saved_plan, layer_directory, data_root, base_label, first_index)` produces reusable local raster inputs, overlay filter clauses and measured evidence. Append these to the existing caller's FFmpeg command; map `[visual_out]` and the caller's unchanged audio. This is the formal headless drawing implementation, also used by `scripts/headless_visual_preview.py`. It does not invoke a second full render or select footage.
4. Canvas proportions are validated. Font/stroke/line advance scale by `min(W/1080,H/1920)`; anchors and CTA dimensions scale against actual W/H. Text positions use actual alpha bounds, not font baselines.
5. Keep plan, layers/font hashes, measured bounds and QA. Missing/changed/misidentified fonts or missing glyphs fail `FONT_LOAD_FAILED`. No fallback. CTA lookup/hash failure stops instead of silently using another asset.

Position overrides require `visual_preset_override=true` and a per-layer record containing `override_reason`, `default_position`, `chosen_position`. Accepted reasons: PRODUCT_OCCLUSION, FACE_OCCLUSION, SOURCE_TEXT_CONFLICT, CTA_SUBTITLE_COLLISION, OTHER. Bounds: title Y12–25%, subtitle Y68–82%, CTA X12–22% or78–88%, Y58–72%. Position exemptions do not exempt fonts, sizes, timing or animation. Right CTA is an explicit reasoned override to85%,65%; no tracking.

Technical checks: TITLE_PRESET_DEVIATION, SUBTITLE_PRESET_DEVIATION, CTA_PRESET_DEVIATION, SUBTITLE_OUT_OF_RANGE, CTA_HORIZONTAL_DRIFT, TEXT_OUT_OF_CANVAS, CTA_OUT_OF_CANVAS and layer collisions. Invalid settings fail before final encoding. Technical PASS does not certify product/face occlusion or replace actual frame review and user confirmation.

## Preview

`artifacts/visual-preset-v1/preview/reference_preview.mp4` is a six-second silent style preview from an existing 9:16 source, without ASR/TTS or commercial batch rendering. Hook copy avoids inventing a promotional percentage. Title0–5s, CTA1–6s. `reference_frame.png` is at3s. The enlarged red arrow preview awaits user visual confirmation; its original side flashes are preserved. The source container's `timescale not set` warning is retained in the log; output FPS/PTS/decode are checked independently. No font warning occurred.
