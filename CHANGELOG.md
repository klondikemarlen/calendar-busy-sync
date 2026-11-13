# Changelog

## Unreleased

### Fixed

- Make desktop failure notices easier to scan with labeled sections and commands on separate lines; keep full authorization recovery instructions in `status`.

## [1.1.0](https://github.com/klondikemarlen/calendar-busy-sync/releases/tag/v1.1.0) - 2026-11-13

### Added

- Publish a free GitHub Pages homepage and privacy policy describing local calendar processing, provider transfers, storage, and deletion controls.
- Document checksum-verified wheel installation, private backup/restore, and offline dependency preservation so reinstalling does not depend solely on the Ubuntu PPA or discard existing Busy-copy ownership.
- Preserve the original logo PNGs and an editable SVG in `design/` for reuse at other sizes.
- Report failed syncs through private desktop notifications with daily reminders and a recovery notice, and persist sync health plus Google/Outlook recovery commands in `status`. Existing managed schedules gain this behavior after upgrade without cron email setup.

### Changed

- Route website support and privacy requests through the dedicated support group instead of publishing the maintainer's personal email address.

### Fixed

- Suppress the redundant GTK `atk-bridge` module request only while opening Google's authorization browser; retain other GTK modules and diagnostics.

### Known Limitations

- Coworker Scheduling Assistant visibility remains unverified and accepted by the maintainer; issue #1 remains open. No new live-calendar checks were performed for this release.
- Desktop alerts require an available graphical notification service; persistent sync health and recovery commands remain available through `status`.

## [1.0.0](https://github.com/klondikemarlen/calendar-busy-sync/releases/tag/v1.0.0) - 2026-11-06

### Changed

- Mark the existing busy-only synchronization workflow as the first stable release; preserve saved configuration, ownership state and managed schedules without migration.
- Replace pre-publication setup guidance with PPA installation and upgrade instructions.

### Fixed

- Include the upstream changelog in Debian source archives and installed packages.

### Known Limitations

- Coworker Scheduling Assistant visibility has not been independently observed. The maintainer accepted this release limitation; issue #1 remains open. Live disposable-event lifecycle checks passed separately in both directions.
- Scheduling requires the computer to be awake and online. OAuth consent and tenant policy remain account-specific; installation does not authorize accounts or enable jobs.

## 0.1.0 - 2026-11-05

### Added

- Direct Google Calendar and Microsoft Graph busy-only mirroring, with explicit one-way or reciprocal direction and private ownership metadata.
- Guided terminal setup, interactive authorization, read-only connection checks, manual/dry-run synchronization and opt-in user-crontab controls.
- Private local credential/state storage, durable reconciliation references and exclusion of owned copies from reverse sync.
- Locally buildable Debian binary/source packages and an Ubuntu 26.04 PPA package (`0.1.0+resolute1`), published on November 5. Signed-PPA installation and installed setup/sync/scheduling checks were verified on November 6.
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
