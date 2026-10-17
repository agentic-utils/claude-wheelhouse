.DEFAULT_GOAL := help
.PHONY: help args install run stop tutorial test stamp uv-check

# where Astral's installer puts uv, which isn't on PATH until a new shell
export PATH := $(PATH):$(HOME)/.local/bin
# make install's last step, holding the modules it installed: make run installs first when
# it's missing, or older than these
STAMP := .venv/.wheelhouse-installed
# the modules in this repo (modules/), each its own package and dependency group; make install
# installs them all with the wheelhouse, or those named: WITH="" for none (.plan/modules.md)
MODULES := $(notdir $(wildcard modules/*))
WITH ?= $(MODULES)
# the app runs on what make install installed: a plain uv run would add back a module left out
RUN := uv run --no-sync

help: ## List the available commands (make help args: the app's command line args)
ifeq ($(filter args,$(MAKECMDGOALS)),)
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
else
	@:
endif

args: uv-check $(STAMP) ## List the app's command line args, for make run ARGS="..."
	@$(RUN) claude-wheelhouse args

install: ## Install, or reinstall after an update, with every module in modules/ or those in WITH="..." (WITH="" for none). Installs uv if it's missing, then uv sync, refreshing the packages' entry points
	@command -v uv >/dev/null 2>&1 || { \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		command -v uv >/dev/null 2>&1 || { echo "uv didn't install: see https://docs.astral.sh/uv/" >&2; exit 1; }; \
		echo "uv is installed in ~/.local/bin: open a new shell to have it on your PATH"; }
	@for m in $(WITH); do test -d modules/$$m || { echo "no module $$m: modules/ has $(MODULES)" >&2; exit 1; }; done
	uv sync --reinstall-package claude-wheelhouse $(foreach m,$(WITH),--reinstall-package claude-wheelhouse-$(m)) \
		$(foreach m,$(filter-out $(WITH),$(MODULES)),--no-group $(m))
	@echo "$(WITH)" > $(STAMP)

$(STAMP): pyproject.toml uv.lock $(wildcard modules/*/pyproject.toml) | uv-check
	@$(MAKE) --no-print-directory install WITH="$$(cat $(STAMP) 2>/dev/null || echo '$(MODULES)')"

run: uv-check $(STAMP) ## Run the wheelhouse app, installing first if needed (ARGS="..." passes args: make args lists them)
	$(RUN) claude-wheelhouse $(ARGS)

stop: uv-check ## Stop the daemon (quit the app first, or it starts a new one)
	$(RUN) claude-wheelhouse daemon --stop

tutorial: uv-check ## Have the next make run offer the tutorial again, ending any earlier tutorial session (quit the app and make stop first)
	$(RUN) claude-wheelhouse tutorial

test: uv-check ## Run the tests
	uv run pytest

stamp: uv-check ## Give every UNRELEASED feature in whatsnew.py a release's version (make stamp VERSION=0.2.0), as a release commit does
	@test -n "$(VERSION)" || { echo "make stamp VERSION=0.2.0"; exit 1; }
	$(RUN) python -m claude_wheelhouse.whatsnew stamp $(VERSION)

uv-check:
	@command -v uv >/dev/null 2>&1 || { echo "uv not found: run make install" >&2; exit 1; }
