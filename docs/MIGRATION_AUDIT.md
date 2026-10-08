# Company Mac migration audit — 2026-10-08

Base accepted product: Creative Factory 0.1.17 / 20261008.145239 / schema 3.

The previous build was not portable: stage_engine.py read the developer's mother checkout,
copy_runtime.py read two private virtual environments, bundle_media.py read Homebrew and a
private whisper.cpp build, and build.py required the mother's absolute path and developer
Downloads directory. Copying Packaging alone would therefore omit the mature Engine and runtime.

The new repository retains the complete 25-module headless import closure, mature renderer,
DeepSeek prompts and presets, plus every existing Packaging adapter. `engine-snapshot.json`
records the mother commit and each unmodified source SHA256. One nonruntime performance
benchmark containing historical personal-job paths is deliberately excluded. No mother files
are changed. Snapshot refresh is explicit; builds never look at the mother checkout.

Large runtime: pinned Git LFS archive, approximately 185 MB; no venv, paid-provider credentials,
production database, source video, outputs, or ProPainter weights. The archive contains the
accepted standalone Python 3.12.14, installed package distributions and their notices,
relocated FFmpeg/ffprobe + dylibs, whisper.cpp executable and MIT-licensed base model.
Every file has a SHA256 or symlink target in runtime-lock.json. Company authentication is
repository SSH → Git LFS delegated download authorization, never a personal HTTP token or
private Release URL. This authentication path still requires company-Mac verification.

Mathilde: user asked to skip distribution. It is absent from Git and the runtime archive.
No fallback font is substituted; full build requires the separately licensed matching font.
Poppins: SIL OFL, notice included. ProPainter: S-Lab noncommercial license; source/weights/
torch are excluded from the company runtime. Existing production scope restrictions remain.
Core local overlay/white-cover strategies are unchanged. FFmpeg/library notices and exact
source URLs, checksums, and build formulas are retained under licenses/media and runtime-lock.
The App is ad-hoc signed, not Apple Developer ID notarized; no Gatekeeper bypass is scripted.

AI architecture unchanged: Creative Factory schedules and renders locally. CodexDriver is
optional CLI-based; OpenAIDriver uses the actual Responses API with image bytes and strict
schema, model gpt-4.1 by default. AUTO chooses an available Codex then configured OpenAI;
without either it raises AI_DRIVER_UNAVAILABLE. A configured key is not proof of live provider
readiness. Company users must choose OpenAI in Settings and enter their own OpenAI, DeepSeek,
and ElevenLabs keys. Provider readiness/vision/quality remain unverified until a real company
run; none of these are inferred from an App opening or an import succeeding.

Installation/update operate only on com.autocut.desktop and AutoCut data. No Order Pulse
ports, SSH files, projects, browser spaces, or downloads are altered. A separate SSH -F config
provides the Creative Factory alias. Updates reject active jobs/items, validate resources and
signature, test an isolated backend + short encode/decode, request a normal app quit, acquire
the backend lock, back up the DB via SQLite backup, and retain the old App. No production
worker is killed. Schema 3 only; untested migrations and incompatible rollback are rejected.

Company production approval is currently false. Main source commits are not automatically
installed; the updater follows only an approved version tag. Real company install, provider
E2E, quality comparison and update/rollback drill are still required before promotion.
