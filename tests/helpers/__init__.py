"""Shared helper modules for the release-workflow contract tests.

These helpers parse CI documents and execute the checked-in ``run`` bodies of
``release.yml`` against stand-in ``gh`` and ``curl`` programs. They live in a
real, importable package for the reason ``tests/support/__init__.py`` gives:
every consumer resolves the same module object, including under mutmut's
sandboxed test run.
"""

from __future__ import annotations
