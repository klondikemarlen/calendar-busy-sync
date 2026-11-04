# Project Guidance

## Shared Base

Apply the task-relevant workflows and rules from [marlens-skills-rules-and-tools](https://github.com/klondikemarlen/marlens-skills-rules-and-tools). In OMP, load the direct packaged workflow, such as `skill://feature/workflow.md`, `skill://code-review/workflow.md`, or `skill://readable-code/workflow.md`; other agents can read the corresponding `docs/workflows/` source in that repository. Do not copy the shared rule catalog into this project. This local overlay takes precedence for project-specific details.

## Local Overlay

- This project owns a local Python CLI, not a hosted service. Keep calendar adapters, reconciliation/state, and operator/scheduling concerns separate without a framework.
- Use `make dev` for the isolated development environment. `make lint` covers application, tests, and package-build Python; `make format-check` and `make test` are separate checks. `make check` runs all three. There is no Docker or database-service wrapper.
- Never use real accounts, OAuth credentials, calendar feeds, the user's crontab, or real event data in automated tests. Scope any explicit live smoke check to disposable generic events, preserving originals and unrelated calendar entries.
- Persist credentials and state outside the checkout. Only modify events whose private ownership metadata matches. Incomplete reads must never drive deletion.
- Keep install and scheduling separate: package installation must not authorize accounts, sync calendars, or enable jobs. Scheduling is opt-in after a successful current-configuration sync.
- Do not close issue #1 or claim live integration until its account-dependent checks have actually run. Do not claim a PPA, publish, install, or release without observed evidence. Public PPA redistribution requires the owner's license decision.
