PY ?= python3.11
VENV := .venv
PYTHON := $(VENV)/bin/python

.PHONY: install check-models fleet fleet-fg fleet-status orchestrator server autopilot autopilot-fg stop mock-events \
	web web-build report test test-fast kill-orchestrator

install:
	$(PY) -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	mkdir -p data/logs

check-models:
	$(PYTHON) -m scripts.check_models

# Starts every node plus the traffic generator and monitor in the background (log: data/logs/fleet.log).
fleet:
	mkdir -p data/logs
	$(PYTHON) -m understudy.fleet.runner --detach

fleet-fg:
	$(PYTHON) -m understudy.fleet.runner

fleet-status:
	$(PYTHON) -m understudy.fleet.nodectl status

# Nodes, traffic, monitor, agents and the UI API in one process, in the foreground. No scheduled faults.
orchestrator:
	$(PYTHON) -m understudy.orchestrator

server: orchestrator

# The same, in the background (log: data/logs/orchestrator.log), with a random training fault every 20-40s and a
# storm about every 5 minutes. Nodes keep running if the orchestrator dies; run this again to resume.
autopilot:
	mkdir -p data/logs
	$(PYTHON) -m understudy.orchestrator --autopilot --detach

autopilot-fg:
	$(PYTHON) -m understudy.orchestrator --autopilot

# The bracketed first letter keeps pkill from matching this recipe's own shell.
stop:
	pkill -f '[u]nderstudy\.orchestrator' || true
	sleep 2
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

# Everything except the tests that call the real API.
test-fast:
	$(PYTHON) -m pytest -q -m "not live"
