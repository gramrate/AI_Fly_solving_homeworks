# Coding agent: shortcuts for setup, running, recovery and tests.
# Variables can be overridden: make run MODE=safe TASK="..."

PYTHON  ?= python3
VENV    ?= .venv
CONFIG  ?= config.yaml
MODE    ?= auto
TASK    ?= Исправь расчёт итоговой стоимости заказа, чтобы тесты проходили. Тесты не меняй. Сам найди нужные файлы, внеси исправление и запусти тесты.
RUN     ?=
REASON  ?=
CHOICE  ?= retry
ATTR    ?=

# Use the venv interpreter when it exists, otherwise the system one.
PY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(PYTHON))
AGENT := $(PY) -m agent --config $(CONFIG)

.DEFAULT_GOAL := help
.PHONY: help venv install config run run-safe run-plan models list status resume \
        approve deny plan plan-approve plan-reject resolve tasks diff rollback crash-demo test test-workspace \
        reset reset-workspace clean clean-runs

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Variables: MODE=$(MODE) MODEL=$(MODEL) CONFIG=$(CONFIG) RUN=<run_id> REASON=... CHOICE=retry|fail|abort ATTR=yes|no"

# ---------- setup ----------

venv: ## Create .venv and install dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

install: ## Install dependencies into the current interpreter
	$(PY) -m pip install -r requirements.txt

config: ## Create config.yaml from config.example.yaml (then put your API key into it)
	@test -e config.yaml && echo "config.yaml already exists" || (cp config.example.yaml config.yaml && echo "created config.yaml")

models: ## List model ids available at the configured API (needs llm.api_key in config.yaml)
	$(AGENT) models

# ---------- runs ----------

run: ## Start a run: make run [MODE=auto|safe] [TASK="..."]
	$(AGENT) run "$(TASK)" --mode $(MODE)

run-safe: ## Start a run in safe mode (writes and bash need approval)
	$(AGENT) run "$(TASK)" --mode safe

run-plan: ## Start a run in Plan Mode
	$(AGENT) run "$(TASK)" --mode $(MODE) --plan

list: ## List all runs
	$(AGENT) list

status: need-run ## Show run status: make status RUN=<id>
	$(AGENT) status $(RUN)

resume: need-run ## Continue a run after restart/crash
	$(AGENT) resume $(RUN)

approve: need-run ## Approve pending action
	$(AGENT) approve $(RUN)

deny: need-run ## Deny pending action: make deny RUN=<id> REASON="..."
	$(AGENT) deny $(RUN) $(if $(REASON),--reason "$(REASON)")

plan: need-run ## Show the structured plan
	$(AGENT) plan $(RUN)

plan-approve: need-run ## Approve the plan and start execution
	$(AGENT) plan-approve $(RUN)

plan-reject: need-run ## Reject the plan: make plan-reject RUN=<id> REASON="..."
	$(AGENT) plan-reject $(RUN) $(if $(REASON),--reason "$(REASON)")

resolve: need-run ## Settle uncertain call: make resolve RUN=<id> CHOICE=retry|fail|abort [ATTR=yes|no]
	$(AGENT) resolve $(RUN) $(CHOICE) $(if $(ATTR),--attribute $(ATTR))

tasks: need-run ## Show task states
	$(AGENT) tasks $(RUN)

diff: need-run ## Show file changes of a run
	$(AGENT) diff $(RUN)

rollback: need-run ## Roll back file changes of a run
	$(AGENT) rollback $(RUN)

crash-demo: ## Start a run that crashes after the first side effect (then: make resume RUN=<id>)
	-AGENT_FAULT=after_execute $(AGENT) run "$(TASK)" --mode auto

need-run:
	@test -n "$(RUN)" || (echo "RUN is required, e.g. make $(MAKECMDGOALS) RUN=<run_id> (see: make list)" && exit 1)

# ---------- tests ----------

test: ## Run the agent runtime tests (no model needed)
	$(PY) -m unittest discover -s tests -v

test-workspace: ## Run the control project's tests
	cd workspace && $(PY) -m unittest -v

reset: ## Reset the control task: original buggy order.py and untouched test_order.py
	printf '%s\n' \
	  'def calculate_total(prices: list[float], discount_percent: float) -> float:' \
	  '    return sum(prices) - discount_percent' > workspace/order.py
	printf '%s\n' \
	  'import unittest' \
	  '' \
	  'from order import calculate_total' \
	  '' \
	  '' \
	  'class OrderTest(unittest.TestCase):' \
	  '    def test_discount(self) -> None:' \
	  '        self.assertEqual(calculate_total([100, 50], 10), 135)' \
	  '' \
	  '    def test_invalid_discount(self) -> None:' \
	  '        for value in (-1, 101):' \
	  '            with self.assertRaises(ValueError):' \
	  '                calculate_total([100], value)' > workspace/test_order.py

reset-workspace: reset

# ---------- cleanup ----------

clean: ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

clean-runs: ## Delete all saved runs (state, snapshots, journal)
	rm -rf runs
