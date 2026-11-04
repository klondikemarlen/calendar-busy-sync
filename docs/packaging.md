# Local Packages and a Possible PPA

A Debian package separates installation from personal setup and makes upgrades/uninstallation manageable. A PPA would be a convenient APT distribution channel, not a way to avoid OAuth application registration or employer approval.

## Build Without Root

```sh
make deb
make source
```

These use `dpkg-buildpackage` in an isolated staging directory and copy the resulting `.deb`, `.dsc`, source archive, and build metadata into `dist/`. Build tools are `dpkg-dev`, `make`, and Python 3.11+. Debian/Ubuntu runtime dependencies are declared in `debian/control`; package installation performs no pip downloads, account authorization, calendar operations, or crontab edits. No maintainer script starts background synchronization. Upgrades preserve existing managed schedules; the next execution loads the upgraded code. On removal, an isolated, standard-library-only `prerm` removes only well-formed managed jobs using `/usr/bin/python3 -m calendar_busy_sync sync` across local accounts. Other interpreter jobs and unrelated cron entries are preserved. Cleanup ambiguity or failure aborts removal; credentials, configuration, state, and calendar events are retained.

The Python package can also be built with `make build` after `make dev`. Install a built wheel into a durable private virtual environment when system-wide package installation is not desired. Do not schedule a temporary build/QA environment.

## PPA Decision

The repository includes native Debian source packaging, but **no Launchpad PPA has been published or verified**. The source changelog currently targets `unstable`; choose and test a supported Ubuntu series before preparing an Ubuntu upload. Do not upload that source package unchanged and assume it is an Ubuntu release.

Before a public PPA:

1. The copyright owner has selected the [MIT license](../LICENSE). Preserve its copyright and permission notice in redistributed artifacts; Debian records the same license as `Expat` in `debian/copyright`.
2. Complete the real Google/Microsoft authorization, busy-only lifecycle, and coworker Scheduling Assistant acceptance checks. A package-build PASS is not live-calendar proof.
3. Configure an owned Launchpad account/PPA and its signing key, select the Ubuntu series, and verify that the declared Python dependencies exist for it.
4. Build and sign that exact source version. Signing and uploading are explicit maintainer actions; never retrigger an opaque signing prompt or claim upload success from an unsigned local build.
5. Observe Launchpad's source acceptance and successful builds. Install the resulting version from the actual PPA on the target Ubuntu release, check its installed version, and exercise setup/sync/scheduling before documenting `add-apt-repository` commands.

Until those conditions are satisfied, use a local `.deb` or a locally installed wheel. There is no paid sync service or public calendar feed in either path.
