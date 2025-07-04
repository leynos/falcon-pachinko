MDLINT ?= $(shell which markdownlint)
NIXIE ?= $(shell which nixie)
MDFORMAT_ALL ?= $(shell which mdformat-all)
TOOLS = $(MDFORMAT_ALL) ruff ty $(MDLINT) $(NIXIE) uv
VENV_TOOLS = pytest

.PHONY: help all clean build build-release lint fmt check-fmt \
	markdownlint nixie test typecheck $(TOOLS) $(VENV_TOOLS)

.DEFAULT_GOAL := all

all: build check-fmt test typecheck

build: uv ## Build virtual-env and install deps
	uv venv
	uv sync --group dev --group examples

build-release: ## Build artefacts (sdist & wheel)
	python -m build --sdist --wheel

clean: ## Remove build artifacts
	rm -rf build dist *.egg-info \
	  .mypy_cache .pytest_cache .coverage coverage.* \
	  lcov.info htmlcov .venv
	find . -type d -name '__pycache__' -print0 | xargs -0 -r rm -rf

define ensure_tool
	@command -v $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required, but not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

define ensure_tool_venv
	@uv run --which $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required in the virtualenv, but is not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)

$(VENV_TOOLS): ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)

fmt: ruff $(MDFORMAT_ALL) ## Format sources
	ruff format
	ruff check --select I --fix
	$(MDFORMAT_ALL)

check-fmt: ruff ## Verify formatting
	ruff format --check
	# mdformat-all doesn't currently do checking

lint: ruff ## Run linters
	ruff check

typecheck: build ty ## Run typechecking
	ty check

markdownlint: $(MDLINT) ## Lint Markdown files
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -print0 | xargs -0 $(MDLINT)

nixie: $(NIXIE) ## Validate Mermaid diagrams
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -print0 | xargs -0 $(NIXIE)

test: build uv pytest ## Run tests
	uv run pytest -v

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
