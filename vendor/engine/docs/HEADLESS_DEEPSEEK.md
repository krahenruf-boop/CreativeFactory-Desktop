# DeepSeek TikTok BR copy integration

New Headless batches use `headless_transport.py deepseek` / `deepseek_batch.py`: at most5 items per request and exactly3 verbatim A/B/C titles. See [HEADLESS_PERFORMANCE.md](HEADLESS_PERFORMANCE.md). The single-group entry below retains historical 10/5-title compatibility; it is no longer the default batch scheduler. JSON fences and trailing commas are handled locally in the shared adapter.

Status: integration acceptance; wait for the user's copy-quality confirmation before making this the new default for Transport work. No UI, ASR, TTS, overlay, font, CTA or video-rendering changes.

- Adapter: `app/s2/deepseek.py`, official HTTPS chat completions, model `deepseek-chat`, JSON object response, 90s timeout/10s connect. Existing S2/ElevenLabs providers remain unchanged.
- Prompt: `prompts/DEEPSEEK_TIKTOK_BR_V1.txt`; version **and actual prompt SHA** enter the fingerprint. Prompt edits require a version increment.
- Credentials: only `DEEPSEEK_API_KEY` from the invoking process environment. No key argument, `.env`, plaintext JSON, source or request logging. The existing Creative Factory credential file is unencrypted, so this integration deliberately does not write the supplied key there. The acceptance session receives it with hidden terminal input; after session exit it must be securely injected again for a future real request. No browser login fallback.
- Codex first performs exact/semantic script grouping, preserving meaningful facts/prices/specifications. `Context.dedupe_reviewed` must be true. Group IDs do not enter cache identity, so identical content/context reused by several output IDs produces one request. Genuine semantic equivalence and source understanding are Codex review tasks, not string-similarity assertions.
- `app/s3/deepseek_copy.py` owns request context, validation, durable cache and explicit Codex approval. It does not choose Voice, scene, speed, font, CTA, overlay method or footage. Missing provider returns DEEPSEEK_NOT_CONFIGURED; batch callers isolate REVIEW_REQUIRED per group and never silently switch models/providers.
- Cache: `<work>/scripts/deepseek/<fingerprint>.json`; includes provider/model/prompt/version/source/clean script/language/product/market/video context/selling points/allowed and blocked claims/promotion/target duration. Per-key filesystem lock prevents parallel paid duplicates. Attempts are written before sending. Success is cached before continuing; an interrupted/uncertain/failed attempt prevents an automatic resubmission on restart.
- A connection failure known to precede sending may retry once. Read/write timeouts and uncertain5xx do not retry. Invalid JSON/schema may request one correction. These separate counters are bounded; no arbitrary retries. Claim flags stop for review, not automatic rewrites. HTTP errors omit server bodies/headers from exceptions and artifacts.
- Validation checks JSON schema, pt-BR label/heuristic language checks, exactly the requested number of unique IDs/titles (default10), nonblank values, no Chinese/Markdown/hashtags/number prefixes, obvious unauthorized promotion and literal blocked phrases. These are screening checks, **not semantic or factual proof**. Codex must check all eight review criteria against the actual video and approved facts before producing `final_script.txt`.
- A task may explicitly request `title_count: 5` and a `title_brief`. The per-request count contract is appended to the unchanged V1 prompt and enters its actual SHA/fingerprint; existing default10 fingerprints remain unchanged. In a two-phase task, save recommendations as `AWAITING_USER_APPROVAL`, with no selected title or approval timestamp. Do not call `approve`, TTS, or rendering until the user confirms the titles.
- `approve(record, review, out)` requires the exact response hash, eight true content-review fields and a selected ID from the candidates with a reason. It never calls ElevenLabs. Candidate titles cannot silently be replaced with newly written titles. Only visual line breaks/punctuation adjustments are allowed downstream.
- `regenerate_titles` permits one title regeneration for an original response/video context, pins the existing Portuguese spoken script and only requests title JSON. Changed feedback does not bypass that limit. A genuinely different video context can request new titles without repeating localization.
- Keep the original punctuated final script for TTS and alignment; existing subtitle display cleaning remains separate. Read the existing visual preset for title fit; do not let DeepSeek choose typography or positions.

One-group invocation (credentials supplied securely by the caller):

```sh
.venv/bin/python scripts/headless_deepseek.py localize --execute \
  --context work/deepseek_test/input_context.json --work work --out work/deepseek_test
.venv/bin/python scripts/headless_deepseek.py review \
  --out work/deepseek_test --review work/deepseek_test/codex_review.json
```

`request_context.json`, `response.json`, `record.json`, `final_script.txt`, `titles.json` and provenance/review/report form the acceptance artifacts. Output content is untrusted until review. No credential belongs in any of these files.
