.DEFAULT_GOAL := help
.PHONY: help install run stop tutorial test stamp

help: ## List the available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install, or reinstall after an update (uv sync, refreshing this package's entry points)
	uv sync --reinstall-package claude-wheelhouse

run: ## Run the wheelhouse app
	uv run claude-wheelhouse

stop: ## Stop the daemon (quit the app first, or it starts a new one)
	uv run claude-wheelhouse daemon --stop

tutorial: ## Have the next make run offer the tutorial again, ending any earlier tutorial session (quit the app and make stop first)
	uv run claude-wheelhouse tutorial

test: ## Run the tests
	uv run pytest

stamp: ## Give every UNRELEASED feature in whatsnew.py a release's version (make stamp VERSION=0.2.0), as a release commit does
	@test -n "$(VERSION)" || { echo "make stamp VERSION=0.2.0"; exit 1; }
	uv run python -m claude_wheelhouse.whatsnew stamp $(VERSION)
