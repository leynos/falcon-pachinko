.PHONY: help all clean build lint fmt check-fmt markdownlint \
tools nixie test typecheck

BUILD_JOBS ?=
MDLINT ?= markdownlint
NIXIE ?= nixie

all: build

build: tools ## Build debug artifact
	uv venv
	uv sync --group dev

define ensure_tool
$(if $(shell command -v $(1) >/dev/null 2>&1 && echo y),,\
$(error $(1) is required but not installed))
endef

tools:
	$(call ensure_tool,mdformat-all)
	$(call ensure_tool,uv)
	$(call ensure_tool,ruff)
	$(call ensure_tool,ty)

fmt: tools ## Format sources
	ruff format
	mdformat-all

check-fmt: tools ## Verify formatting
	ruff format --check

lint: tools ## Run linters
	ruff check

markdownlint: ## Lint Markdown files
	find . -type f -name '*.md' -not -path './target/*' -print0 | xargs -0 $(MDLINT)

nixie: ## Validate Mermaid diagrams
	find . -type f -name '*.md' -not -path './target/*' -print0 | xargs -0 $(NIXIE)

test: build ## Run tests
	uv run pytest -q

typecheck: build ## Static type analysis
	ty check

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
