"""Local-only configuration and credential storage, never inside a checkout."""

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import SyncError


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    state_dir: Path

    @classmethod
    def default(cls):
        home = Path.home()
        return cls(
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "calendar-busy-sync",
            Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "calendar-busy-sync",
        )

    @property
    def config_file(self):
        return self.config_dir / "config.json"

    @property
    def google_client_file(self):
        return self.config_dir / "google-client.json"

    @property
    def google_token_file(self):
        return self.state_dir / "google-token.json"

    @property
    def microsoft_token_file(self):
        return self.state_dir / "microsoft-token.json"

    @property
    def outlook_web_profile_dir(self):
        return self.state_dir / "outlook-web-profile"

    @property
    def outlook_web_identity_file(self):
        return self.state_dir / "outlook-web-identity.json"

    @property
    def database_file(self):
        return self.state_dir / "sync.sqlite3"

    @property
    def lock_file(self):
        return self.state_dir / "sync.lock"

    def ensure(self):
        for directory in (self.config_dir, self.state_dir):
            if not directory.is_absolute() or directory.is_symlink():
                raise SyncError("Configuration and state directories must be absolute, non-symlink paths.")
            resolved = directory.resolve()
            if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
                raise SyncError("Keep configuration, credentials and state outside Git checkouts.")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)


@dataclass
class Settings:
    google_calendar: str = "primary"
    outlook_calendar: str = "default"
    direction: str = "google-to-outlook"
    microsoft_client_id: str = ""
    microsoft_tenant: str = "organizations"
    installation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    days_back: int = 7
    days_forward: int = 60
    work_hours_enabled: bool = False
    work_timezone: str = "UTC"
    work_days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    work_start: str = "08:00"
    work_end: str = "17:00"
    work_lead_minutes: int = 120
    work_trail_minutes: int = 120
    google_lead_minutes: int = 0
    google_trail_minutes: int = 0
    outlook_lead_minutes: int = 0
    outlook_trail_minutes: int = 0
    outlook_backend: str = "graph"
    outlook_web_source_calendar: str = ""
    outlook_web_mirror_calendar: str = ""
    outlook_web_timezone: str = ""

    def _validate_outlook(self):
        if self.outlook_backend == "web":
            source = self.outlook_web_source_calendar
            mirror = self.outlook_web_mirror_calendar
            for name in (source, mirror):
                if not isinstance(name, str) or name != name.strip() or name.splitlines() != [name]:
                    raise SyncError(
                        "Enter the exact single-line names of the Outlook source and mirror calendars."
                    )
            if source.casefold() == mirror.casefold():
                raise SyncError("Outlook web sync requires a separate, dedicated mirror calendar.")
            try:
                ZoneInfo(self.outlook_web_timezone)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                raise SyncError(
                    "Use the IANA timezone that matches the Outlook web calendar display."
                ) from None
            return
        if self.outlook_backend != "graph":
            raise SyncError("Choose graph or web as the Outlook backend.")
        try:
            uuid.UUID(self.microsoft_client_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise SyncError("Setup requires a Microsoft public-client application ID (UUID).") from error
        if not isinstance(self.microsoft_tenant, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.microsoft_tenant
        ):
            raise SyncError("Use a Microsoft tenant ID/domain or organizations, without a URL.")

    def validate(self):
        if self.direction not in ("google-to-outlook", "both"):
            raise SyncError("Choose google-to-outlook or both as the sync direction.")
        if not re.fullmatch(r"[0-9a-f]{32}", self.installation_id):
            raise SyncError("The local installation identity is invalid; restore the original configuration.")
        self._validate_outlook()
        for calendar in (self.google_calendar, self.outlook_calendar):
            if not isinstance(calendar, str) or not calendar.strip() or "\n" in calendar or "\r" in calendar:
                raise SyncError("Both selected calendar identifiers must be nonempty single-line values.")
        if type(self.days_back) is not int or not 0 <= self.days_back <= 365:
            raise SyncError("The look-back window must be between 0 and 365 days.")
        if type(self.days_forward) is not int or not 1 <= self.days_forward <= 730:
            raise SyncError("The look-ahead window must be between 1 and 730 days.")
        if type(self.work_hours_enabled) is not bool:
            raise SyncError("work_hours_enabled must be true or false.")
        try:
            ZoneInfo(self.work_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise SyncError("Use an IANA work timezone such as UTC or America/Phoenix.") from None
        if (
            not isinstance(self.work_days, list)
            or not self.work_days
            or not all(
                isinstance(day, str) and day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
                for day in self.work_days
            )
            or len(set(self.work_days)) != len(self.work_days)
        ):
            raise SyncError("Work days must be unique names from mon through sun.")
        for value in (self.work_start, self.work_end):
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise SyncError("Work start/end must use 24-hour HH:MM.")
        if time.fromisoformat(self.work_end) <= time.fromisoformat(self.work_start):
            raise SyncError("Work end must be later than work start on the same day.")
        for name in (
            "work_lead_minutes",
            "work_trail_minutes",
            "google_lead_minutes",
            "google_trail_minutes",
            "outlook_lead_minutes",
            "outlook_trail_minutes",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 1440:
                raise SyncError(f"{name} must be an integer from 0 to 1440.")


def load_settings(paths: Paths) -> Settings:
    paths.ensure()
    try:
        configuration = json.loads(paths.config_file.read_text(encoding="utf-8"))
        if not isinstance(configuration, dict) or "installation_id" not in configuration:
            raise SyncError(
                "Existing configuration lost its installation identity; restore the original configuration."
            )
        if configuration.pop("event_padding_overrides", []) != []:
            raise SyncError(
                "Per-event padding is no longer supported. Run calendar-busy-sync schedule disable, "
                "then remove event_padding_overrides from your private config.json. "
                "Set directional padding with calendar-busy-sync config set before syncing again."
            )
        settings = Settings(**configuration)
        settings.validate()
        return settings
    except FileNotFoundError as error:
        raise SyncError("Not configured. Run calendar-busy-sync setup first.") from error
    except (OSError, ValueError, TypeError) as error:
        raise SyncError(
            "Cannot read local configuration. Run setup to repair it; preserve its installation identity."
        ) from error


def save_settings(paths: Paths, settings: Settings) -> None:
    paths.ensure()
    settings.validate()
    private_write(paths.config_file, json.dumps(asdict(settings), indent=2) + "\n")
