UV ?= uv
UV_RUN := $(UV) run
CODEX_HOME ?= $(HOME)/.codex
CODEX_SKILLS_DIR ?= $(CODEX_HOME)/skills
CODEX_SKILLS := x-intelligence-reporting arxiv-intelligence-reporting

# uv owns the project environment; loopctl remains the real interface and these
# targets are thin, self-documenting wrappers around its installed entry point.
LOOPCTL := $(UV_RUN) loopctl

.PHONY: help test compile validate-skills install-codex install-codex-skills \
	snapshot-following daily-x-intel discover-follows daily-arxiv-intel \
	run apply validate status logs list fleet check deps init auth

help:           ## list available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

# --- loopcraft control plane --------------------------------------------------
run:            ## run one loop now:  make run LOOP=slack-triage
	$(LOOPCTL) run $(LOOP)

init:           ## bootstrap the memory tree + check the source tree (M2)
	$(LOOPCTL) init

auth:           ## report credential status + guidance for the fleet's deps (M2)
	$(LOOPCTL) auth

apply:          ## validate the fleet + render systemd units into <memory>/var/systemd (M2)
	$(LOOPCTL) apply ./loops

validate:       ## validate all manifests without deploying
	$(LOOPCTL) validate ./loops

status:         ## fleet status in the terminal
	$(LOOPCTL) status

logs:           ## tail a loop's last run:  make logs LOOP=slack-triage
	$(LOOPCTL) logs $(LOOP)

list:           ## list known loops
	$(LOOPCTL) list

fleet:          ## show all loops in a formatted table (schedule, last run, install state)
	$(LOOPCTL) fleet

check:          ## verify deps + manifests + unit rendering without deploying
	$(LOOPCTL) deps check && $(LOOPCTL) apply ./loops --dry-run --skip-preflight

deps:           ## probe required runtimes/tools on PATH
	$(LOOPCTL) deps check

# --- tests / build ------------------------------------------------------------
test:           ## run the loopcraft unit tests
	$(UV_RUN) pytest -q

compile:        ## byte-compile sources + tests
	$(UV_RUN) python -m compileall -q src tests

# --- Codex skills -------------------------------------------------------------
validate-skills:
	@for skill in $(CODEX_SKILLS); do \
		$(UV_RUN) python $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py skills/$$skill; \
	done

install-codex: install-codex-skills

install-codex-skills:
	@mkdir -p $(CODEX_SKILLS_DIR)
	@for skill in $(CODEX_SKILLS); do \
		echo "Installing $$skill into $(CODEX_SKILLS_DIR)/$$skill"; \
		mkdir -p $(CODEX_SKILLS_DIR)/$$skill; \
		cp -R skills/$$skill/. $(CODEX_SKILLS_DIR)/$$skill/; \
		$(UV_RUN) python $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py $(CODEX_SKILLS_DIR)/$$skill; \
	done

# --- intelligence loops / direct CLI entry points -----------------------------
ARXIV_CONFIG ?= $(if $(wildcard config/arxiv_intel.local.yaml),config/arxiv_intel.local.yaml,config/arxiv_intel.yaml)
X_CONFIG ?= $(if $(wildcard config/x_intel.local.yaml),config/x_intel.local.yaml,config/x_intel.yaml)

snapshot-following:
	$(UV_RUN) loopcraft-x-intel snapshot-following

daily-x-intel:  ## fetch + rank a daily X digest into the memory ledger
	$(UV_RUN) loopcraft-x-intel run --config $(X_CONFIG)

discover-follows:
	$(UV_RUN) loopcraft-x-intel discover-follows --config $(X_CONFIG)

daily-arxiv-intel:  ## fetch + rank a daily arXiv digest into the memory ledger
	$(UV_RUN) loopcraft-arxiv-intel run --config $(ARXIV_CONFIG)
