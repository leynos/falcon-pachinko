MDLINT ?= $(shell which markdownlint)
NIXIE ?= $(shell which nixie)
MDFORMAT_ALL ?= $(shell which mdformat-all)
TOOLS = $(MDFORMAT_ALL) $(MDLINT) $(NIXIE) uv
VENV_TOOLS = pytest
UV ?= uv
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
# Retain the typos-config-builder gate: the bespoke spelling machinery, and
# the PATHSPEC_VERSION and TYPOS_VERSION pins that served only it, were
# retired in favour of this single gate subcommand. The RUFF_VERSION below is
# a separate pin driving the repository-wide format and lint gates.
TYPOS_CONFIG_BUILDER_VERSION ?= v0.1.1
TYPOS_CONFIG_BUILDER = $(UV_ENV) $(UV) tool run --python 3.14 --from \
	"git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)" \
	typos-config-builder
# Pin Ruff so `make` invokes the same version as the `ruff==` pin in
# .github/workflows/ci.yml. tests/test_toolchain_versions.py enforces that
# both sites stay in sync; bump them together, because rule sets differ
# between Ruff releases and a mismatch causes version-skew lint failures.
# This pin is unrelated to the retired spelling-helper RUFF_VERSION: it drives
# the repository-wide format and lint gates.
RUFF_VERSION ?= 0.16.4
RUFF ?= $(UV) tool run --from ruff==$(RUFF_VERSION) ruff
# Pin ty so `make` and CI invoke the same typechecker release. ty is
# pre-1.0 and diagnostics shift between releases, so an unpinned install
# breaks the typecheck gate without any code change. Bump deliberately and
# fix new diagnostics in the same commit.
TY_VERSION ?= 0.0.74
TY ?= $(UV) tool run --from ty==$(TY_VERSION) ty
# Run Pylint with the df12-python-lints plugin on CPython 3.14, matching the
# plugin's supported baseline. The same tool environment provides ambrleaks.
# CPython is deliberate: a PyPy-backed run would lag the project's syntax
# baseline and silently skip files it cannot parse.
DF12_PYTHON_LINTS_REF ?= v0.3.0
DF12_PYTHON_LINTS = df12-python-lints @ git+https://github.com/leynos/df12-python-lints.git@$(DF12_PYTHON_LINTS_REF)
PYLINT_PYTHON ?= 3.14
# Fan Pylint out across a tenth of the available cores, with a floor of two.
# This host is shared with other agents, so a full-width fan-out would starve
# them; the floor keeps the gate off Pylint's single-process default.
#
# Caveat: with --jobs > 1, df12-python-lints v0.3.0 reports each of its own
# messages twice, because its register() hook runs again in every worker and
# registers the checkers a second time. Pylint's builtin messages are
# unaffected, and the exit status stays correct, so the gate still passes or
# fails accurately -- only plugin findings appear duplicated. Set
# PYLINT_JOBS=1 when counting them matters.
PYLINT_JOBS ?= $(shell n=$$(nproc 2>/dev/null || echo 2); \
	echo $$(( n / 10 > 2 ? n / 10 : 2 )))
# List falcon_pachinko/unittests and falcon_pachinko/behaviour explicitly:
# they carry no __init__.py, so package discovery from falcon_pachinko does
# not descend into them.
PYLINT_TARGETS ?= falcon_pachinko falcon_pachinko/unittests \
	falcon_pachinko/behaviour tests examples
PYLINT = $(UV_ENV) $(UV) tool run --python $(PYLINT_PYTHON) \
	--from pylint --with '$(DF12_PYTHON_LINTS)' pylint --jobs $(PYLINT_JOBS)
AMBRLEAKS = $(UV_ENV) $(UV) tool run --python $(PYLINT_PYTHON) \
	--from '$(DF12_PYTHON_LINTS)' ambrleaks

.PHONY: help all clean build build-release lint fmt check-fmt \
	markdownlint nixie spelling test typecheck $(TOOLS) $(VENV_TOOLS)

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

fmt: uv $(MDFORMAT_ALL) ## Format sources
	$(RUFF) format
	$(RUFF) check --select I --fix
	$(MDFORMAT_ALL)

check-fmt: uv ## Verify formatting
	$(RUFF) format --check
	# mdformat-all doesn't currently do checking

lint: uv ## Run linters
	$(RUFF) check
	$(PYLINT) $(PYLINT_TARGETS)
	$(AMBRLEAKS) tests

typecheck: build uv ## Run typechecking
	$(TY) check falcon_pachinko tests

markdownlint: spelling $(MDLINT) ## Lint Markdown files and enforce spelling
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -print0 | xargs -0 $(MDLINT)

spelling: ## Enforce en-GB-oxendict spelling
	$(TYPOS_CONFIG_BUILDER) gate --repository .

nixie: $(NIXIE) ## Validate Mermaid diagrams
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -print0 | xargs -0 $(NIXIE)

test: build uv pytest ## Run tests
	uv run pytest -v

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
