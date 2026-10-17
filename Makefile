.DEFAULT_GOAL := help
.PHONY: help args install run stop tutorial test stamp uv-check

# where Astral's installer puts uv, which isn't on PATH until a new shell
export PATH := $(PATH):$(HOME)/.local/bin
# make install's last step: make run installs first when it's missing, or older than these
STAMP := .venv/.wheelhouse-installed

help: ## List the available commands (make help args: the app's command line args)
ifeq ($(filter args,$(MAKECMDGOALS)),)
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
else
	@:
endif

args: uv-check $(STAMP) ## List the app's command line args, for make run ARGS="..."
	@uv run claude-wheelhouse args

install: ## Install, or reinstall after an update (installs uv if it's missing, then uv sync, refreshing this package's entry points)
	@command -v uv >/dev/null 2>&1 || { \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		command -v uv >/dev/null 2>&1 || { echo "uv didn't install: see https://docs.astral.sh/uv/" >&2; exit 1; }; \
		echo "uv is installed in ~/.local/bin: open a new shell to have it on your PATH"; }
	uv sync --reinstall-package claude-wheelhouse
	@touch $(STAMP)

$(STAMP): pyproject.toml uv.lock | uv-check
	@$(MAKE) --no-print-directory install

run: uv-check $(STAMP) ## Run the wheelhouse app, installing first if needed (ARGS="..." passes args: make args lists them)
	uv run claude-wheelhouse $(ARGS)

stop: uv-check ## Stop the daemon (quit the app first, or it starts a new one)
	uv run claude-wheelhouse daemon --stop

tutorial: uv-check ## Have the next make run offer the tutorial again, ending any earlier tutorial session (quit the app and make stop first)
	uv run claude-wheelhouse tutorial

test: uv-check ## Run the tests
	uv run pytest

stamp: uv-check ## Give every UNRELEASED feature in whatsnew.py a release's version (make stamp VERSION=0.2.0), as a release commit does
	@test -n "$(VERSION)" || { echo "make stamp VERSION=0.2.0"; exit 1; }
	uv run python -m claude_wheelhouse.whatsnew stamp $(VERSION)

uv-check:
	@command -v uv >/dev/null 2>&1 || { echo "uv not found: run make install" >&2; exit 1; }
