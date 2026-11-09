# calendar-busy-sync

Mirror unavailable time between Google Calendar and Outlook as private, anonymous Busy appointments. Calendar Busy Sync is a local Python CLI: no hosted sync service or calendar feed is required. Google-to-Outlook is the default; reciprocal sync is explicitly opt-in.

Visit the [project homepage](https://klondikemarlen.github.io/calendar-busy-sync/) and read the [privacy policy](https://klondikemarlen.github.io/calendar-busy-sync/privacy/) before connecting accounts.

## Install and Use

On Ubuntu 26.04, install from the [project PPA](https://launchpad.net/~klondikemarlen/+archive/ubuntu/calendar-busy-sync) using the [installation and upgrade instructions](docs/packaging.md#install-from-the-ppa). Installation does not authorize accounts, synchronize events, or enable scheduling.

Run `calendar-busy-sync setup` in a terminal for guided configuration, then follow [local setup and operation](docs/usage.md). Scheduling is opt-in after a successful sync of the current configuration. The computer must be awake and online; Busy status does not prevent someone from sending a conflicting invitation.

See the [changelog](CHANGELOG.md) for release history and [project guidance](AGENTS.md) for contribution rules. Live disposable-event lifecycle checks passed in both directions. Coworker Scheduling Assistant visibility remains unverified and was accepted by the maintainer as a release limitation; [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1) remains open for that observation.

## Automated Checks

The [CI workflow](.github/workflows/ci.yml) supplies the `ci` check for pull requests and pushes to `main`. When a Python project is present, it runs `make dev`, `make check`, and `make build`, then installs the wheel in a separate virtual environment and checks CLI help/version. It uses synthetic tests, not real calendar credentials or events.

Revisions without a Python project report those checks as not applicable; tracked Python source without `pyproject.toml` fails. A passing CI run does not replace the live-account acceptance checks in issue #1.

## Website Publication

The public homepage and privacy policy are plain HTML/CSS in `site/`. Preview them with `python3 -m http.server 8765 --directory site`. The [Pages workflow](.github/workflows/pages.yml) deploys only that directory when it or the workflow changes on `main`; it can also be dispatched on `main`. GitHub Pages uses the free default project hostname, with no custom domain, JavaScript, analytics, or application backend. Website publication is independent of Python/PPA releases and does not establish Google OAuth verification.
