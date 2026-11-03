# Changelog

## Unreleased

- Direct Google Calendar and Microsoft Graph busy-only mirroring, with explicit one-way or reciprocal direction and private ownership metadata.
- Guided terminal setup, interactive authorization, read-only connection checks, manual/dry-run synchronization and opt-in user-crontab controls.
- Private local credential/state storage, durable reconciliation references and exclusion of owned copies from reverse sync.
- Locally buildable Debian binary/source packages. No public PPA is published; licensing, Launchpad configuration and real-account release verification remain prerequisites.
- Suggest Google Cloud CLI and Azure CLI as optional Debian setup aids, and recommend Python virtual-environment support for temporary Azure installation; normal authorization and sync remain independent of both CLIs.
- Offer just-in-time registration assistance in interactive setup: reuse installed CLIs, confirm temporary downloads when missing, and remove setup-owned tooling and administrative login data after use. Preserve application credentials, external registrations and global installations.
