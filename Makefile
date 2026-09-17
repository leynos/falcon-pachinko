# The linter is `markdownlint-cli2`, not `markdownlint`: they are different
# programs. Naming the wrong one left every local run silently linting
# nothing, because `markdownlint` resolved to nothing and the target then ran
# `xargs` with no command at all. Named rather than resolved here, so a
# missing binary is reported by `ensure_tool` under the name a reader can
# install.
MDLINT ?= markdownlint-cli2
NIXIE ?= $(shell which nixie)
# `fmt` and `check-fmt` call mdtablefix directly. `--git` selects the tracked
# Markdown set and `--include-untracked` adds new files, so a document is
# neither missed because it is new nor rewritten twice.
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_SELECT = --git --include-untracked
TOOLS = ruff ty $(MDLINT) $(MDTABLEFIX) $(NIXIE) uv
VENV_TOOLS = pytest
UV ?= uv
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
TYPOS_CONFIG_BUILDER_VERSION ?= v0.1.1
TYPOS_CONFIG_BUILDER = $(UV_ENV) $(UV) tool run --python 3.14 --from \
	"git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)" \
	typos-config-builder

.PHONY: help all clean build build-release lint fmt check-fmt \
	markdownlint nixie spelling test test-workflow-contracts typecheck \
	$(TOOLS) $(VENV_TOOLS)

.DEFAULT_GOAL := all

all: build check-fmt test test-workflow-contracts typecheck spelling

.venv: pyproject.toml
	uv venv --clear

build: uv .venv ## Build virtual-env and install deps
	uv sync --group dev

build-release: ## Build artefacts (sdist & wheel)
	python -m build --sdist --wheel

clean: ## Remove build artefacts
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
	@uv run which $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required in the virtualenv, but is not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)

$(VENV_TOOLS): ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)

fmt: ruff $(MDTABLEFIX) $(MDLINT) ## Format sources
	ruff format
	ruff check --select I --fix
	$(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT)
	@unset FORCE_COLOR; $(MDLINT) --fix "**/*.md"

check-fmt: ruff $(MDTABLEFIX) ## Verify formatting
	ruff format --check
	$(MDTABLEFIX) --check $(MDTABLEFIX_SELECT)

lint: ruff ## Run linters
	ruff check

typecheck: build ty ## Run typechecking
	ty check falcon_pachinko tests

# Local convenience only. CI lints Markdown through
# DavidAnson/markdownlint-cli2-action, whose release carries the linter's
# whole dependency graph, so nothing is resolved from the registry at run
# time. The globs and exclusions live in `.markdownlint-cli2.jsonc`, which is
# why this no longer builds a file list of its own.
markdownlint: spelling $(MDLINT) ## Lint Markdown files and enforce spelling
	$(MDLINT) "**/*.md"

spelling: ## Enforce en-GB-oxendict spelling
	$(TYPOS_CONFIG_BUILDER) gate --repository .

nixie: $(NIXIE) ## Validate Mermaid diagrams
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -print0 | xargs -0 $(NIXIE)

test: build uv pytest ## Run tests
	uv run pytest -v

# The workflow contracts read the workflow documents rather than any Python
# source, so they are fast and worth running on their own while editing a
# workflow. `test` collects them too, which is what makes them a gate.
test-workflow-contracts: build uv pytest ## Check the CI workflows' shape
	uv run pytest tests/workflow_contracts -v

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
