PYTHON ?= python3
VENV := .venv

.PHONY: dev lint format-check test check build deb source

dev:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -e '.[dev]'

lint:
	$(VENV)/bin/ruff check calendar_busy_sync tests packaging debian/calendar-busy-sync-cli

format-check:
	$(VENV)/bin/ruff format --check calendar_busy_sync tests packaging debian/calendar-busy-sync-cli

test:
	$(VENV)/bin/python -m unittest discover -s tests -v

check: lint format-check test

build:
	$(VENV)/bin/python -m build

deb:
	$(PYTHON) packaging/build.py binary

source:
	$(PYTHON) packaging/build.py source
