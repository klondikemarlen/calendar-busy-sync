# Local Packages and Launchpad Publication

A Debian package separates installation from personal setup and makes upgrades/uninstallation manageable. A PPA would be a convenient APT distribution channel, not a way to avoid OAuth application registration or employer approval.

## Build Without Root

```sh
make deb
make source
```

These use `dpkg-buildpackage` in an isolated staging directory and copy the resulting `.deb`, `.dsc`, source archive, and build metadata into `dist/`. Build tools are `dpkg-dev`, `make`, and Python 3.11+. Debian/Ubuntu runtime dependencies are declared in `debian/control`; package installation performs no pip downloads, account authorization, calendar operations, or crontab edits. No maintainer script starts background synchronization. Upgrades preserve existing managed schedules; the next execution loads the upgraded code. On removal, an isolated, standard-library-only `prerm` removes only well-formed managed jobs using `/usr/bin/python3 -m calendar_busy_sync sync` across local accounts. Other interpreter jobs and unrelated cron entries are preserved. Cleanup ambiguity or failure aborts removal; credentials, configuration, state, and calendar events are retained.

The Python package can also be built with `make build` after `make dev`. Install a built wheel into a durable private virtual environment when system-wide package installation is not desired. Do not schedule a temporary build/QA environment.

## PPA Decision

The public [Calendar Busy Sync PPA](https://launchpad.net/~klondikemarlen/+archive/ubuntu/calendar-busy-sync) exists, but **no package has been published or installed from it yet**. The initial Ubuntu source candidate is `0.1.0+resolute1` for Ubuntu 26.04 (`resolute`); the upstream Python version remains `0.1.0`. The Ubuntu suffix lets APT upgrade a previously installed local `0.1.0` package. A prepared archive is not a published release.

Before a public PPA:

1. The copyright owner has selected the [MIT license](../LICENSE). Preserve its copyright and permission notice in redistributed artifacts; Debian records the same license as `Expat` in `debian/copyright`.
2. Record real Google/Microsoft authorization, busy-only lifecycle, and coworker Scheduling Assistant acceptance outcomes in [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1). An unperformed account-dependent check remains unverified and requires explicit maintainer acceptance before release; do not report it as passed or close issue #1 while its required observations remain missing. A package-build PASS is not live-calendar proof.
3. Configure an owned Launchpad account/PPA and its signing key, select the Ubuntu series, and verify that the declared Python dependencies exist for it.
4. Build and sign that exact source version. Signing and uploading are explicit maintainer actions; never retrigger an opaque signing prompt or claim upload success from an unsigned local build.
5. Observe Launchpad's source acceptance and successful builds. Install the resulting version from the actual PPA on the target Ubuntu release, check its installed version, and exercise setup/sync/scheduling before documenting `add-apt-repository` commands.

Until those conditions are satisfied, use a local `.deb` or a locally installed wheel. There is no paid sync service or public calendar feed in either path.

## Explicit Signing and Upload

Create or edit the PPA through Launchpad's website. Reading public archive/build metadata requires no account grant; signing and uploading a source package do not require a reusable Launchpad API token. Do not authorize desktop-wide or account-wide API access merely to simplify publication.

After release acceptance and merging the reviewed source, run `make source`. Inspect the generated `.dsc` and `_source.changes` in `dist/`: their version must match the intended upload and their distribution must be `resolute`. The staging directory name follows the Debian changelog version. `make source` deliberately leaves artifacts unsigned.

Sign that exact source version explicitly with a locally held key whose public key is registered to the Launchpad uploader account. For the initial candidate, from the repository root:

```sh
debsign -kYOUR_REGISTERED_SIGNING_KEY dist/calendar-busy-sync_0.1.0+resolute1_source.changes
dscverify --keyring "$HOME/.gnupg/pubring.kbx" dist/calendar-busy-sync_0.1.0+resolute1_source.changes
```

Replace `YOUR_REGISTERED_SIGNING_KEY` with the intended signing-key fingerprint, and select the uploader's actual public keyring if it differs from the GnuPG keybox path shown. Signing may open a GPG passphrase prompt; it is not `sudo`, does not upload anything, and does not grant API access. Do not enter a passphrase in chat. Verification must succeed against the signer's trusted local public keyring before upload.

Upload only after checking the signed version and target archive:

```sh
dput ppa:klondikemarlen/calendar-busy-sync dist/calendar-busy-sync_0.1.0+resolute1_source.changes
```

Launchpad accepts source uploads, not the locally built binary `.deb`. Successful transfer alone does not prove acceptance or a successful build. Observe the exact source version and build result on Launchpad, then verify installation of that version from the PPA in an isolated Ubuntu 26.04 environment before adding public installation instructions. Existing user configuration, credentials and scheduling remain separate from package publication.

