PYTHON ?= python
CODEX_HOME ?= $(HOME)/.codex
CODEX_SKILLS_DIR ?= $(CODEX_HOME)/skills

.PHONY: test compile validate-skills install-codex install-codex-skills snapshot-following daily-x-intel discover-follows

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q

compile:
	PYTHONPATH=src $(PYTHON) -m compileall -q src tests

validate-skills:
	$(PYTHON) $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py skills/x-intelligence-reporting

install-codex: install-codex-skills

install-codex-skills:
	mkdir -p $(CODEX_SKILLS_DIR)/x-intelligence-reporting
	cp -R skills/x-intelligence-reporting/. $(CODEX_SKILLS_DIR)/x-intelligence-reporting/
	$(PYTHON) $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py $(CODEX_SKILLS_DIR)/x-intelligence-reporting

snapshot-following:
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli snapshot-following

daily-x-intel:
	./scripts/daily_x_intel.sh

discover-follows:
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli discover-follows --config config/x_intel.json

