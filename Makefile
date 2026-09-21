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
# Pylint runs in two isolated tool environments, never in the project .venv:
#
# - Classic checks: vanilla Pylint on PyPy 8.0.0's Python 3.12 build, one
#   worker, configured by pyproject.toml. The linter's interpreter matches the
#   project's Python 3.12 baseline, so it parses exactly the supported syntax.
# - df12-python-lints: CPython 3.14, configured by pylintrc-df12.toml, which
#   pins py-version to the 3.12 baseline so running on 3.14 cannot impose it.
#
# Both passes share these pins. Pylint 4.0.8 is the newest release and declares
# astroid>=4.0.2,<=4.1.dev0, which excludes Astroid 4.3.x and its descriptor
# fix. PyPy 8.0.0 fixes the underlying runtime bug (pypy/pypy#5458), so
# vanilla Astroid 4.0.4 bootstraps on PyPy without a shim. Move to Astroid
# 4.3.1 once a Pylint release accepts it.
PYLINT_VERSION ?= 4.0.8
ASTROID_VERSION ?= 4.0.4
# List falcon_pachinko/unittests and falcon_pachinko/behaviour explicitly:
# they carry no __init__.py, so package discovery from falcon_pachinko does
# not descend into them.
PYLINT_TARGETS ?= falcon_pachinko falcon_pachinko/unittests \
	falcon_pachinko/behaviour tests examples tools
# Persisted Pylint state lives in one directory per runtime, so the two passes
# never read each other's statistics.
PYLINT_CACHE ?= .cache/pylint

# Classic pass. uv's built-in interpreter catalogue has no PyPy 3.12 build yet,
# so tools/pypy-downloads.json lists the official PyPy 8.0.0 tarballs with
# their published SHA-256 sums, and uv refuses any download that does not
# match. The interpreter lives in PYPY_INSTALL_DIR rather than uv's shared
# store, and --no-bin keeps it off PATH.
PYPY_RELEASE ?= 8.0.0
PYPY_PYTHON_VERSION ?= 3.12.14
PYPY_DOWNLOADS_JSON ?= tools/pypy-downloads.json
PYPY_INSTALL_DIR ?= .uv-python
PYPY_UV = $(UV_ENV) UV_PYTHON_INSTALL_DIR=$(PYPY_INSTALL_DIR) \
	UV_PYTHON_DOWNLOADS_JSON_URL=$(PYPY_DOWNLOADS_JSON) $(UV)
PYPY_REQUEST = pypy@$(PYPY_PYTHON_VERSION)
# Install the pinned build if needed, then print its executable path.
PYPY_PROVISION = $(PYPY_UV) python install --managed-python --no-bin \
	$(PYPY_REQUEST) >&2 && $(PYPY_UV) python find --managed-python $(PYPY_REQUEST)
# Leave PYLINT_PYTHON empty to use the pinned PyPy build, or set it to an
# interpreter path. Either way, tools/check_lint_runtime.py verifies the
# interpreter inside the tool environment before Pylint runs.
PYLINT_PYTHON ?=
PYLINT_TOOL = PYLINTHOME=$(PYLINT_CACHE)/pypy$(PYPY_PYTHON_VERSION)-v$(PYPY_RELEASE) \
	$(UV_ENV) $(UV) tool run --from pylint==$(PYLINT_VERSION) \
	--with astroid==$(ASTROID_VERSION)
PYLINT_RUNTIME_CHECK = --implementation pypy --python-version 3.12 \
	--pypy-version $(PYPY_RELEASE) --require-dist pylint==$(PYLINT_VERSION) \
	--require-dist astroid==$(ASTROID_VERSION) \
	--forbid-module df12_python_lints --forbid-module pylint_pypy_shim

# df12 pass: the plugin, and ambrleaks from the same package, on CPython 3.14.
DF12_PYTHON ?= cpython@3.14
DF12_PYTHON_VERSION ?= 3.14
DF12_PYTHON_LINTS_VERSION ?= 0.3.0
DF12_PYTHON_LINTS_REF ?= v$(DF12_PYTHON_LINTS_VERSION)
DF12_PYTHON_LINTS = df12-python-lints @ git+https://github.com/leynos/df12-python-lints.git@$(DF12_PYTHON_LINTS_REF)
DF12_TOOL = PYLINTHOME=$(PYLINT_CACHE)/cpython$(DF12_PYTHON_VERSION) \
	$(UV_ENV) $(UV) tool run --python $(DF12_PYTHON) \
	--from pylint==$(PYLINT_VERSION) --with astroid==$(ASTROID_VERSION) \
	--with '$(DF12_PYTHON_LINTS)'
DF12_RUNTIME_CHECK = --implementation cpython \
	--python-version $(DF12_PYTHON_VERSION) \
	--require-dist pylint==$(PYLINT_VERSION) \
	--require-dist astroid==$(ASTROID_VERSION) \
	--require-dist df12-python-lints==$(DF12_PYTHON_LINTS_VERSION) \
	--require-module df12_python_lints --forbid-module pylint_pypy_shim
AMBRLEAKS = $(UV_ENV) $(UV) tool run --python $(DF12_PYTHON) \
	--from '$(DF12_PYTHON_LINTS)' --with pylint==$(PYLINT_VERSION) \
	--with astroid==$(ASTROID_VERSION) ambrleaks

.PHONY: help all clean build build-release lint lint-pylint lint-df12 \
	pylint-pypy-python fmt check-fmt markdownlint nixie spelling test \
	typecheck $(TOOLS) $(VENV_TOOLS)

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
	$(MAKE) --no-print-directory lint-pylint
	$(MAKE) --no-print-directory lint-df12

pylint-pypy-python: uv ## Print the pinned PyPy interpreter, installing it if needed
	@$(PYPY_PROVISION)

lint-pylint: uv ## Run the classic Pylint checks on PyPy 8.0.0 (Python 3.12)
	set -eu; \
	python='$(PYLINT_PYTHON)'; \
	if [ -z "$$python" ]; then python=$$($(PYPY_PROVISION)); fi; \
	$(PYLINT_TOOL) --python "$$python" \
		python tools/check_lint_runtime.py $(PYLINT_RUNTIME_CHECK); \
	$(PYLINT_TOOL) --python "$$python" \
		pylint --rcfile=pyproject.toml --jobs=1 $(PYLINT_TARGETS)

lint-df12: uv ## Run df12-python-lints and ambrleaks on CPython 3.14
	$(DF12_TOOL) python tools/check_lint_runtime.py $(DF12_RUNTIME_CHECK)
	$(DF12_TOOL) pylint --rcfile=pylintrc-df12.toml --jobs=1 $(PYLINT_TARGETS)
	$(AMBRLEAKS) tests

typecheck: build uv ## Run typechecking
	$(TY) check falcon_pachinko tests tools

markdownlint: spelling $(MDLINT) ## Lint Markdown files and enforce spelling
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -not -path './.uv-python/*' \
	  -print0 | xargs -0 $(MDLINT)

spelling: ## Enforce en-GB-oxendict spelling
	$(TYPOS_CONFIG_BUILDER) gate --repository .

nixie: $(NIXIE) ## Validate Mermaid diagrams
	find . -type f -name '*.md' \
	  -not -path './.venv/*' -not -path './.uv-cache/*' \
	  -not -path './.uv-tools/*' -not -path './.uv-python/*' \
	  -print0 | xargs -0 $(NIXIE)

test: build uv pytest ## Run tests
	uv run pytest -v

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
