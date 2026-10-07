.DEFAULT_GOAL := help
.PHONY: help install run test

help: ## List the available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (uv sync)
	uv sync

run: ## Run the wheelhouse app
	uv run claude-wheelhouse

test: ## Run the tests
	uv run pytest
