.PHONY: help default all clean build build-release lint fmt check-fmt \
       markdownlint tools nixie test typecheck

MDLINT ?= markdownlint
NIXIE ?= nixie

all: build check-fmt test typecheck

default: build

build: ## Build virtual-env and install deps
	uv venv
	uv sync --group dev --group examples

build-release: ## Build artefacts (sdist & wheel)
	python -m build --sdist --wheel

clean: ## Remove build artifacts
	rm -rf build dist *.egg-info \
	  .mypy_cache .pytest_cache .coverage coverage.* lcov.info htmlcov \
	  .venv
	find . -type d -name '__pycache__' -exec rm -rf '{}' +

define ensure_tool
$(if $(shell uv run --which $(1) >/dev/null 2>&1 && echo y),,\
$(error $(1) is required but not installed in the uv environment))
endef


TOOLS = mdformat-all ruff ty $(MDLINT) $(NIXIE) pytest uv

tools: ## Verify required CLI tools
	$(foreach t,$(TOOLS),$(call ensure_tool,$t))
	@:

fmt: tools ## Format sources
	ruff format
	mdformat-all

check-fmt: ## Verify formatting
	ruff format --check
	mdformat-all --check

lint: tools ## Run linters
	ruff check

typecheck: build ## Run typechecking
	uv run pyright
	uv run ty check

markdownlint: tools ## Lint Markdown files
	find . -type f -name '*.md' -not -path './target/*' -print0 | xargs -0 $(MDLINT)

nixie: tools ## Validate Mermaid diagrams
	find . -type f -name '*.md' -not -path './target/*' -print0 | xargs -0 $(NIXIE)

test: build ## Run tests
	uv run pytest -v

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
