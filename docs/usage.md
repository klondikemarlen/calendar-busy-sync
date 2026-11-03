# Local Setup and Operation

These are procedures for development builds. The real-account acceptance check in [issue #1](https://github.com/klondikemarlen/calendar-busy-sync/issues/1) must be completed before claiming the integration works for a particular Google account and Microsoft tenant. Automated fixtures do not prove provider consent or coworkers' free/busy visibility.

## Install and Inspect

For a locally built Debian package, use `sudo apt install ./dist/calendar-busy-sync_0.1.0_all.deb`. APT resolves the Python authentication dependencies and cron. Installation does not change your calendars or crontab.

For development, run `make dev`, then `.venv/bin/calendar-busy-sync --help`. The runtime requires Python 3.11 or newer. `make lint`, `make format-check`, and `make test` are the supported checks. Development virtual environments/checkouts are not intended as a durable cron installation.

## Create Your Own OAuth Applications

No shared application credentials are bundled. Both registrations are free; your organization still controls whether its account can consent.

### Optional Registration Tools

The Debian package suggests `google-cloud-cli` (the `gcloud` command) and `azure-cli` (the `az` command) as optional one-time registration aids. Neither is required by `calendar-busy-sync setup`, account authorization, manual sync or cron: the Python application talks directly to the calendar APIs. Browser-based registration remains supported without either CLI. They are suggestions rather than recommendations because normal installations can operate without them, and APT normally installs recommended packages by default.

Install them yourself only if you want terminal-based project or application administration. Follow the [Google Cloud CLI installation instructions](https://docs.cloud.google.com/sdk/docs/install-sdk) or [Azure CLI installation instructions](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux?pivots=apt); their APT packages may require separately configured vendor repositories. Installing this package does not add those repositories, install the suggested CLIs by default, sign you in or register applications.

Google Workspace CLI (`gws`) is not a dependency. Its setup wizard is another optional helper, not needed for the procedure below. Administrative CLI sign-in and the calendar application's calendar-access consent are separate authorizations.

### Google

1. In [Google Cloud](https://console.cloud.google.com/), create/select a project and enable the Google Calendar API.
2. Configure the OAuth consent audience for your own use, then create an OAuth client with application type **Desktop app**. Download its JSON file to a local private location outside the checkout.
3. Setup uses a browser sign-in with a loopback callback and PKCE. The downloaded desktop-client secret is not a meaningful application-identity security boundary: installed apps cannot keep a shared secret.
4. Google-to-Outlook mode requests `calendar.readonly`. Reciprocal mode also requests `calendar.events`. No Gmail, Drive, Contacts or birth-date permissions are requested.

Google's consent-screen **Testing** status can cause refresh tokens for these scopes to expire after seven days. Review [Google's refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration); do not assume a scheduled sync will remain authorized indefinitely. A broader old Google grant must be revoked and authorized again before it can truthfully become read-only.

For optional CLI-assisted preparation, the following commands create a dedicated project and enable only Calendar, using a separate owner-only `gcloud` configuration outside the checkout. Choose a globally unique project ID in place of `your-unique-project-id`. Complete Google's browser sign-in yourself; these commands authorize project administration, not the calendar application's access to your events.

```sh
export CLOUDSDK_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/calendar-busy-sync/gcloud"
install -d -m 700 "$CLOUDSDK_CONFIG"
gcloud auth login
PROJECT_ID=your-unique-project-id
gcloud projects create "$PROJECT_ID" --name="Calendar Busy Sync"
gcloud services enable calendar-json.googleapis.com --project="$PROJECT_ID"
```

For an existing project, set `PROJECT_ID` to its ID and skip `gcloud projects create`. These commands do not link billing, create calendar events or enable scheduling. Select that project in Cloud Console and finish the audience and **Desktop app** client creation described above; Google's [documented Desktop client registration](https://developers.google.com/identity/protocols/oauth2/native-app#creatingcred) still uses the Console. Do not substitute `gcloud iam oauth-clients` (workforce identity federation) for the Calendar Desktop client.

After supplying the downloaded client JSON to `calendar-busy-sync setup`, `calendar-busy-sync auth google` opens the browser consent flow and stores refreshable credentials privately. No CLI administration token is reused as a calendar-access token. Refresh remains subject to the Testing-mode expiry and revocation rules above.

### Microsoft

1. Ask your organization's administrator whether a locally run public-client app may access your own calendar through Microsoft Graph. Register an application in [Microsoft Entra](https://entra.microsoft.com/) or obtain the approved application ID from your administrator.
2. Use a public-client application with **Allow public client flows** enabled for device sign-in. Do not create an application client secret.
3. Add the **delegated** Microsoft Graph `Calendars.ReadWrite` permission, not an application/directory-wide permission. Your tenant may require administrator consent or prohibit device sign-in.
4. Record the application/client ID and tenant ID/domain for the wizard. `organizations` is appropriate only when the registration supports that audience. MSAL also handles the standard sign-in/offline scopes needed for refresh.

The tool cannot bypass a tenant policy. If authorization is blocked, keep scheduling disabled and ask the administrator to review this exact delegated-calendar use case.

Azure CLI can help an authorized administrator provision the public-client registration through Microsoft Graph, instead of using Entra's forms. It is not needed to accept an existing approved client ID or to complete the application's device sign-in. Keep any administrative CLI login separate from the application's local token cache; never use tenant-wide calendar consent merely to simplify setup.

## Guided Setup

Run `calendar-busy-sync setup` in a terminal. The wizard accepts the local Google desktop-client JSON, Microsoft client ID/tenant, and direction. It preserves the installation identity on reconfiguration. It separately offers authorization, a read-only connectivity check, an initial sync, and finally scheduling; none is implied by installing the package. A corrupt existing configuration must be repaired without inventing a new installation identity.

Alternatively, after configuration:

```sh
calendar-busy-sync auth
calendar-busy-sync doctor
calendar-busy-sync sync --dry-run
calendar-busy-sync sync
calendar-busy-sync status
```

`doctor` reads both calendars without writing events. A dry run previews operation counts, not private details, and does not satisfy the scheduling gate. A manual sync prints created/updated/deleted/unchanged counts. Nonzero exit status means the run did not finish successfully; scheduled runs never open an interactive sign-in prompt. Reauthorize explicitly with `calendar-busy-sync auth google` or `calendar-busy-sync auth outlook` when instructed.

## Optional Scheduling

After a successful sync using the current configuration:

```sh
calendar-busy-sync schedule enable --minutes 15
calendar-busy-sync schedule status
calendar-busy-sync schedule disable
```

The scheduler uses your user crontab, an absolute installed Python/application invocation, and explicit XDG storage roots. It changes only its marked block and refuses to overwrite a crontab that changed during the update. A manual run and a scheduled run cannot execute concurrently. Existing unrelated jobs remain untouched. Reconfiguring requires disabling the managed schedule first.

The computer must be awake, online, and running cron. Missed cron executions are not automatically replayed; run `calendar-busy-sync sync` when you need immediate reconciliation. Busy status informs scheduling but does not prevent someone from sending a conflicting invitation.

## Privacy, Ownership and Horizon

Configuration is stored under `${XDG_CONFIG_HOME:-~/.config}/calendar-busy-sync`; credentials, SQLite references and lock files are under `${XDG_STATE_HOME:-~/.local/state}/calendar-busy-sync`. Directories are owner-only and credential/state files are private. Never commit or share these files. Preserve the configuration's installation identity and sync database; discarding ownership state can leave anonymous orphan copies that require manual review.

Copies are timed, private Busy appointments without the source title, description, location, attendees, meeting links or reminders. All-day originals become timed blocks spanning their actual local-midnight boundaries; daylight-saving days need not be 24 hours long. Recurring occurrences are expanded by the providers rather than recreated as destination recurrence rules.

The default horizon is seven days back and 365 days ahead. Sources inside it are mirrored; a tracked source that moves outside it has its old copy removed. Historical copied events outside the current views are retained when they cannot be identified from the local tracking references. Do not use destination copies to edit originals. One-way mode never writes Google; reciprocal mode requires explicit additional Google consent. Tool-owned copies are not mirrored back.

To stop background use, disable the schedule before removing the package. Revoke the application's access in your Google account and Microsoft account/organization controls when abandoning it. Removing a package does not safely imply permission to delete your calendar events or personal state.
