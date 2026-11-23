# Private calendar connectors

Use this mode when employees should connect two accounts without putting their work credential on a personal machine. The agency operates a Microsoft connector in its trust domain. A separate operator runs the Google connector in its own environment. There is no project-operated relay, public feed, or requirement that either calendar be public. The existing local CLI remains a separate option. Do not run the CLI and the connectors on the same calendar pair.

## What an employee authorizes

Deployment, administrator approval, visiting the enrollment page, and signing in **do not pair calendars**. On each connector, read the permission disclosure, check the consent box, and sign in with that provider. Set the times that calendar may send to its peer. Generate an invitation on the Microsoft connector, enter it on the Google connector, and bring the confirmation code back to the signed-in Microsoft connector. Confirm the transfer. Codes expire after ten minutes; treat both as private. This proves control of the two sessions, not that both providers verified the same legal person. Each account can have only one active pair.

The sync writes real, private `Busy` appointments to each **primary calendar**. Microsoft's **Can view when I'm busy** and Google's **See only free/busy** are viewer permissions, not permissions to write those appointments. Neither connector publishes a calendar or changes its sharing policy. No event-detail sharing option exists. The agency must authorize the transfer of work timing to this Google destination; a coworker's Scheduling Assistant access does not authorize it. Managed Google accounts can also require administrator approval.

The source provider already hosts its own events. The destination provider learns that its user is busy at the copied times; the application cannot hide an appointment from the provider storing it. Connector copies have no origin note or common cross-provider pairing identifier. People already entitled to see availability on the destination calendar may see the new interval under those existing permissions.

### Share only the time the other calendar needs

Set the outgoing rules on the calendar that holds the originals. For example, to keep evenings off a work Outlook calendar, set the **Google → Outlook** sharing window on the Google connector. Choose the time zone, days and local hours, and any minutes to block before or after appointments. Padding applies first; the connector sends only the part inside the window. With a 09:00–17:00 window, a padded 16:30–17:30 appointment sends 16:30–17:00. An appointment whose padded time falls entirely after 17:00 sends nothing. The original does not change.

The reverse direction has independent settings. Neither account is classified as work or personal. Without a sharing window or padding, pairing shares the unpadded busy intervals in the rolling 30-day horizon. Restricting an existing window changes eligible copies on the next successful sync; it cannot undo earlier disclosure.

### What crosses the boundary

| Boundary | Stored or exchanged |
| --- | --- |
| Microsoft deployment | One pinned work tenant, web-app credential, individual delegated token caches, local OIDC subjects and pairing state. `Calendars.ReadWrite` can read event details, not just free/busy; the connector uses `/me`'s default calendar, not a directory or mailbox-list API. No Graph application permissions. |
| Google deployment | Web-client credential, individual OAuth grants, local subjects and pairing state. `calendar.events.owned` can read and edit event details on calendars the user owns; the implementation uses only `primary`. No service-account impersonation or Workspace-wide delegation. |
| Between deployments | During pairing: random codes/digests and an opaque pair identifier. During sync: that identifier, the UTC window date, and sorted, merged UTC start/end intervals. Never provider tokens, email addresses, event IDs, subject lines, locations, attendees, or descriptions. Timing and association with the pair are still sensitive. |

Each connector reads enough local event data to exclude the copies it created. Subtracting busy periods instead would hide real overlapping meetings. It exchanges a rolling 30-day window from midnight UTC, not a calendar history. The peer writes generic events with private ownership markers. A failing read is not an empty calendar. If a write outcome is uncertain, the next successful run reconciles it. The two calendars are not one transaction.

## Operator setup for a pilot

