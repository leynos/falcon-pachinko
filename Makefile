MDLINT ?= $(shell which markdownlint)
NIXIE ?= $(shell which nixie)
MDFORMAT_ALL ?= $(shell which mdformat-all)
TOOLS = $(MDFORMAT_ALL) ruff ty $(MDLINT) $(NIXIE) uv
VENV_TOOLS = pytest
UV ?= uv
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
RUFF_VERSION ?= 0.15.12
PATHSPEC_VERSION ?= 1.1.1
TYPOS_VERSION ?= 1.48.0
TYPOS_CONFIG_BUILDER_COMMIT := d6da92f02240a79a945c835f69bdd08a888da1d0
TYPOS_CONFIG_BUILDER_SOURCE := git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_COMMIT)
TYPOS_CONFIG_BUILDER := $(UV_ENV) $(UV) tool run --python 3.14 \
	--from "$(TYPOS_CONFIG_BUILDER_SOURCE)" typos-config-builder
SPELLING_PY_SRCS := \
	scripts/typos_rollout_check.py scripts/tests/test_typos_rollout_check.py
SPELLING_PY_TESTS := scripts/tests/test_typos_rollout_check.py
SPELLING_COVERAGE_ARGS := --cov=typos_rollout_check --cov-fail-under=90
SPELLING_HELPER_PYTEST = PYTHONPATH=scripts $(UV_ENV) $(UV) run --no-project \
	--python 3.14 --with pathspec==$(PATHSPEC_VERSION) --with pytest==9.0.2 \
	--with pytest-cov==7.0.0 python -m pytest

.PHONY: help all clean build build-release lint fmt check-fmt \
	markdownlint nixie spelling spelling-config spelling-config-write \
	spelling-phrase-check spelling-helper-test test typecheck $(TOOLS) $(VENV_TOOLS)

.DEFAULT_GOAL := all

all: build check-fmt test typecheck spelling

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
	ty check falcon_pachinko tests

markdownlint: spelling $(MDLINT) ## Lint Markdown files and enforce spelling
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -print0 | xargs -0 $(MDLINT)

spelling: spelling-phrase-check ## Enforce en-GB-oxendict spelling in tracked Markdown prose
	@git ls-files -z '*.md' | xargs -0 -r env $(UV_ENV) \
		$(UV) tool run typos@$(TYPOS_VERSION) --config typos.toml --force-exclude

spelling-phrase-check: spelling-config ## Reject prohibited spelling phrases
	@PYTHONPATH=scripts $(UV_ENV) $(UV) run --no-project --python 3.14 scripts/typos_rollout_check.py --repository .

spelling-config: spelling-helper-test ## Verify the generated spelling configuration
	@git ls-files --error-unmatch typos.toml >/dev/null
	@$(TYPOS_CONFIG_BUILDER) --repository . --check

spelling-config-write: spelling-helper-test ## Generate the spelling configuration
	@$(TYPOS_CONFIG_BUILDER) --repository .

spelling-helper-test: ## Validate the shared spelling-policy integration
	@$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) format --isolated --target-version py312 --check $(SPELLING_PY_SRCS)
	@$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) check --isolated --target-version py312 $(SPELLING_PY_SRCS)
	@$(SPELLING_HELPER_PYTEST) $(SPELLING_PY_TESTS) -c /dev/null --rootdir=. -p no:cacheprovider $(SPELLING_COVERAGE_ARGS)

nixie: $(NIXIE) ## Validate Mermaid diagrams
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -print0 | xargs -0 $(NIXIE)

test: build uv pytest ## Run tests
	uv run pytest -v

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
