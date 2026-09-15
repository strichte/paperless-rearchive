"""Secret resolution following the paperless-ngx ``_FILE`` convention.

For every sensitive setting ``NAME``:

* if the environment variable ``NAME_FILE`` points at a file, that file's
  content (surrounding whitespace stripped) becomes the value of ``NAME``
  (Docker secrets mounted into the container);
* otherwise the value of the plain environment variable ``NAME`` is used.

A user may therefore configure each credential either directly (env, ``.env``
file) or via a secret file in the compose file, exactly like paperless-ngx
itself (``PAPERLESS_DBPASS`` vs ``PAPERLESS_DBPASS_FILE``, …). If both are set,
the ``_FILE`` variant wins.
"""

from __future__ import annotations

import os
from pathlib import Path


def secret(name: str) -> str:
    """Resolve a secret from ``NAME`` or ``NAME_FILE`` (file wins when set)."""
    file_var = f"{name}_FILE"
    file_path = os.environ.get(file_var, "").strip()
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    value = os.environ.get(name, "")
    return value.strip()


def secret_or_default(name: str, default: str) -> str:
    """Like :func:`secret`, but returns ``default`` when nothing is set."""
    return secret(name) or default
