"""paperless-rearchive: tag-driven re-OCR sidecar for paperless-ngx."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is `project.version` in pyproject.toml; this
    # read works because the package is always installed (Docker image,
    # editable checkout for development).
    __version__ = version("paperless-rearchive")
except PackageNotFoundError:  # running from a bare, uninstalled checkout
    __version__ = "0.0.0+unknown"

