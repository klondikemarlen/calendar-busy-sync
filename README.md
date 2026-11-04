# calendar-busy-sync

Project goal: mirror unavailable time between Google Calendar and Outlook as anonymous Busy appointments, using a local command-line workflow rather than a paid service or calendar feed.

Development is tracked in [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1). Real-account integration and release verification are not complete. See [local setup and operation](docs/usage.md), [Debian packages and PPA prerequisites](docs/packaging.md), and [project guidance](AGENTS.md).

## Automated Checks

The [CI workflow](.github/workflows/ci.yml) supplies the `ci` check for pull requests and pushes to `main`. When a Python project is present, it runs `make dev`, `make check`, and `make build`, then installs the wheel in a separate virtual environment and checks CLI help/version. It uses synthetic tests, not real calendar credentials or events.

Revisions without a Python project report those checks as not applicable; tracked Python source without `pyproject.toml` fails. A passing CI run does not replace the live-account acceptance checks in issue #1.
