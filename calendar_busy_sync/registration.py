"""Interactive provider registration, separate from calendar-access consent."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from urllib.parse import quote

from .models import SyncError
from .settings import Paths
from .setup_tools import SetupTool, temporary_cli


def _run(tool: SetupTool, args: list[str], *, interactive: bool = False) -> subprocess.CompletedProcess[str]:
    timeout = 120
    if interactive:
        timeout = 900
    try:
        result = subprocess.run(
            [tool.executable, *args],
            env=tool.environment,
            text=True,
            capture_output=not interactive,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SyncError(
            "Provider administration could not complete; inspect the provider console before retrying."
        ) from None
    if result.returncode:
        raise SyncError(
            "Provider administration failed. Check the account and its project/tenant access; use manual registration if needed."
        )
    return result


def prepare_google(paths: Paths, prompt: Callable[..., str], confirm: Callable[..., bool]) -> None:
    paths.ensure()
    print(
        "Google Cloud CLI sign-in authorizes Cloud administration, not Calendar access. Its isolated login data will be removed after this helper."
    )
    with temporary_cli("gcloud", confirm) as tool:
        if tool is None:
            print(
                "Continue in Cloud Console: https://console.cloud.google.com/auth/clients — create a Desktop app client and download its JSON."
            )
            return
        _run(tool, ["auth", "login"], interactive=True)
        project = prompt("Existing Google Cloud project ID (leave blank to create one)")
        if project:
            if not confirm(f"Use existing project {project!r}?"):
                return
        else:
            project = prompt("New globally unique Google Cloud project ID")
            if not project:
                raise SyncError("A project ID is required.")
            if not confirm(
                f"Create project {project!r}? The project will remain in Google Cloud; no billing will be linked."
            ):
                return
            try:
                _run(tool, ["projects", "create", project, "--name=Calendar Busy Sync", "--quiet"])
            except SyncError:
                raise SyncError(
                    "Google project creation did not complete successfully. It may already exist; inspect Cloud Console before retrying."
                ) from None
            print(f"Project {project!r} was created and will be preserved even if later setup is cancelled.")
        if not confirm(f"Enable Calendar API for {project!r}? No billing will be configured."):
            return
        try:
            _run(
                tool,
                ["services", "enable", "calendar-json.googleapis.com", f"--project={project}", "--quiet"],
            )
        except SyncError:
            raise SyncError(
                "Calendar API enablement did not complete successfully. Preserve the project and inspect Cloud Console before retrying."
            ) from None
    print(
        "Temporary Google administration files have been removed. Complete Branding, External audience and a Desktop app client in Cloud Console, then download its JSON outside the checkout."
    )
    for section in ("branding", "audience", "clients"):
        print(f"https://console.cloud.google.com/auth/{section}?project={quote(project, safe='')}")


def create_microsoft(
    paths: Paths, prompt: Callable[..., str], confirm: Callable[..., bool]
) -> tuple[str, str] | None:
    paths.ensure()
    tenant = prompt("Microsoft Entra tenant ID or verified domain")
    if not tenant:
        raise SyncError("A tenant ID or verified domain is required.")
    print(
        "Azure CLI sign-in authorizes administration, separately from the calendar application's consent. Its isolated login data will be removed after this helper."
    )
    with temporary_cli("az", confirm) as tool:
        if tool is None:
            return None
        _run(tool, ["login", "--tenant", tenant, "--allow-no-subscriptions"], interactive=True)
        account = _run(tool, ["account", "show", "--query", "tenantId", "--output", "tsv"])
        try:
            resolved_tenant = str(uuid.UUID(account.stdout.strip()))
        except ValueError:
            raise SyncError(
                "The signed-in tenant could not be resolved. No app was created; check tenant access."
            ) from None
        if not confirm(
            f"Create a single-tenant public-client app named Calendar Busy Sync in tenant {resolved_tenant}, declaring only delegated Calendars.ReadWrite? No secret or tenant-wide consent will be created."
        ):
            return None
        access = [
            {
                "resourceAppId": "00000003-0000-0000-c000-000000000000",
                "resourceAccess": [{"id": "1ec239c2-d7c9-4623-a91a-a9775856bb36", "type": "Scope"}],
            }
        ]
        try:
            result = _run(
                tool,
                [
                    "ad",
                    "app",
                    "create",
                    "--display-name",
                    "Calendar Busy Sync",
                    "--sign-in-audience",
                    "AzureADMyOrg",
                    "--is-fallback-public-client",
                    "true",
                    "--required-resource-access",
                    json.dumps(access),
                    "--query",
                    "appId",
                    "--output",
                    "tsv",
                ],
            )
            app_id = str(uuid.UUID(result.stdout.strip()))
        except (SyncError, ValueError):
            raise SyncError(
                "Microsoft app creation returned no confirmed ID. An app may already exist; inspect Entra before retrying. No application has been deleted."
            ) from None
        print(
            f"Preserve Microsoft application {app_id} in tenant {resolved_tenant}; it remains if local setup is cancelled."
        )
    print("Temporary Azure administration files have been removed; application consent is still separate.")
    return app_id, resolved_tenant
