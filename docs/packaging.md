# Local Packages and Launchpad Publication

A Debian package separates installation from personal setup and makes upgrades/uninstallation manageable. The PPA provides an APT distribution channel, not a way to avoid OAuth application registration or employer approval.

## Build Without Root

```sh
make deb
make source
```

These use `dpkg-buildpackage` in an isolated staging directory and copy the resulting `.deb`, `.dsc`, source archive, and build metadata into `dist/`. Build tools are `dpkg-dev`, `make`, and Python 3.11+. Debian/Ubuntu runtime dependencies are declared in `debian/control`; package installation performs no pip downloads, account authorization, calendar operations, or crontab edits. No maintainer script starts background synchronization. Upgrades preserve existing managed schedules; the next execution loads the upgraded code. On removal, an isolated, standard-library-only `prerm` removes only well-formed managed jobs using `/usr/bin/python3 -m calendar_busy_sync sync` across local accounts. Other interpreter jobs and unrelated cron entries are preserved. Cleanup ambiguity or failure aborts removal; credentials, configuration, state, and calendar events are retained.

The Python package can also be built with `make build` after `make dev`. Install a built wheel into a durable private virtual environment when system-wide package installation is not desired. Do not schedule a temporary build/QA environment.

## Install from the PPA

The public [Calendar Busy Sync PPA](https://launchpad.net/~klondikemarlen/+archive/ubuntu/calendar-busy-sync) supports Ubuntu 26.04 (`resolute`). Its initial `0.1.0+resolute1` package passed signed-APT installation and installed setup/sync/scheduling checks. GitHub releases and PPA publication are separate: inspect the available APT candidate rather than assuming a release tag is already installable from the PPA.

```sh
sudo add-apt-repository ppa:klondikemarlen/calendar-busy-sync
sudo apt update
apt-cache policy calendar-busy-sync
sudo apt install calendar-busy-sync
dpkg-query -W -f='${Version}\n' calendar-busy-sync
calendar-busy-sync --version
```

The Debian package version includes an Ubuntu suffix, such as `1.0.0+resolute1`; the CLI reports the upstream version, such as `1.0.0`. Existing local packages can be upgraded through the same APT source.

```sh
sudo apt update
sudo apt install --only-upgrade calendar-busy-sync
calendar-busy-sync status
calendar-busy-sync schedule status
```

An upgrade preserves configuration, credentials, ownership state and the managed schedule. The next scheduled execution loads the new code; an already-running process may finish with the old code. Do not repeat authorization or setup merely to upgrade. For a new installation, follow [local setup and operation](usage.md); account authorization, first synchronization and scheduling are separate opt-in steps.

## Install a Released Wheel Without the PPA

This is an alternative Linux installation, not a claim of native Windows or macOS support. Version 1.0 uses Unix file locking and permissions, and its scheduler requires `crontab`. Ubuntu 26.04 is the verified package platform; other Linux distributions need their own Python 3.11+ interpreter, virtual-environment support, timezone database, and cron for optional scheduling. The `py3-none-any` wheel filename describes packaging, not operating-system compatibility.

Start with the [published releases](https://github.com/klondikemarlen/calendar-busy-sync/releases). The following selects the existing **v1.0.0** release explicitly; when choosing another release, use its actual asset names and checksum manifest. Run this in a terminal, outside a Git checkout:

```sh
(
  set -eu
  umask 077
  download="$HOME/Downloads/calendar-busy-sync-v1.0.0"
  mkdir -p "$download"
  cd "$download"
  release="https://github.com/klondikemarlen/calendar-busy-sync/releases/download/v1.0.0"
  curl --fail --location --output calendar_busy_sync-1.0.0-py3-none-any.whl \
    "$release/calendar_busy_sync-1.0.0-py3-none-any.whl"
  curl --fail --location --output SHA256SUMS-v1.0.0 \
    "$release/SHA256SUMS-v1.0.0"
  sha256sum --check --ignore-missing SHA256SUMS-v1.0.0
  python3 -m venv "$HOME/.local/share/calendar-busy-sync/venv"
  "$HOME/.local/share/calendar-busy-sync/venv/bin/python" -m pip install \
    ./calendar_busy_sync-1.0.0-py3-none-any.whl
  "$HOME/.local/share/calendar-busy-sync/venv/bin/calendar-busy-sync" --version
)
```

Prerequisites for these download commands are `curl` and GNU `sha256sum`. Stop if a command fails; do not bypass a checksum mismatch. The checksum detects damaged or mismatched downloads, not compromise of the release account. Dependency installation normally needs internet access. Do not use `sudo pip` or copy an old virtual environment to a new machine: rebuild it at its final, durable location.

Use the full `~/.local/share/calendar-busy-sync/venv/bin/calendar-busy-sync` path for subsequent commands, or deliberately add that `bin` directory to your shell's `PATH`. If a system package is also installed, do not accidentally invoke its CLI when rebuilding the wheel installation's schedule. Installation does not authorize accounts or enable a job. A new user continues with [setup](usage.md#guided-setup); an existing user should [restore their backup](usage.md#backup-and-reinstall) **before** running setup.

### Keep a Reinstall Kit

Keep the downloaded wheel, checksum manifest, release source archive, these instructions, and the output of the following beside your private backup. The release files are public; the [configuration and token backup](usage.md#backup-and-reinstall) is not.

```sh
"$HOME/.local/share/calendar-busy-sync/venv/bin/python" --version
"$HOME/.local/share/calendar-busy-sync/venv/bin/python" -m pip freeze
```

For an offline reinstall on the **same OS, architecture, and Python version**, save the freeze output to `requirements.txt` and download its dependencies while they are available:

```sh
(
  set -eu
  cd "$HOME/Downloads/calendar-busy-sync-v1.0.0"
  "$HOME/.local/share/calendar-busy-sync/venv/bin/python" -m pip freeze > requirements.txt
  "$HOME/.local/share/calendar-busy-sync/venv/bin/python" -m pip download \
    --only-binary=:all: --dest wheelhouse --requirement requirements.txt
)
```

Keep `wheelhouse/` with the kit. On that matching platform, create a fresh virtual environment and install with `python -m pip install --no-index --find-links /path/to/kit/wheelhouse calendar-busy-sync==1.0.0` using that environment's Python. If the download cannot supply a wheel for a dependency, the offline kit is incomplete; do not treat the application's wheel alone as a dependency backup. For a different platform or Python version, resolve compatible dependencies online instead.

No archived package guarantees Google/Microsoft API compatibility or valid authorization five years later. Keep control of the original cloud projects, account recovery methods, and support email; record the project/application IDs and selected calendars privately. Reauthorize when necessary rather than assuming saved refresh tokens remain valid indefinitely.

## Release Requirements

Before publishing a new version:

1. The copyright owner has selected the [MIT license](../LICENSE). Preserve its copyright and permission notice in redistributed artifacts; Debian records the same license as `Expat` in `debian/copyright`.
2. Record real Google/Microsoft authorization, busy-only lifecycle, and coworker Scheduling Assistant acceptance outcomes in [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1). An unperformed account-dependent check remains unverified and requires explicit maintainer acceptance before release; do not report it as passed or close issue #1 while its required observations remain missing. A package-build PASS is not live-calendar proof.
3. Configure an owned Launchpad account/PPA and its signing key, select the Ubuntu series, and verify that the declared Python dependencies exist for it.
4. Build and sign that exact source version. Signing and uploading are explicit maintainer actions; never retrigger an opaque signing prompt or claim upload success from an unsigned local build.
5. Observe Launchpad's source acceptance and successful build. Install that exact version from the PPA on the target Ubuntu release, verify the package and CLI versions, and exercise setup/sync/scheduling with isolated synthetic data. Record publication and install evidence in the owning release or issue, not as assumptions from a local build.

If a version is not yet published in the PPA, a local `.deb` or a privately installed wheel remains a separate installation path. There is no paid sync service or public calendar feed in either path.

## Explicit Signing and Upload

Create or edit the PPA through Launchpad's website. Reading public archive/build metadata requires no account grant; signing and uploading a source package do not require a reusable Launchpad API token. Do not authorize desktop-wide or account-wide API access merely to simplify publication.

After release acceptance and merging the reviewed source, run `make source`. Inspect the generated `.dsc` and `_source.changes` in `dist/`: their version must match the intended upload and their distribution must be `resolute`. The staging directory name follows the Debian changelog version. `make source` deliberately leaves artifacts unsigned.

Sign that exact source version explicitly with a locally held key whose public key is registered to the Launchpad uploader account. For version 1.1, from the repository root:

```sh
debsign -kYOUR_REGISTERED_SIGNING_KEY dist/calendar-busy-sync_1.1.0+resolute1_source.changes
dscverify --keyring "$HOME/.gnupg/pubring.kbx" dist/calendar-busy-sync_1.1.0+resolute1_source.changes
```

Replace `YOUR_REGISTERED_SIGNING_KEY` with the intended signing-key fingerprint, and select the uploader's actual public keyring if it differs from the GnuPG keybox path shown. Signing may open a GPG passphrase prompt; it is not `sudo`, does not upload anything, and does not grant API access. Do not enter a passphrase in chat. Verification must succeed against the signer's trusted local public keyring before upload.

Upload only after checking the signed version and target archive:

```sh
dput ppa:klondikemarlen/calendar-busy-sync dist/calendar-busy-sync_1.1.0+resolute1_source.changes
```

Launchpad accepts source uploads, not the locally built binary `.deb`. Successful transfer alone does not prove acceptance or a successful build. Observe the exact source version and build result on Launchpad, then verify installation of that version from the PPA in an isolated Ubuntu 26.04 environment before claiming it is available there. Publish the `v1.1.0` GitHub release against the reviewed merge commit with the corresponding wheel, source and Debian artifacts; verify downloaded release assets separately. Existing user configuration, credentials and scheduling remain separate from package publication.

