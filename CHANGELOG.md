# Changelog

## Unreleased

- Direct Google Calendar and Microsoft Graph busy-only mirroring, with explicit one-way or reciprocal direction and private ownership metadata.
- Guided terminal setup, interactive authorization, read-only connection checks, manual/dry-run synchronization and opt-in user-crontab controls.
- Private local credential/state storage, durable reconciliation references and exclusion of owned copies from reverse sync.
- Locally buildable Debian binary/source packages and an initial Ubuntu 26.04 source candidate (`0.1.0+resolute1`). The Launchpad archive exists; package publication and PPA install verification remain pending.
- Suggest Google Cloud CLI and Azure CLI as optional Debian setup aids, and recommend Python virtual-environment support for temporary Azure installation; normal authorization and sync remain independent of both CLIs.
- Offer just-in-time registration assistance in interactive setup: reuse installed CLIs, confirm temporary downloads when missing, and remove setup-owned tooling and administrative login data after use. Preserve application credentials, external registrations and global installations.
- Filter copies by configurable local workdays/hours and independent work-window margins; keep full eligible intervals rather than clipping them.
- Add separate event lead/trail defaults in each direction and private per-occurrence travel overrides, without changing originals.
- Keep only fixed origin notes in Busy copies; refuse guest-bearing copy updates/deletions and migrate existing copy content once without duplicate creation.
- Preserve Outlook ownership markers in calendar-range reads so existing Busy copies are not duplicated or mirrored back when Graph suppresses selected extended properties.
- Default new configurations to seven days back and 60 days ahead; expose both day counts during setup and retain existing explicit horizon choices.
- License the project under MIT, including Python package metadata and license notices in source, wheel and Debian artifacts.
- Explain read-only connectivity success separately for Google and Outlook and label 24-hour event counts in `doctor` and setup, including that zero events is not a connection failure.
- Preserve managed schedules across upgrades, load updated code on the next execution, and remove only the Debian package's managed system-Python cron job during uninstall while retaining unrelated jobs and user data.
- Derive Debian source staging names from the package changelog version and document explicit signing/upload without reusable Launchpad account authorization.
