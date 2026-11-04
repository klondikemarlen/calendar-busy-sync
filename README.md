# calendar-busy-sync

Planned local, privacy-preserving busy-only synchronization between Google Calendar and Outlook, intended to run on demand or from cron.

No sync implementation or runnable command exists yet. Requirements and acceptance criteria are tracked in [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1).

## Automated Checks

The [CI workflow](.github/workflows/ci.yml) supplies the `ci` check for pull requests and pushes to `main`. When a Python project is present, it runs `make dev`, `make check`, and `make build`, then installs the wheel in a separate virtual environment and checks CLI help/version. It uses synthetic tests, not real calendar credentials or events.

Revisions without a Python project report those checks as not applicable; tracked Python source without `pyproject.toml` fails. A passing CI run does not replace the live-account acceptance checks in issue #1.
