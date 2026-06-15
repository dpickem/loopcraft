PYTHON ?= python
CODEX_HOME ?= $(HOME)/.codex
CODEX_SKILLS_DIR ?= $(CODEX_HOME)/skills
CODEX_SKILLS := x-intelligence-reporting arxiv-intelligence-reporting

.PHONY: test compile validate-skills install-codex install-codex-skills snapshot-following daily-x-intel discover-follows daily-arxiv-intel

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q

compile:
	PYTHONPATH=src $(PYTHON) -m compileall -q src tests

validate-skills:
	@for skill in $(CODEX_SKILLS); do \
		$(PYTHON) $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py skills/$$skill; \
	done

install-codex: install-codex-skills

install-codex-skills:
	@mkdir -p $(CODEX_SKILLS_DIR)
	@for skill in $(CODEX_SKILLS); do \
		echo "Installing $$skill into $(CODEX_SKILLS_DIR)/$$skill"; \
		mkdir -p $(CODEX_SKILLS_DIR)/$$skill; \
		cp -R skills/$$skill/. $(CODEX_SKILLS_DIR)/$$skill/; \
		$(PYTHON) $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py $(CODEX_SKILLS_DIR)/$$skill; \
	done

snapshot-following:
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli snapshot-following

daily-x-intel:
	./scripts/daily_x_intel.sh

discover-follows:
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli discover-follows --config config/x_intel.json

daily-arxiv-intel:
	./scripts/daily_arxiv_intel.sh
