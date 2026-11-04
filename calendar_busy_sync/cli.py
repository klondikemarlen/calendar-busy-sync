"""Command-line interface for calendar-busy-sync."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from . import __version__, auth, engine, models, registration, settings, state
from .schedule import ScheduleError, disable_schedule, enable_schedule, schedule_status


def _prompt(text: str, *, default: str = "") -> str:
    if not sys.stdin.isatty():
        raise models.SyncError("Setup requires an interactive terminal; rerun from a terminal.")
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value or default


def _yes_no(text: str, *, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        raise models.SyncError("This action requires an interactive terminal; rerun from a terminal.")
    label = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{text} [{label}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.", file=sys.stderr)


def _paths() -> settings.Paths:
    return settings.Paths.default()


def _config_is_scheduled(paths: settings.Paths) -> bool:
    try:
        return schedule_status(paths=paths).enabled
    except ScheduleError as exc:
        if "crontab is not installed" in str(exc):
            return False
        raise


def _configure_timing(current: settings.Settings) -> settings.Settings:
    enabled = _yes_no("Restrict copies to padded work hours?", default=True)
    timezone = _prompt("Work timezone (IANA name)", default=current.work_timezone)
    work_days = _prompt("Workdays (mon tue wed thu fri sat sun)", default=" ".join(current.work_days))
    work_start = _prompt("Work start (HH:MM)", default=current.work_start)
    work_end = _prompt("Work end (HH:MM)", default=current.work_end)
    minutes = {}
    for name, label in (
        ("work_lead_minutes", "Work-window minutes before work starts"),
        ("work_trail_minutes", "Work-window minutes after work ends"),
        ("google_lead_minutes", "Google-to-Outlook event minutes before"),
        ("google_trail_minutes", "Google-to-Outlook event minutes after"),
        ("outlook_lead_minutes", "Outlook-to-Google event minutes before"),
        ("outlook_trail_minutes", "Outlook-to-Google event minutes after"),
    ):
        try:
            minutes[name] = int(_prompt(label, default=str(getattr(current, name))))
        except ValueError:
            raise models.SyncError("Padding minutes must be whole numbers from 0 to 1440.") from None
    return replace(
        current,
        work_hours_enabled=enabled,
        work_timezone=timezone,
        work_days=work_days.lower().replace(",", " ").split(),
        work_start=work_start,
        work_end=work_end,
        **minutes,
    )


def _doctor(paths: settings.Paths) -> None:
    counts = engine.doctor(settings.load_settings(paths), paths)
    print("Google calendar reads: OK (read-only).")
    print("Outlook calendar reads: OK (read-only).")
    print("Event counts returned for the next 24 hours:")
    print(f"  Google: {counts['google']}")
    print(f"  Outlook: {counts['outlook']}")
    print("A count of 0 means no events in that window, not a connection failure.")
    print("No calendar events were changed.")


def _setup(paths: settings.Paths) -> int:
    if _config_is_scheduled(paths):
        if not _yes_no(
            "A managed schedule is enabled. Disable it before changing configuration?", default=False
        ):
            raise models.SyncError("Configuration unchanged; disable the managed schedule before setup.")
        disable_schedule(paths=paths)
    if paths.config_file.exists():
        old = settings.load_settings(paths)
    else:
        old = None
    original_configuration = paths.config_file.read_bytes() if paths.config_file.exists() else None
    print(
        "This tool copies only Busy blocks. Google-to-Outlook needs Google read access and Outlook write access; bidirectional mode also writes to Google."
    )
    direction = _prompt(
        "Direction (google-to-outlook/both)", default=old.direction if old else "google-to-outlook"
    )
    if direction not in {"google-to-outlook", "both"}:
        raise models.SyncError("Direction must be google-to-outlook or both.")
    google_input = _prompt(
        "Path to Google OAuth Desktop client JSON (leave blank for guided registration)",
        default=str(paths.google_client_file) if paths.google_client_file.is_file() else "",
    )
    if not google_input:
        if _yes_no("Prepare a Google Calendar project with CLI assistance?", default=False):
            registration.prepare_google(paths, prompt=_prompt, confirm=_yes_no)
        else:
            print("Create a Desktop OAuth client at https://console.cloud.google.com/auth/clients")
        google_input = _prompt("Path to downloaded Google OAuth Desktop client JSON")
    google_path = Path(google_input).expanduser()
    try:
        client_data = json.loads(google_path.read_text(encoding="utf-8"))
        if not isinstance(client_data, dict) or "installed" not in client_data:
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        raise models.SyncError("Could not read a valid Google Desktop OAuth client JSON file.") from None
    ms_id = _prompt(
        "Microsoft public-client application ID (leave blank for guided registration)",
        default=old.microsoft_client_id if old else "",
    )
    created_microsoft = None
    if not ms_id:
        if _yes_no("Create a Microsoft public-client registration with CLI assistance?", default=False):
            created_microsoft = registration.create_microsoft(paths, prompt=_prompt, confirm=_yes_no)
        if created_microsoft is None:
            print("Obtain an approved public-client application ID at https://entra.microsoft.com/")
            ms_id = _prompt("Microsoft public-client application ID")
    if created_microsoft is not None:
        ms_id, tenant = created_microsoft
    else:
        tenant = _prompt("Microsoft tenant", default=old.microsoft_tenant if old else "organizations")
    try:
        current = replace(
            old or settings.Settings(),
            direction=direction,
            microsoft_client_id=ms_id,
            microsoft_tenant=tenant,
        )
        try:
            current.days_back = int(_prompt("Sync look-back days (0-365)", default=str(current.days_back)))
            current.days_forward = int(
                _prompt(
                    "Sync look-ahead days (1-730; 60 is about two months)", default=str(current.days_forward)
                )
            )
        except ValueError:
            raise models.SyncError("Sync horizon days must be whole numbers.") from None
        if _yes_no("Configure work hours and event padding?", default=False):
            current = _configure_timing(current)
        current.validate()
        with engine.sync_lock(paths):
            if _config_is_scheduled(paths):
                raise models.SyncError(
                    "A schedule became enabled during setup; disable it before changing configuration."
                )
            latest_configuration = paths.config_file.read_bytes() if paths.config_file.exists() else None
            if latest_configuration != original_configuration:
                raise models.SyncError(
                    "Configuration changed during setup; restart setup without overwriting it."
                )
            paths.ensure()
            settings.private_write(paths.google_client_file, json.dumps(client_data))
            settings.save_settings(paths, current)
            state.invalidate_success(paths)
    except (OSError, ValueError):
        raise models.SyncError("Could not save configuration securely.") from None
    if _yes_no("Authorize Google and Outlook now?", default=False):
        _authorize("all", paths)
    if _yes_no("Run a read-only connectivity check?", default=False):
        _doctor(paths)
    if _yes_no("Run the initial synchronization now?", default=False):
        print(json.dumps(_run_sync(paths), sort_keys=True))
        if _yes_no("Enable a recurring schedule?", default=False):
            enable_schedule(paths=paths)
            print("Schedule enabled.")
    return 0


def _authorize(provider: str, paths: settings.Paths) -> None:
    with engine.sync_lock(paths):
        configured = settings.load_settings(paths)
        state.invalidate_success(paths)
        if provider in ("google", "all"):
            auth.authorize_google(configured, paths)
        if provider in ("outlook", "all"):
            auth.authorize_outlook(configured, paths)
    print("Authorization completed; tokens were stored locally.")


def _run_sync(paths: settings.Paths, dry_run: bool = False) -> dict:
    configured = settings.load_settings(paths)
    try:
        return engine.run_sync(configured, paths, dry_run=dry_run)
    except models.SyncError as exc:
        message = str(exc)
        if "revok" in message.lower() or "expired" in message.lower():
            raise models.SyncError(
                f"Authorization was rejected or revoked. Run 'calendar-busy-sync auth'. {message}"
            ) from None
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calendar-busy-sync", description="Copy busy-only calendar blocks between Google and Outlook."
    )
    parser.add_argument("--version", action="version", version=f"calendar-busy-sync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="configure accounts interactively")
    auth_parser = sub.add_parser("auth", help="authorize calendar accounts")
    auth_parser.add_argument("provider", nargs="?", choices=("google", "outlook", "all"), default="all")
    sync_parser = sub.add_parser("sync", help="synchronize busy blocks")
    sync_parser.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor", help="check account connectivity without changing calendars")
    sub.add_parser("status", help="show local configuration and sync state")
    schedule = sub.add_parser("schedule", help="manage recurring synchronization")
    schsub = schedule.add_subparsers(dest="schedule_action", required=True)
    enable = schsub.add_parser("enable")
    enable.add_argument("--minutes", type=int, default=15)
    schsub.add_parser("disable")
    schsub.add_parser("status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    paths = _paths()
    try:
        if args.command == "setup":
            return _setup(paths)
        if args.command == "auth":
            _authorize(args.provider, paths)
        elif args.command == "sync":
            result = _run_sync(paths, args.dry_run)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "doctor":
            _doctor(paths)
        elif args.command == "status":
            configured = settings.load_settings(paths)
            print(
                json.dumps(
                    {
                        "configured": True,
                        "direction": configured.direction,
                        "last_success": state.last_success(paths),
                        "schedule": schedule_status(paths=paths).enabled,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "schedule":
            if args.schedule_action == "enable":
                enable_schedule(paths=paths, minutes=args.minutes)
            elif args.schedule_action == "disable":
                disable_schedule(paths=paths)
            else:
                status = schedule_status(paths=paths)
                print("enabled" if status.enabled else "disabled")
        return 0
    except (models.SyncError, ScheduleError) as exc:
        print(f"calendar-busy-sync: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print(
            "calendar-busy-sync: Cannot access local configuration or state; preserve the files and check permissions.",
            file=sys.stderr,
        )
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
