# portfolio-dash — dev tasks. Always uses the repo venv interpreter.
PY := ./.venv/Scripts/python.exe

# Live SQLite DB path. Mirrors shared/config.py (db_path default = data/portfolio.db,
# overridable via the DB_PATH env var). Override on the command line: make restore FILE=bak.db DB=other.db
DB ?= data/portfolio.db

.PHONY: run test contract e2e regress all mypy ruff restore

run:
	$(PY) -m uvicorn portfolio_dash.api.app:create_app --factory --port 8400

# Full suite minus browser e2e (sockets). This is real regression — every
# tests/<module>/ tree, not just unit + contract.
test:
	$(PY) -m pytest tests --ignore=tests/e2e -q

contract:
	$(PY) -m pytest tests/contract -q

e2e:
	$(PY) -m pytest tests/e2e -q

# Whole tree minus e2e (the primary regression gate).
regress:
	$(PY) -m pytest tests --ignore=tests/e2e -q

mypy:
	$(PY) -m mypy portfolio_dash --strict

ruff:
	$(PY) -m ruff check portfolio_dash tests

all: ruff mypy test
	@echo "make all: green"

# Ops convenience: restore the live SQLite DB from a backup file.
# Usage: make restore FILE=path/to/backup.db   (optionally DB=path/to/live.db)
# There is no programmatic scheduler-stop hook — STOP the running server/scheduler
# FIRST so nothing holds a write lock or overwrites the swap, THEN run this target.
restore:
ifndef FILE
	$(error FILE is required: make restore FILE=path/to/backup.db [DB=path/to/live.db])
endif
	@test -f "$(FILE)" || { echo "restore: backup file '$(FILE)' not found" >&2; exit 1; }
	cp "$(FILE)" "$(DB)"
	@echo "restore: copied $(FILE) -> $(DB) (restart the server to pick it up)"
