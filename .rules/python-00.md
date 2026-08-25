# Python 3.12 Code Style Guidelines (with Ruff, ty, Pylint, and pytest)

## Naming Conventions

- **Directories:** Use *snake_case* for top-level features or modules (e.g.,
  `data_pipeline`, `user_auth`).
- **Files:** Use *snake_case.py*; name for contents (e.g., `http_client.py`,
  `task_queue.py`).
- **Classes:** Use *PascalCase*.
- **Variables & Functions:** Use *snake_case*.
- **Constants:** Use *UPPER_SNAKE_CASE* for module-level constants.
- **Private/Internal:** Prefix with a single underscore (`_`) for non-exported
  helpers or internal APIs.

## Python Typing Practices

- **Use typing everywhere.** Enable and maintain full static type coverage.
  The commit gate runs `ty` (pinned via `TY_VERSION` in the `Makefile`);
  Pyright in strict mode remains configured in `pyproject.toml` for editor
  support.
- **Use `TypedDict` or `Dataclass` for structured data where appropriate.**
  For internal-only usage, prefer `@dc.dataclass(slots=True)`; the
  `prefer-slots-for-dataclass` checker enforces slots on closed dataclasses.
- **Avoid `Any`.** Use `object`, generics, or `typ.cast()` when
  necessary—always document why `Any` is acceptable if used (ANN401 is
  enforced).
- **Be explicit with returns.** Use `-> None`, `-> str`, etc., for all public
  functions and class methods.
  - **Favour immutability.** Prefer tuples to lists, and
    `types.MappingProxyType` where appropriate.

## Tooling and Runtime Practices

- **Enable Ruff.** Use Ruff to lint for performance, security, consistency,
  and style issues. Enable fixers and formatters. The pinned version lives in
  the `Makefile` (`RUFF_VERSION`) and must match the CI workflow;
  `tests/test_toolchain_versions.py` enforces the pairing.
- Use `pyproject.toml` to configure tools like Ruff, Pylint, Pyright, and
  pytest.
- **Treat typecheck diagnostics as CI errors.** `make typecheck` runs `ty`
  over the package and tests. Use `# ty: ignore[...]` sparingly and always
  with an explanation; the `typecheck-suppression-without-explanation`
  checker rejects bare pragmas.
- **Run Pylint with the df12 house checkers.** `make lint` loads the
  `df12_python_lints` plugin on CPython 3.14; fix findings at source rather
  than suppressing them.
- **Avoid side effects at import time.** Modules should not modify global
  state or perform actions on import.
- **Use `.env` or settings modules** for environment-specific configuration.
  Never hardcode secrets.

## Linting and Formatting

- **Use Ruff for linting** (replacing flake8, isort, pyflakes, etc.).
- **Use Ruff for formatting**. Let Ruff handle whitespace and formatting
  entirely—don't fight it. Under preview mode it also formats Python code
  fences in Markdown.

## Documentation

- **Use docstrings.** Document public functions, classes, and modules using
  NumPy format. The DOC rules require the Returns/Raises/Yields sections to
  match the code. For example:

```python
def scale(values: list[float], factor: float) -> list[float]:
    """
    Scale a list of numbers by a given factor.

    Parameters
    ----------
    values : list of float
        The list of numeric values to scale.
    factor : float
        The multiplier to apply to each value.

    Returns
    -------
    list of float
        The scaled numeric values.
    """
    return [v * factor for v in values]
```

- **Explain tricky code.** Use inline comments for non-obvious logic or
  decisions.
- **Colocate documentation.** Keep README.md or `docs/` near reusable
  packages; include usage examples.

## Testing with pytest

- **Colocate unit tests with code** using an `unittests` subdirectory and a
  `test_` prefix. This keeps logic and its tests together:

```text
user_auth/
  models.py
  login_flow.py
  unittests/
    test_models.py
    test_login_flow.py
```

- **Structure integration tests separately.** When tests span multiple
  components, use `tests/` (behavioural suites live in `tests/behaviour/`):

  ```text
  tests/
    behaviour/
      test_login_flow_steps.py
    test_user_onboarding.py
  ```

- **Use `pytest` idioms.** Prefer fixtures over setup/teardown methods.
  Parametrize broadly. Avoid unnecessary mocks.

- **Group related tests** using `class` with method names prefixed by
  `test_`.

- **Write tests from a user's perspective.** Test public behaviour, not
  internals.

- **Avoid mocking too much.** Prefer test doubles only for external services
  or non-deterministic behaviours.

- **Give every `assert` a failure message** naming the violated expectation;
  the `assert-missing-message` checker enforces this.

## Example

```python
# login_flow.py
def login_user(username: str, password: str) -> bool:
    """Return True if the user is authenticated."""
    ...


# test_login_flow.py
def test_login_success():
    assert login_user("alice", "correct-password") is True, (
        "valid credentials must authenticate"
    )


def test_login_failure():
    assert not login_user("alice", "wrong-password"), (
        "invalid credentials must be rejected"
    )
```

______________________________________________________________________

This style guide aims to foster clean, consistent, and maintainable Python
3.12 code with modern tooling. The priority is correctness, clarity, and
developer empathy.
