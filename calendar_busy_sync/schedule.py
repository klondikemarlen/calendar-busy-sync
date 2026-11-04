"""Safe ownership-marked per-user crontab management."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import engine, models, settings, state

_BEGIN = "# BEGIN calendar-busy-sync managed schedule v1"
_END = "# END calendar-busy-sync managed schedule v1"


class ScheduleError(models.SyncError):
    """A safe-to-display scheduling failure."""


@dataclass(frozen=True)
class ScheduleStatus:
    enabled: bool
    minutes: int | None = None


def _crontab_path() -> str:
    value = shutil.which("crontab")
    if not value:
        raise ScheduleError("crontab is not installed; install cron before managing a schedule.")
    return os.path.abspath(value)


def _invoke(binary: str, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, LC_ALL="C")
    try:
        return subprocess.run(
            [binary, *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ScheduleError(
            "Crontab command did not complete; inspect schedule status before retrying."
        ) from None


def _read(binary: str) -> tuple[str, bool]:
    result = _invoke(binary, "-l")
    if result.returncode == 0:
        return result.stdout, True
    if result.returncode == 1 and "no crontab for " in result.stderr.lower():
        return "", False
    raise ScheduleError("Could not read the current crontab; no changes were made.")


def _entry(minutes: int, paths: settings.Paths) -> str:
    from . import cli

    module_file = Path(cli.__file__).resolve()
    if any((parent / ".git").exists() for parent in (module_file.parent, *module_file.parents)):
        raise ScheduleError(
            "Scheduling from a source checkout is unsupported; install the application first."
        )
    executable = sys.executable
    if not os.path.isabs(executable):
        raise ScheduleError("Cannot determine an absolute installed Python interpreter for cron.")
    command = f"{shlex.quote(executable)} -m calendar_busy_sync sync"
    env = (
        f"XDG_CONFIG_HOME={shlex.quote(str(paths.config_dir.parent))} "
        f"XDG_STATE_HOME={shlex.quote(str(paths.state_dir.parent))}"
    )
    path = os.pathsep.join(dict.fromkeys([str(Path(executable).parent), "/usr/bin", "/bin"]))
    rendered = f"{env} PATH={shlex.quote(path)} {command}".replace("%", r"\%")
    return f"*/{minutes} * * * * {rendered}"


def _owned_lines(lines: list[str]) -> tuple[list[str], int]:
    begins = [i for i, line in enumerate(lines) if line == _BEGIN]
    ends = [i for i, line in enumerate(lines) if line == _END]
    if not begins and not ends:
        return lines, 0
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0] or ends[0] != begins[0] + 2:
        raise ScheduleError("Managed schedule block is malformed; review the crontab manually.")
    return lines[: begins[0]] + lines[ends[0] + 1 :], 1


def schedule_status(*, paths: settings.Paths | None = None) -> ScheduleStatus:
    binary = _crontab_path()
    text, _ = _read(binary)
    lines = text.splitlines()
    _, removed = _owned_lines(lines)
    if not removed:
        return ScheduleStatus(False)
    line = lines[lines.index(_BEGIN) + 1]
    try:
        minutes = int(line.split()[0][2:])
    except (ValueError, IndexError):
        raise ScheduleError("Managed schedule command is malformed; review the crontab manually.") from None
    return ScheduleStatus(True, minutes)


def enable_schedule(*, paths: settings.Paths | None = None, minutes: int = 15) -> None:
    paths = paths or settings.Paths.default()
    if not 1 <= minutes <= 59:
        raise ScheduleError("Schedule interval must be between 1 and 59 minutes.")
    binary = _crontab_path()
    with engine.sync_lock(paths):
        configured = settings.load_settings(paths)
        if not state.has_success(paths, configured):
            raise ScheduleError(
                "A successful sync with the current configuration is required before scheduling."
            )
        before, _ = _read(binary)
        clean, _ = _owned_lines(before.splitlines())
        new_text = "\n".join(clean + [_BEGIN, _entry(minutes, paths), _END]) + "\n"
        current, _ = _read(binary)
        if current != before:
            raise ScheduleError(
                "Crontab changed during schedule update; no changes were made. Retry after reviewing it."
            )
        result = _invoke(binary, "-", input_text=new_text)
        if result.returncode:
            raise ScheduleError(
                "Could not install the managed schedule; unrelated crontab entries were not changed."
            )


def disable_schedule(*, paths: settings.Paths | None = None) -> None:
    paths = paths or settings.Paths.default()
    binary = _crontab_path()
    with engine.sync_lock(paths):
        before, _ = _read(binary)
        clean, removed = _owned_lines(before.splitlines())
        if not removed:
            return
        new_text = "\n".join(clean) + ("\n" if clean else "")
        current, _ = _read(binary)
        if current != before:
            raise ScheduleError(
                "Crontab changed during schedule update; no changes were made. Retry after reviewing it."
            )
        if clean:
            result = _invoke(binary, "-", input_text=new_text)
        else:
            result = _invoke(binary, "-r")
        if result.returncode:
            raise ScheduleError("Could not remove the managed schedule; crontab was left unchanged.")
