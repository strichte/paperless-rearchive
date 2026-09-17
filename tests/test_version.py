"""Version consistency: __version__ must come from the installed package."""

from __future__ import annotations

import importlib.metadata

import paperless_rearchive


def test_version_is_single_sourced_from_installed_metadata() -> None:
    """__init__.py reads project.version from pyproject.toml via
    importlib.metadata — drift between the two must be impossible."""
    assert paperless_rearchive.__version__ == importlib.metadata.version(
        "paperless-rearchive"
    )


def test_version_is_not_the_placeholder() -> None:
    """A bare checkout without an installed package yields the placeholder;
    the test environment must always have the package installed."""
    assert paperless_rearchive.__version__ != "0.0.0+unknown"
