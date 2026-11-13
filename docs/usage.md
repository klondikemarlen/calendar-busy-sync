# Local Setup and Operation

These procedures cover installed releases and development builds. Google and Microsoft authorization depends on the selected accounts and tenant policy. Automated fixtures do not prove provider consent or coworkers' free/busy visibility. The outstanding coworker observation is tracked in [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1); it remains unverified despite maintainer acceptance for release.

## Install and Inspect

On Ubuntu 26.04, follow [PPA installation](packaging.md#install-from-the-ppa). For a locally built Debian package, run `make deb`, then `sudo apt install ./dist/calendar-busy-sync_1.0.0+resolute1_all.deb`. APT resolves the Python authentication dependencies and cron. Installation does not change your calendars or crontab.

For development, run `make dev`, then `.venv/bin/calendar-busy-sync --help`. The runtime requires Python 3.11 or newer. `make lint`, `make format-check`, and `make test` are the supported checks. Development virtual environments/checkouts are not intended as a durable cron installation.

## Create Your Own OAuth Applications

No shared application credentials are bundled. Both registrations are free; your organization still controls whether its account can consent.

### Optional Registration Tools

The Debian package suggests `google-cloud-cli` (the `gcloud` command) and `azure-cli` (the `az` command), and recommends `python3-venv` for temporary Azure CLI installation. Normal authorization, sync and cron use Python APIs directly and do not need either CLI. APT normally installs the recommended virtual-environment support, but not the suggested cloud CLIs. Browser-based registration remains supported without them.

In `calendar-busy-sync setup`, leave the Google client path or Microsoft application ID blank to request guided registration. The wizard offers CLI assistance, reuses an executable already on `PATH`, and asks before downloading a missing tool into a private temporary directory. Declining leaves the manual credential/ID path available. No `sudo`, vendor-repository changes, shell-profile edits or permanent tool cache are involved.

Each provider helper uses an isolated administrative login/configuration directory even when reusing a global executable. Setup removes its downloaded tool files and temporary administrative login data when that provider's helper finishes, fails or is cancelled with Ctrl+C. Global executables and their existing configuration remain untouched. Final application credentials/state and provider-side registrations are not temporary and are preserved. A forced process kill or power failure can prevent normal temporary-file cleanup.

Temporary Azure CLI installation needs Python virtual-environment/pip support. If installing without recommended packages, install `python3-venv` yourself if missing or use the manual registration path. Users preferring system CLIs can follow the [Google Cloud CLI](https://docs.cloud.google.com/sdk/docs/install-sdk) or [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux?pivots=apt) installation instructions. Package installation itself does not download cloud CLIs, add repositories, sign in, register applications or enable jobs.

Google Workspace CLI (`gws`) is not a dependency. Its setup wizard is another optional helper, not needed for the procedure below. Administrative CLI sign-in and the calendar application's calendar-access consent are separate authorizations.

### Google

1. In [Google Cloud](https://console.cloud.google.com/), create/select a project and enable the Google Calendar API.
2. Configure the OAuth consent audience for your own use, then create an OAuth client with application type **Desktop app**. Download its JSON file to a local private location outside the checkout.
3. Setup uses a browser sign-in with a loopback callback and PKCE. The downloaded desktop-client secret is not a meaningful application-identity security boundary: installed apps cannot keep a shared secret.
4. Google-to-Outlook mode requests `calendar.readonly`. Reciprocal mode also requests `calendar.events`. No Gmail, Drive, Contacts or birth-date permissions are requested.

Google's consent-screen **Testing** status can cause refresh tokens for these scopes to expire after seven days. Review [Google's refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration); do not assume a scheduled sync will remain authorized indefinitely. A broader old Google grant must be revoked and authorized again before it can truthfully become read-only.

Read the project's [privacy policy](https://klondikemarlen.github.io/calendar-busy-sync/privacy/) before authorizing accounts. The [public homepage](https://klondikemarlen.github.io/calendar-busy-sync/) and policy describe the official CLI; linking them in Google Branding does not itself verify an OAuth project or change its publishing status. Personal-use verification exemptions and Google's Console validation are separate from free website hosting; consult [Google's exemption guidance](https://support.google.com/cloud/answer/13464323) for your intended audience.

For guided Google preparation, choose an existing project or confirm creation of a new one, then confirm enabling Calendar API. These are explicit durable Cloud changes; setup never links billing. The helper removes its temporary tooling/login data before handing off to the project-specific Console branding, audience and client pages.

Google's [documented Desktop client registration](https://developers.google.com/identity/protocols/oauth2/native-app#creatingcred) still requires the Console step. Complete it and download the JSON to a private location outside the checkout, then provide its path when the wizard resumes. Do not substitute `gcloud iam oauth-clients` (workforce identity federation) for the Calendar Desktop client.

After supplying the downloaded client JSON to `calendar-busy-sync setup`, `calendar-busy-sync auth google` opens the browser consent flow and stores refreshable credentials privately. No CLI administration token is reused as a calendar-access token. Refresh remains subject to the Testing-mode expiry and revocation rules above.

### Microsoft

1. Ask your organization's administrator whether a locally run public-client app may access your own calendar through Microsoft Graph. Register an application in [Microsoft Entra](https://entra.microsoft.com/) or obtain the approved application ID from your administrator.
2. Use a public-client application with **Allow public client flows** enabled for device sign-in. Do not create an application client secret.
3. Add the **delegated** Microsoft Graph `Calendars.ReadWrite` permission, not an application/directory-wide permission. Your tenant may require administrator consent or prohibit device sign-in.
4. Record the application/client ID and tenant ID/domain for the wizard. `organizations` is appropriate only when the registration supports that audience. MSAL also handles the standard sign-in/offline scopes needed for refresh.

The tool cannot bypass a tenant policy. If authorization is blocked, keep scheduling disabled and ask the administrator to review this exact delegated-calendar use case.

The guided Microsoft helper signs into your tenant using isolated Azure CLI configuration and separately asks before creating a single-tenant public-client registration named **Calendar Busy Sync**. It declares only delegated `Calendars.ReadWrite`, creates no secret, and grants no tenant-wide consent. The resulting application and tenant IDs feed the setup wizard; normal application device consent remains separate. If an external creation may have succeeded before a later failure, inspect the provider Console rather than blindly creating another registration.

## Guided Setup

Run `calendar-busy-sync setup` in a terminal. Supply an existing Google desktop-client JSON and approved Microsoft client ID/tenant, or leave the corresponding entry blank for the optional just-in-time registration guide described above. The wizard preserves the installation identity on reconfiguration. It separately offers application authorization, a read-only connectivity check, an initial sync, and finally scheduling; none is implied by installing the package or an administration helper. A corrupt existing configuration must be repaired without inventing a new installation identity.

Alternatively, after configuration:

```sh
calendar-busy-sync auth
calendar-busy-sync doctor
calendar-busy-sync sync --dry-run
calendar-busy-sync sync
calendar-busy-sync status
```

`doctor` prints separate Google and Outlook read-only success lines after both calendars have been read, followed by their event counts for the next 24 hours. These are counts, not error codes: zero means no events were returned in that window, not a connection failure. The probe does not use the full configured sync horizon and never writes calendar events. Setup uses the same report for its optional connectivity check. If either read fails, the command reports an error instead of success.

A dry run previews operation counts, not private details, and does not satisfy the scheduling gate. A manual sync prints created/updated/deleted/unchanged counts. Nonzero exit status means the run did not finish successfully; scheduled runs never open an interactive sign-in prompt. Reauthorize explicitly with `calendar-busy-sync auth google` or `calendar-busy-sync auth outlook` when instructed.

## Work Hours and Travel Time

During `setup`, choose **Configure work hours and event padding?** to set the work timezone, weekdays, local start/end, independent work-window margins, and event padding for each direction. Skipping this step preserves existing choices. Legacy configurations remain unfiltered with zero event padding until explicitly configured.

Work-window margins decide which appointments qualify; they do not extend an appointment. For example, 08:00–17:00 with 120 minutes on either side admits intervals overlapping 06:00–19:00 on the configured workdays. `Etc/GMT+7` means UTC-7 year-round; choose a regional IANA zone instead when the work schedule should follow daylight saving. Start/end use local wall time, with end later than start on the same day. Margins may cross midnight and remain anchored to the configured workday.

Event lead/trail time separately expands the copied Busy block using elapsed minutes. An appointment at 10:15–10:25 with 60 minutes before and after becomes 09:15–11:25 in the destination. Originals never change. A per-event override replaces both directional defaults for that exact provider event/expanded occurrence ID; it does not apply automatically to the entire recurring series or infer travel from a title or location.

For per-event overrides, disable the managed schedule before editing the private `config.json` under the configuration directory described below. Preserve its existing account fields and `installation_id`; merge these timing fields rather than replacing the whole file:

```json
{
  "work_hours_enabled": true,
  "work_timezone": "Etc/GMT+7",
  "work_days": ["mon", "tue", "wed", "thu", "fri"],
  "work_start": "08:00",
  "work_end": "17:00",
  "work_lead_minutes": 120,
  "work_trail_minutes": 120,
  "google_lead_minutes": 0,
  "google_trail_minutes": 0,
  "outlook_lead_minutes": 0,
  "outlook_trail_minutes": 0,
  "event_padding_overrides": [
    {
      "source": "google",
      "event_id": "synthetic-occurrence-id",
      "lead_minutes": 60,
      "trail_minutes": 60
    }
  ]
}
```

`source` is `google` for Google-to-Outlook or `outlook` for Outlook-to-Google. Use unique source/event-ID pairs. Every minutes field must be an integer from 0 through 1440, and workdays must be unique lowercase names from `mon` through `sun`. Keep real event IDs and all account configuration outside Git.

The complete buffered interval is copied when it overlaps both the sync horizon and an eligible padded work window; it is never clipped or split. Touching a boundary without any overlap does not qualify. All-day and multi-day events qualify if any permitted window overlaps. Reads extend beyond the nominal horizon to include events whose travel padding overlaps it. Run `sync --dry-run` before applying changes: events that no longer qualify have only their owned copies removed. A successful sync under the changed configuration is required before re-enabling scheduling.


## Optional Scheduling

After a successful sync using the current configuration:

```sh
calendar-busy-sync schedule enable --minutes 15
calendar-busy-sync schedule status
calendar-busy-sync schedule disable
```

The scheduler uses your user crontab, an absolute installed Python/application invocation, and explicit XDG storage roots. It changes only its marked block and refuses to overwrite a crontab that changed during the update. A manual run and a scheduled run cannot execute concurrently. Existing unrelated jobs remain untouched. Reconfiguring requires disabling the managed schedule first.

Package upgrades preserve the managed job and its interval. Each execution starts the installed Python module afresh, so the next run after an upgrade uses the new code; a run already in progress may finish using the old code. Reinstalling a package does not require re-enabling its existing schedule.

The computer must be awake, online, and running cron. Missed cron executions are not automatically replayed; run `calendar-busy-sync sync` when you need immediate reconciliation. Busy status informs scheduling but does not prevent someone from sending a conflicting invitation.

## Failure Notifications and Recovery

Real syncs, including existing cron jobs after an upgrade, record `sync_health` in `calendar-busy-sync status`: `outcome` (`unknown`, `failed`, or `ok`), `last_attempt`, `failure_since`, recovery instructions and notification delivery status. `last_success` remains the last completed real sync; an old success does not clear a newer failure. Legacy databases report unknown health until the next real sync. Dry runs, connectivity checks and lock contention do not change real-sync health. A configuration error is also shown by `status`; preserve existing files and follow its setup/repair instruction.

A failed sync sends a short, plain-text desktop notification with separate connection-check and recovery sections: `calendar-busy-sync doctor` and `calendar-busy-sync status` each appear on their own line. Full Google/Outlook authorization recovery instructions remain in `status`, not the banner. Notifications contain no event details, calendar identifiers or provider responses. While failures continue, successfully submitted reminders are limited to one per 24 hours across processes; the next successful sync attempts one recovery notice. Reauthorization alone does not mark the sync healthy. Open a terminal using the same XDG roots and installed executable as your schedule:

```sh
calendar-busy-sync status
calendar-busy-sync doctor
# Run only the affected account's command if authorization is rejected:
calendar-busy-sync auth google
calendar-busy-sync auth outlook
calendar-busy-sync sync
calendar-busy-sync status
```

Google Testing-mode token expiry may require publishing your personal OAuth project rather than repeatedly signing in; review the Google guidance above. Outlook tenant/consent restrictions may require administrator assistance. Do not delete configuration, tokens or ownership state to repair an expired grant. For network/service failures, restore connectivity and retry sync; authorization is not needed for every failure.

Desktop delivery requires `/usr/bin/notify-send` (`libnotify-bin`, recommended by the Debian package) and a logged-in Linux desktop providing notifications on your own `/run/user/UID/bus`. Cron discovers this same-user bus without requiring inherited desktop environment variables. Missing tools, logout, unavailable desktop service or notification timeouts never change the sync's failure exit code; status remains available, and failed delivery is retried on the next failed sync. A successful submission does not prove that desktop Do Not Disturb settings allowed a visible popup. A recovery notice unavailable at the recovery sync is not queued for later. Local state access failures can prevent health recording; preserve files and check permissions using the terminal diagnostic.

No email account or MTA is required or configured. Existing cron commands and their stdout/stderr diagnostics are preserved; any separately configured cron mail behavior remains unchanged. Notifications do not detect missed runs while the computer is asleep or cron is stopped, and `ok` is a historical result, not a live heartbeat.

## Backup and Reinstall

For an existing installation, restore its identity and ownership records rather than starting setup from scratch. These Linux steps use `flock` from util-linux and ordinary file copies; they do not require cloud administration tools. Keep only one computer syncing this installation: its lock coordinates local processes, not two machines.

### Save a Consistent Private Backup

Use the same XDG environment as your normal sync commands. If you configured custom storage roots, export those exact `XDG_CONFIG_HOME` and `XDG_STATE_HOME` values first. Record your schedule interval privately; the example below disables only this tool's managed job. It does not export or back up unrelated crontab entries.

Set `cbs` to the installed executable you actually use: `calendar-busy-sync` for the PPA, or the full virtual-environment path from the [wheel installation instructions](packaging.md#install-a-released-wheel-without-the-ppa).

```sh
(
  set -eu
  umask 077
  cbs=calendar-busy-sync
  config="${XDG_CONFIG_HOME:-$HOME/.config}/calendar-busy-sync"
  state="${XDG_STATE_HOME:-$HOME/.local/state}/calendar-busy-sync"
  backup="$HOME/calendar-busy-sync-backup-$(date +%Y%m%d-%H%M%S)"
  test -f "$config/config.json"
  test -f "$state/sync.sqlite3"
  "$cbs" schedule disable
  mkdir "$backup"
  flock --nonblock "$state/sync.lock" sh -c \
    'cp -a "$1" "$3/config" && cp -a "$2" "$3/state"' \
    sh "$config" "$state" "$backup"
  chmod -R go-rwx "$backup"
  "$cbs" --version > "$backup/version.txt"
  touch "$backup/COMPLETE"
  printf 'Completed private backup: %s\n' "$backup"
)
```

Do not run setup, authorization, or manual sync while backing up. Disabling scheduling does not terminate a running sync; if it or lock acquisition reports contention, let the current operation finish and retry with a new backup directory. Any failure leaves the backup incomplete: use only a directory with `COMPLETE`. The lock keeps configuration, token files, and SQLite ownership records from changing during the copy. Both directories are needed: `config/` includes `config.json` (with `installation_id`) and `google-client.json`; `state/` includes provider tokens and `sync.sqlite3`.

The backup contains credentials and private calendar identifiers. Owner-only permissions are **not encryption**. Store it on encrypted backup storage outside Git, restrict access, and never attach it to an issue. Keep a second protected copy off the machine before reinstalling. Save the [public reinstall kit](packaging.md#keep-a-reinstall-kit) too; a backup of a Python virtual environment is not a portable installer.

If you are only making a periodic backup, re-enable your recorded schedule after confirming the backup completed. For a migration, leave the old machine's job disabled and do not run manual sync there after cutover.

### Restore Before Setup or Scheduling

Install the recorded release first, using the [PPA or released wheel](packaging.md). On the new machine, select your intended XDG roots and restore **before** setup, authorization, or sync. The following deliberately refuses existing destination directories, including empty ones; do not delete existing data just to make it pass. If this is a package reinstall on the same machine and your files survived, skip copying and use those existing files instead.

Replace the backup path with your completed private backup. Run as your normal user, not root:

```sh
(
  set -eu
  umask 077
  backup="$HOME/calendar-busy-sync-backup-YYYYMMDD-HHMMSS"
  config="${XDG_CONFIG_HOME:-$HOME/.config}/calendar-busy-sync"
  state="${XDG_STATE_HOME:-$HOME/.local/state}/calendar-busy-sync"
  test -f "$backup/COMPLETE"
  test -f "$backup/config/config.json"
  test -f "$backup/state/sync.sqlite3"
  test ! -e "$config"
  test ! -L "$config"
  test ! -e "$state"
  test ! -L "$state"
  mkdir -p "$(dirname "$config")" "$(dirname "$state")"
  cp -a "$backup/config" "$config"
  cp -a "$backup/state" "$state"
  chmod -R go-rwx "$config" "$state"
)
```

Stop on any error and preserve both the backup and any partial destination for inspection; do not merge an incomplete restore into a different installation. Do not edit `installation_id` or discard `sync.sqlite3`. Restoring these records lets later runs update or remove the existing owned Busy copies instead of treating them as a new installation. If either is lost or corrupt, preserve what remains and review recovery before syncing; creating a new identity can leave old copies orphaned.

Use the **new installation's** executable for the following read-only checks:

```sh
calendar-busy-sync --version
calendar-busy-sync status
calendar-busy-sync doctor
calendar-busy-sync sync --dry-run
```

Substitute the full wheel-installed path if applicable. A restored `last_success` is historical, not evidence that the new machine can connect. If credentials have expired or were revoked, run `calendar-busy-sync auth google` or `calendar-busy-sync auth outlook` for the affected provider, then repeat the checks; do not create new cloud registrations merely because a token expired. A dry run can legitimately show changes since the backup, but unexpected wholesale creation or deletion needs investigation before proceeding.

After reviewing the preview, run one successful `calendar-busy-sync sync`. Only then enable your chosen schedule, for example `calendar-busy-sync schedule enable --minutes 5`, and check `calendar-busy-sync schedule status`. Do not import the old managed cron block: it contains the old interpreter path and storage roots. Re-enabling with the new executable regenerates these while preserving unrelated jobs. Never reactivate the old machine alongside the restored one.

## Privacy, Ownership and Horizon

Configuration is stored under `${XDG_CONFIG_HOME:-~/.config}/calendar-busy-sync`; credentials, SQLite references and lock files are under `${XDG_STATE_HOME:-~/.local/state}/calendar-busy-sync`. Directories are owner-only and credential/state files are private. Never commit or share these files. Preserve the configuration's installation identity and sync database; discarding ownership state can leave anonymous orphan copies that require manual review.

Copies are timed, private Busy appointments without the source title, description, location, attendees, meeting links or reminders. The only description is a fixed origin note: `From personal calendar.` in Outlook or `From work calendar.` in Google. Existing owned copies receive the note once on their next successful sync; later unchanged syncs do not rewrite them. Copies are created without guests or online meetings. Do not add guests to them: the tool refuses to update or delete a guest-bearing copy rather than trigger meeting updates or cancellations. An unrelated calendar overlay is never changed just because its title or time looks similar.

All-day originals become timed blocks spanning their actual local-midnight boundaries; daylight-saving days need not be 24 hours long. Recurring occurrences are expanded by the providers rather than recreated as destination recurrence rules.

The default horizon is seven days back and 60 days ahead (approximately two months, not two calendar months). During `setup`, set **Sync look-back days** (0–365) and **Sync look-ahead days** (1–730); pressing Enter retains existing values, or uses 7/60 for a new configuration. The private configuration fields are `days_back` and `days_forward`. Package upgrades preserve explicitly saved values, including an older 365-day choice. Changing the horizon requires a new successful manual sync before scheduling. Sources inside it are mirrored; a tracked source that moves outside it has its old copy removed. Historical copied events outside the current views are retained when they cannot be identified from the local tracking references. Do not use destination copies to edit originals. One-way mode never writes Google; reciprocal mode requires explicit additional Google consent. Tool-owned copies are not mirrored back.

To stop background use manually, run `calendar-busy-sync schedule disable`. Removing the Debian package automatically removes its well-formed, ownership-marked system-Python job from local users' crontabs before deleting application files. Unrelated entries and jobs using other Python installations are preserved. Ambiguous markers, changed crontabs, or cleanup failures stop removal for manual review rather than silently leaving the managed job behind. For wheel or virtual-environment installations, disable the schedule before uninstalling; pip does not run Debian removal hooks. Revoke the application's access in your Google account and Microsoft account/organization controls when abandoning it. Package removal and purge preserve credentials, user configuration, ownership state, originals, and copied calendar events; they do not authorize deleting calendar data.
