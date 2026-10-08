.DEFAULT_GOAL := help
.PHONY: help install run tutorial test

help: ## List the available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install, or reinstall after an update (uv sync, refreshing this package's entry points)
	uv sync --reinstall-package claude-wheelhouse

run: ## Run the wheelhouse app
	uv run claude-wheelhouse

tutorial: ## Start the tutorial afresh (ending any earlier one) and open the wheelhouse
	uv run claude-wheelhouse tutorial

test: ## Run the tests
	uv run pytest
