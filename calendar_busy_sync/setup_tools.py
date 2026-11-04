"""Consent-gated, short-lived cloud CLI installations for registration."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import SyncError


@dataclass
class SetupTool:
    executable: str
    environment: dict[str, str]


_GCLOUD_VERSION = "592.0.0"
_GCLOUD_RELEASE_BASE = "https://storage.googleapis.com/cloud-sdk-release"
# Versioned and rapid gzip bytes differ; these pins match the tar payloads of
# Google's SHA256-verified rapid archives.
_GCLOUD_ARCHIVES = {
    "x86_64": ("x86_64", "2d841a23a681ab94486af6841e5f1cd123cd300fb8c4be6ba5b51350fca3d50b"),
    "aarch64": ("arm", "ba588759c4c42122a8c14536009738c0f0030989ca75788235f9956555e44f4d"),
    "i686": ("x86", "d116c211ac91d11f1f039ffc40827b2a49c50806d23330f1e0d001cf399ee503"),
}
_MAX_ARCHIVE_BYTES = 130 * 1024 * 1024
_AZURE_CLI_VERSION = "2.92.0"


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "calendar-busy-sync-setup"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_ARCHIVE_BYTES:
                    raise SyncError("Cloud CLI download exceeded its size limit.")
                output.write(chunk)
    except OSError:
        raise SyncError(
            "Cloud CLI download failed. Use manual registration or try setup again later."
        ) from None


def _private_environment(root: Path, name: str) -> dict[str, str]:
    environment = os.environ.copy()
    if name == "gcloud":
        config_variable = "CLOUDSDK_CONFIG"
    else:
        config_variable = "AZURE_CONFIG_DIR"
    environment[config_variable] = str(root / "admin-config")
    environment["XDG_CACHE_HOME"] = str(root / "cache")
    environment["TMPDIR"] = str(root / "scratch")
    environment["CLOUDSDK_CORE_DISABLE_USAGE_REPORTING"] = "true"
    environment["CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK"] = "true"
    environment["AZURE_CORE_COLLECT_TELEMETRY"] = "no"
    environment["AZURE_CORE_CHECK_VERSION"] = "false"
    environment["PIP_CONFIG_FILE"] = os.devnull
    for directory in ("admin-config", "cache", "scratch"):
        (root / directory).mkdir(mode=0o700)
    return environment


def _install_gcloud(root: Path) -> str:
    if sys.platform != "linux":
        raise SyncError("Temporary gcloud downloads are supported only on Linux; use manual registration.")
    release = _GCLOUD_ARCHIVES.get(platform.machine())
    if release is None:
        raise SyncError(
            "Temporary gcloud download is unavailable for this architecture; use manual registration."
        )
    if not hasattr(tarfile, "data_filter"):
        raise SyncError("Safe gcloud extraction requires an updated Python with tarfile data-filter support.")
    architecture, expected_digest = release
    archive = root / "gcloud.tar.gz"
    filename = f"google-cloud-cli-{_GCLOUD_VERSION}-linux-{architecture}.tar.gz"
    _download(f"{_GCLOUD_RELEASE_BASE}/{filename}", archive)
    digest = hashlib.sha256()
    with archive.open("rb") as downloaded:
        while chunk := downloaded.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_digest:
        raise SyncError("The downloaded gcloud archive failed its pinned SHA256 check.")
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(root, filter="data")
    except (OSError, tarfile.TarError):
        raise SyncError("The gcloud archive could not be extracted safely.") from None
    executable = root / "google-cloud-sdk" / "bin" / "gcloud"
    if not executable.is_file():
        raise SyncError("The gcloud archive did not contain its expected executable.")
    return str(executable)


def _install_az(root: Path, environment: dict[str, str]) -> str:
    virtualenv = root / "venv"
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(virtualenv)],
            env=environment,
            check=True,
            timeout=120,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise SyncError(
            "Temporary Azure CLI setup requires Python venv/pip support. On Debian/Ubuntu, install python3-venv manually or use browser registration."
        ) from None
    try:
        subprocess.run(
            [
                str(virtualenv / "bin" / "python"),
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--index-url",
                "https://pypi.org/simple",
                f"azure-cli=={_AZURE_CLI_VERSION}",
            ],
            env=environment,
            check=True,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise SyncError(
            "Temporary Azure CLI installation failed. Use browser registration or install Azure CLI yourself."
        ) from None
    return str(virtualenv / "bin" / "az")


@contextmanager
def temporary_cli(name: str, confirm: Callable[[str], bool]) -> Iterator[SetupTool | None]:
    if name not in {"gcloud", "az"}:
        raise ValueError("Unknown setup tool.")
    found = shutil.which(name)
    if not found:
        if name == "gcloud":
            distribution = f"Google Cloud SDK {_GCLOUD_VERSION} from Google's verified Linux archive"
        else:
            distribution = f"Azure CLI {_AZURE_CLI_VERSION} and its dependencies from PyPI in a private Python environment"
        if not confirm(
            f"{name} is missing. Download and install {distribution} temporarily for registration? "
            "Its files and administrative login data will be removed when this helper finishes."
        ):
            yield None
            return
    with tempfile.TemporaryDirectory(prefix="calendar-busy-sync-") as directory:
        root = Path(directory)
        if any((parent / ".git").exists() for parent in (root, *root.parents)):
            raise SyncError(
                "Temporary registration files must be outside Git checkouts; choose another TMPDIR."
            )
        environment = _private_environment(root, name)
        try:
            if found:
                executable = found
            elif name == "gcloud":
                executable = _install_gcloud(root)
            else:
                executable = _install_az(root, environment)
        except OSError:
            raise SyncError(
                "Temporary CLI installation could not complete; use manual registration."
            ) from None
        yield SetupTool(executable, environment)
