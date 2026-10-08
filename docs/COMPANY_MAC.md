# Company Mac installation, acceptance and updates

Target: macOS 27+, arm64, user jx. No Codex, personal GitHub token or GitHub web login is needed.

## First bootstrap

Copy `scripts/company_ssh_setup.sh` to the company Mac through an authorized file transfer,
then run `bash company_ssh_setup.sh`. It creates only the Creative Factory key and a separate
SSH configuration. Send only its `.pub` content to the repository administrator, who adds it
as a **read-only Deploy Key** on `krahenruf-boop/CreativeFactory-Desktop`. Never copy the private
key away. GitHub's Ed25519 host key is verified against the official SHA256 fingerprint.

Then copy/run `scripts/clone_company_mac.sh`. It clones into
`~/Projects/CreativeFactory-Desktop`, sets a repo-local `core.sshCommand`, installs the pinned
Git LFS 3.7.1 client under `~/.local/share/creativefactory-tools`, and fetches the runtime using
this repository's SSH authorization. No global Git config or Order Pulse identity is changed.

Equivalent clone command after key authorization:

```sh
GIT_LFS_SKIP_SMUDGE=1 GIT_SSH_COMMAND="ssh -F $HOME/.ssh/creativefactory_desktop.conf" \
  git clone git@github-creativefactory-desktop:krahenruf-boop/CreativeFactory-Desktop.git \
  "$HOME/Projects/CreativeFactory-Desktop"
cd "$HOME/Projects/CreativeFactory-Desktop"
git config core.sshCommand "ssh -F $HOME/.ssh/creativefactory_desktop.conf"
bash scripts/company_lfs_setup.sh
PATH="$HOME/.local/share/creativefactory-tools/git-lfs-3.7.1:$PATH" git lfs pull
bash scripts/setup_company_mac.sh
```

Xcode Command Line Tools must already be installed (`xcode-select --install` if absent;
user completes Apple's system installation/license UI). Setup does not upgrade global
Homebrew packages or an existing Python environment. The accepted runtime is extracted from
its hash-checked archive, not re-resolved to today's package versions.

## Font and first candidate

Mathilde is currently excluded by user request. No substitute is applied. An authorized copy
with SHA256 `3dbb44f22e330eac8349d30e7bdd951a20df53c4049a2a46630f03d0171e1255`
can later be supplied using `CF_LICENSED_FONT=/absolute/path/Mathilde-BlackItalic.ttf`.
It must remain outside Git. Do not mark the company build accepted without it (or a separately
approved and quality-validated font change).

Once all licensed resources are available, an explicit acceptance candidate may be built:

```sh
bash scripts/build_company_mac.sh --candidate
bash scripts/install_company_mac.sh --candidate
```

Installation is to `~/Applications/Creative Factory.app`. The script verifies hashes/signature,
imports the real Engine, starts the backend in an isolated temporary directory, performs a
short real FFmpeg encode/decode, checks idle state, normally quits Creative Factory, backs up
the existing database and App, then installs and opens the new App. Gatekeeper/system grants,
if required, must be completed by the user. No security protection is disabled by these scripts.

First launch: choose OpenAI API in Settings and enter **company-owned** OpenAI, DeepSeek and
ElevenLabs keys. Keys go only to macOS Keychain service `com.autocut.providers`. No personal
Mac DB, history, cookies, keys or downloaded media are migrated.

## Golden acceptance (not yet completed)

Use three authorized local short fixtures: 01 person + product, 02 product-only, 03 original
text/title. The smoke fixture validates encoding only; synthetic frames do not prove person
recognition. Run at least one real authorized video through the normal two batch confirmations.
Record requested/actual driver, model, vision_enabled, fallback_used/reason; verify person,
product, EditPlan, target-language localization, TTS, caption timing/placement, CTA in final
5 seconds, full decode and quality against the personal-Mac baseline. A missing vision driver
or silent degraded analysis must block acceptance. Store evidence outside Git (`build/`).

Restart the App and verify job history/output, Settings and Keychain remain usable. Then perform
a controlled accepted-version update and a same-schema rollback drill. Record actual results;
do not promote a release merely because mocks or App startup passed.

## Production publishing and update

After acceptance, maintainer updates AUTOCUT_VERSION, latest-build.json (portable version/
build metadata), release notes, Engine and runtime fingerprints, then commits and creates a
new immutable `vX.Y.Z` tag. The tagged `production.json` and main channel must both declare
`approved: true` and the tag. Never reset existing tags. Do not promote this migration yet:
its company acceptance remains pending.

Employees subsequently run:

```sh
cd "$HOME/Projects/CreativeFactory-Desktop"
bash scripts/update_company_mac.sh
```

The updater fetches tags/main, chooses only the explicitly accepted tag, builds in a detached
release worktree, verifies and smokes, then installs. It refuses downgrade, dirty tracked
source, active jobs/items and any unverified schema migration. The worker/database lock is
held during replacement. Data, output files, source folders, Settings and Keychain are retained.
No update script stops Order Pulse or touches its fixed port, SSH key or Ego Lite space.

The prior App and SQLite online backup remain under `~/Library/Application Support/AutoCut/updates/`.
A failed startup does not delete them. `bash scripts/rollback_company_mac.sh` checks that the
current DB schema equals the old App schema before restoring the App. It never overwrites a
new-schema database with old data automatically. Failed-version App is also retained.