1. Provision two **separate** service identities, HTTPS origins, private storage volumes, and operator access boundaries. Do not mount the other node's config, database or backups. Restrict access to the work enrollment page to authorized participants (for example, Entra enterprise application assignment). Use supported commercial Microsoft Graph endpoints. A national-cloud deployment needs a separate assessment; do not substitute hosts or use real government data for testing.
2. [Register the Microsoft connector](#register-the-microsoft-connector) in the organization's own directory. Keep Conditional Access and MFA in place. Configure assignment and consent under organization policy.
3. On the Google side enable Google Calendar API and configure an OAuth **web application** with exact redirect `https://GOOGLE-HOST/callback`. Request `openid` and `https://www.googleapis.com/auth/calendar.events.owned`, and offline access. Configure the consent audience and publishing/verification for the intended users. Consumer Google consent is still required. Google Testing-mode refresh grants can expire. Use separate approved test accounts for the pilot.
4. Install this revision on both nodes under separate unprivileged OS users. Use an operator-supported WSGI server (e.g. Gunicorn installed in the same environment), a TLS reverse proxy, and private encrypted disks/backups. Grants are not encrypted by the application; anyone able to read the service database or its backups is trusted. Do not enable debug mode, request-body/header logging, OAuth debug logging, or proxy/server logging that includes callback query strings. Limit body size (512 KiB), rate-limit requests and restrict `/peer/` at the proxy/firewall to the peer deployment. Set HSTS on the external origin, preserve its Host header, and never expose the plain HTTP backend directly.
5. Place a JSON configuration file outside the checkout, owned by the service user with mode `0600`. Point `CBS_CONNECTOR_CONFIG` at its full path. The required keys are `role` (`microsoft` or `google`), `origin` (this node's HTTPS origin without a trailing slash), `peer_origin` (the other origin), `client_id`, `client_secret`, `peer_secret`, and `state_dir` (absolute private directory owned by the service user with mode `0700`, or an absent directory with an existing private parent). Microsoft additionally requires `tenant` (the agency's directory UUID); omit it on Google. Configure only the local provider's credential. Generate a 32-byte random URL-safe peer secret, e.g. `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, in a private administrative session, and deliver it to the two operators through their secret manager. Never use example keys, log this secret or enter it into the enrollment page.
6. Example service launch on each node, behind its proxy: `gunicorn --bind 127.0.0.1:8090 --timeout 180 'calendar_busy_sync.connector:application()'`. Set `umask 077` for both the server and worker; disable access logging and use a private service environment. Use local SQLite on one host per node; do not share it across replicas. A simultaneous operation may receive a temporary busy response rather than racing a sync or revocation. The origin, peer origin, role, tenant and client ID are bound to that database. Protect both backups from unauthorized rollback or replay; do not restore one side of a disconnected pairing as an active production enrollment.
7. After the enrolled pilot users explicitly agree, run `calendar-busy-sync connector sync` as the service user with the same configuration on each node. Microsoft initiates all calendar exchanges. Google processes deferred disconnects only in its scheduled invocation. Run periodically with your approved scheduler (for example every 15 minutes); installation enables nothing. No eligible pairs means no calendar calls. An error does not authorize sending an empty snapshot. Use bounded WSGI/proxy timeouts suitable for your calendars and monitor worker exit status without logging account data.

## Register the Microsoft connector

Run `calendar-busy-sync connector setup` in an administrator's terminal to create the Microsoft web registration and Enterprise application interactively. This can run on an admin workstation with Calendar Busy Sync installed; it does not need connector configuration or an employee's login. Supply the tenant UUID and the deployed connector's HTTPS origin. Setup uses an isolated Azure CLI login, confirms the tenant and callback before writing, and prints the public IDs. Complete credential creation and consent review as described below.

If you already have a registration or lack administrator access, decline creation. Ask the authorized admin to run setup if a new registration is needed. The admin hands the public IDs and expiring credential to the deployment operator through approved channels. Employees only visit the deployed connector's enrollment page; do not give them the server credential. The manual equivalent follows.

Use Bash and Azure CLI as an operator authorized to register apps in the intended commercial Microsoft 365 tenant. This recipe is for the server connector, not the desktop CLI's public-client registration. It needs an existing Entra directory and an HTTPS connector origin, not an Azure compute subscription.

In a fresh Bash shell, enter the directory ID and the actual connector origin (no trailing slash). Check the active directory **before** creating anything:

```bash
set -eu
read -rp 'Organization tenant UUID: ' tenant
read -rp 'Microsoft connector origin (https://hostname): ' origin
[[ "$origin" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?$ ]] || exit 1
az login --tenant "$tenant" --allow-no-subscriptions --output none
[[ "$(az account show --query tenantId -o tsv)" == "$tenant" ]] || exit 1
```

Create the web registration **once**. Record the printed public client ID. If interrupted, inspect **Entra → App registrations** and resume with the confirmed ID; running `app create` again can create a duplicate with the same name.

```bash
app_id=$(az ad app create --display-name 'Calendar Busy Sync' \
  --sign-in-audience AzureADMyOrg --web-redirect-uris "$origin/callback" \
  --query appId -o tsv)
printf 'Client ID: %s\n' "$app_id"
```

Declare Microsoft Graph **delegated** `Calendars.ReadWrite` (`Scope`, not `Role`), then create the tenant's Enterprise application. These commands do **not** grant admin consent. If resuming, inspect the registration and Enterprise application first; run only missing steps.

```bash
az ad app permission add --id "$app_id" \
  --api 00000003-0000-0000-c000-000000000000 \
  --api-permissions 1ec239c2-d7c9-4623-a91a-a9775856bb36=Scope
az ad sp create --id "$app_id" --output none
az ad app show --id "$app_id" \
  --query '{audience:signInAudience,callbacks:web.redirectUris,permissions:requiredResourceAccess}'
```

Verify `AzureADMyOrg`, the exact HTTPS `/callback` URL, and only the Graph scope above. In **App registrations → your app → Certificates & secrets**, create an expiring client secret. Store its **value**, not its ID, as `client_secret` in the protected connector configuration alongside `tenant` and `client_id` from above. Keep it out of tickets, shell history and source control; assign an owner to rotate it before expiry. Review consent under local policy. Registering the app neither enrolls users nor starts synchronization.

**Distribution decision:** We chose one organization-owned registration per deployment over a shared publisher registration or Entra Gallery listing. A publisher tenant, verification and cross-tenant onboarding would add ongoing maintenance and support for the project without eliminating customer approval or deployment. This is an operating-cost decision, not a claim that Microsoft charges a listing fee or requires Azure hosting. Each organization instead pays the setup cost once and owns credential rotation and revocation. The [shared desktop identity proposal](https://github.com/klondikemarlen/calendar-busy-sync/issues/46) is not planned.

## Approval, testing and disconnect

In a disposable pair, confirm that an enrolled but unpaired user creates no blocks. Pair the accounts, create one generic overlapping appointment on each side, run the worker, then repeat, move, and cancel one original. Only the paired accounts should receive private Busy copies; originals and the other overlap must survive. Test an unpaired second account. Check coworker Scheduling Assistant visibility separately; a successful API write is not that observation.

**Disconnect** removes the local saved grant, stops local exchange and sends the peer an unlink notice. Offline notices are retried. The partner's own authorization remains under its user's control, but the disconnected pair cannot synchronize. Disconnect at both ends and revoke the app at each provider to withdraw both grants. An already completed or in-flight transfer cannot be recalled. Existing calendar blocks and backups are not automatically erased by disconnect; users can remove previously copied blocks in their calendars. Stop the worker before retiring a deployment, revoke user/app grants, retire its credentials and paired secret, and apply the operators' approved backup retention policy.
