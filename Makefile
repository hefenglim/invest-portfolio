# portfolio-dash — dev tasks. Always uses the repo venv interpreter.
PY := ./.venv/Scripts/python.exe

.PHONY: run test contract e2e regress all mypy ruff

run:
	$(PY) -m uvicorn portfolio_dash.api.app:create_app --factory --port 8400

test:
	$(PY) -m pytest tests/unit tests/contract -q

contract:
	$(PY) -m pytest tests/contract -q

e2e:
	$(PY) -m pytest tests/e2e -q

regress:
	$(PY) -m pytest tests/contract -q -k golden

mypy:
	$(PY) -m mypy portfolio_dash --strict

ruff:
	$(PY) -m ruff check portfolio_dash tests

all: ruff mypy test
	@echo "make all: green"
