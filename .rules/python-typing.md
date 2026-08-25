# Advanced Typing and Language Features (Python 3.12)

> This section documents modern Python typing features and best practices to
> improve clarity, correctness, and tooling support, adapted to this
> project's Python 3.12 baseline. Features that first landed in Python 3.13
> are marked as such.

## `enum.Enum`, `enum.IntEnum`, `enum.StrEnum`

Use `Enum` for fixed sets of related constants. Use `enum.auto()` to avoid
repeating values manually. Use `IntEnum` or `StrEnum` when interoperability
with integers or strings is required (e.g. for database or JSON
serialization).

```python
import enum


class Status(enum.Enum):
    PENDING = enum.auto()
    COMPLETE = enum.auto()


class ErrorCode(enum.IntEnum):
    OK = 0
    NOT_FOUND = 404


class Role(enum.StrEnum):
    ADMIN = enum.auto()
    GUEST = enum.auto()
```

Use `auto()` when exact values are unimportant and you want to avoid
duplication. Avoid `auto()` in `IntEnum` where numeric meaning matters.

## `match` / `case` (Structural Pattern Matching)

Use structural pattern matching for branching over structured data. This is
especially useful for enums, discriminated unions, or pattern-rich data
structures. The `prefer-structural-pattern-matching` and
`prefer-match-over-constant-chain` checkers report `isinstance` ladders and
constant `if`/`elif` chains that should be `match` statements.

```python
def handle_status(status: Status) -> str:
    match status:
        case Status.PENDING:
            return "Still processing"
        case Status.COMPLETE:
            return "Done"
```

## Generic Class Declarations (PEP 695)

Use bracketed class-level type variables directly for generic class
declarations. PEP 695 syntax is fully available on Python 3.12.

```python
class Box[T]:
    def __init__(self, value: T):
        self.value = value
```

This is cleaner and avoids the indirection of separate `TypeVar`
declarations.

## `Self` Type (PEP 673)

Use `Self` in fluent interfaces and builder-style APIs to indicate the
method returns the same instance.

```python
import typing as typ


class Builder:
    def add(self, value: int) -> typ.Self:
        self.values.append(value)
        return self
```

This improves tool support and enforces correct chaining semantics.

## `@override` Decorator (PEP 698)

Use `@override` to indicate that a method overrides one from a superclass.
This enables static analysis tools to detect typos and signature mismatches,
and Ruff exempts `@typ.override`-decorated methods from `no-self-use`.

```python
import typing as typ


class Base:
    def run(self) -> None: ...


class Child(Base):
    @typ.override
    def run(self) -> None:
        print("Running")
```

This decorator is a no-op at runtime but improves tooling correctness.

## `TypeIs` (PEP 742) — Python 3.13+

`typing.TypeIs` arrived in Python 3.13. On the 3.12 baseline, import it from
`typing_extensions` instead (and only add that dependency when a narrowing
guard genuinely needs it).

```python
from typing_extensions import TypeIs


def is_str_list(val: list[object]) -> TypeIs[list[str]]:
    return all(isinstance(x, str) for x in val)
```

Unlike `isinstance`, this informs the type checker that `val` is now
`list[str]`.

## Defaults for TypeVars (PEP 696) — Python 3.13+

Type-variable defaults reached `typing` in Python 3.13. On 3.12, spell the
default with PEP 695 syntax only where the type checker supports it, or fall
back to `typing_extensions.TypeVar` for a runtime-visible default.

```python
from typing_extensions import TypeVar

T = TypeVar("T", default=int)
```

This makes APIs more ergonomic while retaining type safety; do not reach for
it until an API genuinely benefits from a defaulted parameter.

## Standard Library Generics (PEP 585)

Use built-in generics from the standard library (`list`, `dict`, `tuple`,
etc.) instead of `typing.List`, `typing.Dict`, etc. The Ruff configuration
bans the deprecated `typing` generics outright; use `collections.abc` for
the abstract collection types.

```python
names: list[str] = ["Alice", "Bob"]
```

This reduces imports and reflects the modern style.

## Union Syntax and Optional (PEP 604)

Use `|` to write union types, and `A | None` instead of `Optional[A]`.

```python
value: int | None = None
```

This is more concise and readable, especially for nested types.

## Type Aliases using `type`

Use the `type` keyword to create type aliases with better IDE and runtime
support. This is available from Python 3.12, and the `prefer-type-statement`
checker enforces it for module-level aliases.

```python
type StrDict = dict[str, str]
```

This replaces `StrDict: TypeAlias = ...` and is preferred in modern Python.

## `from __future__ import annotations`

Use this import in modules with type annotations to defer evaluation of
annotation expressions to runtime. This prevents issues with forward
references and circular imports.

```python
from __future__ import annotations
```

Recommended in all modern Python files using type hints while the baseline
remains below Python 3.14 (where deferred evaluation becomes the default).

## `if typ.TYPE_CHECKING`

Use this conditional to guard imports required only for static typing. The
TC rules enforce moving typing-only imports into this block.

```python
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
```

This avoids runtime import costs or circular imports.

## Standard Aliases

Use the following import aliases consistently; the Ruff
`flake8-import-conventions` configuration enforces them and bans the
corresponding `from` imports:

```python
import collections.abc as cabc
import dataclasses as dc
import datetime as dt
import typing as typ
import unittest.mock as mock

import msgspec as ms
import msgspec.inspect as msinspect
import msgspec.json as msjson
```

This simplifies common types such as `dt.datetime`, `cabc.Iterable`,
`cabc.Callable`, and helps disambiguate usage.

______________________________________________________________________

These conventions promote clarity, tool compatibility, and future-ready
Python.
