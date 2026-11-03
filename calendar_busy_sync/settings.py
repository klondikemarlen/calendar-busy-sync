"""Local-only configuration and credential storage, never inside a checkout."""

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    days_forward: int = 365

    def validate(self):
        if self.direction not in ("google-to-outlook", "both"):
            raise SyncError("Choose google-to-outlook or both as the sync direction.")
        if not re.fullmatch(r"[0-9a-f]{32}", self.installation_id):
            raise SyncError("The local installation identity is invalid; restore the original configuration.")
        try:
            uuid.UUID(self.microsoft_client_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise SyncError("Setup requires a Microsoft public-client application ID (UUID).") from error
        if not isinstance(self.microsoft_tenant, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.microsoft_tenant
        ):
            raise SyncError("Use a Microsoft tenant ID/domain or organizations, without a URL.")
        for calendar in (self.google_calendar, self.outlook_calendar):
            if not isinstance(calendar, str) or not calendar.strip() or "\n" in calendar or "\r" in calendar:
                raise SyncError("Both selected calendar identifiers must be nonempty single-line values.")
        if type(self.days_back) is not int or not 0 <= self.days_back <= 365:
            raise SyncError("The look-back window must be between 0 and 365 days.")
        if type(self.days_forward) is not int or not 1 <= self.days_forward <= 730:
            raise SyncError("The look-ahead window must be between 1 and 730 days.")


def load_settings(paths: Paths) -> Settings:
    paths.ensure()
    try:
        configuration = json.loads(paths.config_file.read_text(encoding="utf-8"))
        if not isinstance(configuration, dict) or "installation_id" not in configuration:
            raise SyncError(
                "Existing configuration lost its installation identity; restore the original configuration."
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
