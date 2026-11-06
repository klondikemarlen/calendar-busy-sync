"""Build binary/source Debian artifacts without root, credentials or checkout state."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("binary", "source"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = root / "dist"
    output.mkdir(exist_ok=True)
    version = subprocess.run(
        ["dpkg-parsechangelog", "--show-field", "Version"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="debian-build-", dir=output) as temporary:
        build_root = Path(temporary)
        source = build_root / f"calendar-busy-sync-{version}"
        source.mkdir()
        # Public source inputs only; never copy an arbitrary checkout/credential file.
        for name in ("calendar_busy_sync", "debian", "docs", "packaging", "tests"):
            shutil.copytree(root / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "Makefile", "README.md", "CHANGELOG.md", "LICENSE"):
            shutil.copy2(root / name, source / name)
        command = ["dpkg-buildpackage", "--no-sign", "--build=" + args.kind]
        subprocess.run(command, cwd=source, check=True)
        for artifact in build_root.iterdir():
            if artifact.is_file():
                shutil.copy2(artifact, output / artifact.name)
                print(output / artifact.name)


if __name__ == "__main__":
    main()
