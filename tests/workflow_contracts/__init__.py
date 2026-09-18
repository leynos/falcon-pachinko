"""Contracts over this repository's GitHub Actions workflows.

These read the workflow documents themselves rather than any Python source,
so they hold facts a green CI run cannot demonstrate: which runner a lane
asks for, whether a lane declares a ceiling, and whether a label a workflow
uses is registered where `actionlint` can see it.
"""
