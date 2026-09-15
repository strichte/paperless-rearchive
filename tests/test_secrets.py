"""Tests for the paperless-ngx-style ``_FILE`` secret resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.secrets import secret


def test_plain_var_when_no_file() -> None:
    with patch.dict("os.environ", {"MY_SECRET": "token-value"}, clear=False):
        assert secret("MY_SECRET") == "token-value"


def test_file_var_wins_over_plain(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("file-value\n", encoding="utf-8")
    with patch.dict(
        "os.environ",
        {"MY_SECRET": "plain-value", "MY_SECRET_FILE": str(secret_file)},
        clear=False,
    ):
        assert secret("MY_SECRET") == "file-value"


def test_file_content_is_stripped(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("  file-value  \n", encoding="utf-8")
    with patch.dict(
        "os.environ",
        {"MY_SECRET_FILE": str(secret_file)},
        clear=False,
    ):
        assert secret("MY_SECRET") == "file-value"


def test_missing_file_raises(tmp_path: Path) -> None:
    with patch.dict(
        "os.environ",
        {"MY_SECRET_FILE": str(tmp_path / "does-not-exist")},
        clear=False,
    ), pytest.raises(FileNotFoundError):
        secret("MY_SECRET")


def test_unset_returns_empty() -> None:
    with patch.dict("os.environ", {}, clear=False):
        assert secret("MY_SECRET") == ""
