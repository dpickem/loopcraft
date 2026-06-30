PYTHON ?= python
CODEX_HOME ?= $(HOME)/.codex
CODEX_SKILLS_DIR ?= $(CODEX_HOME)/skills
CODEX_SKILLS := x-intelligence-reporting arxiv-intelligence-reporting

# loopctl is the real interface; these targets are thin, self-documenting wrappers.
LOOPCTL := PYTHONPATH=src $(PYTHON) -m loopcraft.cli

.PHONY: help test compile validate-skills install-codex install-codex-skills \
	snapshot-following daily-x-intel discover-follows daily-arxiv-intel \
	run apply validate status logs list check deps

help:           ## list available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

# --- loopcraft control plane (M1) ---------------------------------------------
run:            ## run one loop now:  make run LOOP=slack-triage
	$(LOOPCTL) run $(LOOP)

apply:          ## validate + (re)deploy all manifests in loops/ (deploy lands in M2)
	$(LOOPCTL) apply ./loops

validate:       ## validate all manifests without deploying
	$(LOOPCTL) validate ./loops

status:         ## fleet status in the terminal
	$(LOOPCTL) status

logs:           ## tail a loop's last run:  make logs LOOP=slack-triage
	$(LOOPCTL) logs $(LOOP)

list:           ## list known loops
	$(LOOPCTL) list

check:          ## verify deps + manifests without deploying
	$(LOOPCTL) deps check && $(LOOPCTL) apply ./loops --dry-run

deps:           ## probe required runtimes/tools on PATH
	$(LOOPCTL) deps check

# --- tests / build ------------------------------------------------------------
test:           ## run the loopcraft unit tests
	PYTHONPATH=src $(PYTHON) -m pytest -q

compile:        ## byte-compile sources + tests
	PYTHONPATH=src $(PYTHON) -m compileall -q src tests

# --- Codex skills -------------------------------------------------------------
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

# --- intel prototypes (L2 sources; migrated onto the unified store in M2) ------
ARXIV_CONFIG ?= config/arxiv_intel.json
X_CONFIG ?= config/x_intel.json

snapshot-following:
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli snapshot-following

daily-x-intel:  ## fetch + rank a daily X digest
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli run --config $(X_CONFIG)

discover-follows:
	PYTHONPATH=src $(PYTHON) -m loopcraft.x_intel.cli discover-follows --config $(X_CONFIG)

daily-arxiv-intel:  ## fetch + rank a daily arXiv digest
	PYTHONPATH=src $(PYTHON) -m loopcraft.arxiv_intel.cli run --config $(ARXIV_CONFIG)
