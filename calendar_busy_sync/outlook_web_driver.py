"""Own a Firefox process and speak standard WebDriver over loopback only."""

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from .models import SyncError

_ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
_STARTUP_ERROR = (
    "Could not start the Outlook web browser. Install Firefox and a compatible geckodriver on PATH, "
    "and make sure Firefox can access the isolated Outlook profile directory."
)
_BROWSER_ERROR = "Outlook web browser communication failed; no uncertain action will be retried."


class Firefox:
    def __init__(self, port: int):
        self.endpoint = f"http://127.0.0.1:{port}"
        self.opener = build_opener(ProxyHandler({}))
        self.session = None

    def request(self, path, payload=None, *, method=None, timeout=30):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.endpoint + path, data=data, headers={"Content-Type": "application/json"}, method=method
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)["value"]
        except (OSError, ValueError, KeyError, URLError):
            raise SyncError(_BROWSER_ERROR) from None

    def navigate(self, url: str):
        self.request(f"/session/{self.session}/url", {"url": url})

    def execute(self, script, *args):
        return self.request(f"/session/{self.session}/execute/sync", {"script": script, "args": args})

    def type_text(self, element, text: str):
        self.request(f"/session/{self.session}/element/{element[_ELEMENT]}/value", {"text": text})


def _await_driver(browser: Firefox, process: subprocess.Popen):
    deadline = monotonic() + 10
    while process.poll() is None and monotonic() < deadline:
        try:
            if browser.request("/status", timeout=1).get("ready"):
                return
        except SyncError:
            sleep(0.1)
    raise SyncError(_STARTUP_ERROR)


@contextmanager
def open_firefox(profile: Path, *, headless: bool) -> Iterator[Firefox]:
    executable = shutil.which("geckodriver")
    if not executable or not shutil.which("firefox"):
        raise SyncError(_STARTUP_ERROR)
    try:
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        if profile.is_symlink() or profile.stat().st_uid != os.getuid():
            raise SyncError("The isolated Outlook profile must be a directory owned by the current user.")
        profile.chmod(0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [executable, "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise SyncError(_STARTUP_ERROR) from None

    browser = Firefox(port)
    try:
        _await_driver(browser, process)
        args = ["-profile", str(profile.resolve())]
        if headless:
            args.insert(0, "-headless")
        try:
            result = browser.request(
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "browserName": "firefox",
                            "acceptInsecureCerts": False,
                            "moz:firefoxOptions": {"args": args, "prefs": {"intl.accept_languages": "en-US"}},
                        }
                    }
                },
                timeout=45,
            )
            browser.session = result["sessionId"]
            browser.request(f"/session/{browser.session}/window/rect", {"width": 1600, "height": 1000})
        except (SyncError, KeyError, TypeError):
            raise SyncError(_STARTUP_ERROR) from None
        yield browser
    finally:
        if browser.session:
            with suppress(SyncError):
                browser.request(f"/session/{browser.session}", method="DELETE", timeout=5)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
