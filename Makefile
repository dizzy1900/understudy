PY ?= python3.11
VENV := .venv
PYTHON := $(VENV)/bin/python

.PHONY: install check-models fleet orchestrator server autopilot stop mock-events \
	web web-build report test kill-orchestrator

install:
	$(PY) -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	mkdir -p data/logs

check-models:
	$(PYTHON) -m scripts.check_models

fleet:
	$(PYTHON) -m understudy.fleet.supervisor up

orchestrator:
	$(PYTHON) -m understudy.orchestrator

server:
	$(PYTHON) -m uvicorn understudy.server:app --host 127.0.0.1 --port 8000

autopilot:
	$(PYTHON) -m understudy.orchestrator --autopilot

# The bracketed first letter keeps pkill from matching this recipe's own shell.
stop:
	pkill -f '[u]nderstudy\.orchestrator' || true
	pkill -f '[u]vicorn understudy\.server' || true
	pkill -f '[u]nderstudy\.fleet' || true
	pkill -f '[s]cripts\.mock_events' || true

kill-orchestrator:
	pkill -9 -f '[u]nderstudy\.orchestrator' || true

mock-events:
	$(PYTHON) -m scripts.mock_events

web: web/node_modules
	cd web && npm run dev

web-build: web/node_modules
	cd web && npm run build

web/node_modules: web/package-lock.json
	cd web && npm ci
	touch web/node_modules

report:
	$(PYTHON) -m scripts.report

test:
	$(PYTHON) -m pytest -q
