"""Parse CI documents with a loader that refuses duplicate mapping keys.

PyYAML keeps the last of two identical keys and says nothing. A workflow that
declares ``runs-on`` twice therefore parses into a document that discarded the
first value, so a lane can carry a paid label, a token, or a guard in the
discarded half while every contract reads the half that survived. GitHub's own
parser rejects the file, so the contract and the platform would disagree about
what the repository declares.

Every workflow reader in the test suite parses through :func:`load`, which
refuses the document instead. Scope: CI documents only (workflows and
composite actions); it is not a general-purpose YAML helper.
"""

from __future__ import annotations

import yaml

from tests.workflow_contracts.strict_yaml import StrictLoader


def load(source: str, name: str) -> object:
    """Parse one CI document, refusing duplicate keys and naming the file.

    Parameters
    ----------
    source : str
        The document's text.
    name : str
        The file name to cite in a failure.

    Returns
    -------
    object
        The parsed document.

    Raises
    ------
    AssertionError
        If the text is not valid YAML or declares a mapping key twice.

    Examples
    --------
    >>> load("jobs: {}", "ci.yml")
    {'jobs': {}}

    """
    try:
        return yaml.load(source, Loader=StrictLoader)  # ruff: ignore[unsafe-yaml-load] - strict SafeLoader subclass
    except yaml.YAMLError as error:
        message = f"{name} is not valid YAML: {error}"
        raise AssertionError(message) from error
