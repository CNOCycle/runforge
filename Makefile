# Global configuration.
ROOT_DIR := $(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
PYTHON   ?= python3

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "%-20s %s\n", $$1, $$2}'

install-dev: ## Install RunForge in editable mode with development tools
	$(PYTHON) -m pip install -e '$(ROOT_DIR)[dev]'

format: ## Rewrite Python files using Ruff's formatter
	cd "$(ROOT_DIR)" && $(PYTHON) -m ruff format .

format-check: ## Verify formatting without modifying files
	cd "$(ROOT_DIR)" && $(PYTHON) -m ruff format --check .

lint: ## Run Ruff lint checks without applying fixes
	cd "$(ROOT_DIR)" && $(PYTHON) -m ruff check .

test: ## Run the pytest suite
	cd "$(ROOT_DIR)" && $(PYTHON) -m pytest

check: format-check lint test ## Run format-check, lint, and test

.PHONY: help install-dev format format-check lint test check
