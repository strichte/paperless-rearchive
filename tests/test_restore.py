"""Unit tests for the restore_backup tool (no DB, no paperless needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_rearchive import restore


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- post_process_text: must mirror paperless-ngx exactly --------------------


def test_post_process_text_collapses_spaces_keeps_newlines() -> None:
    assert restore.post_process_text("a   b\tc") == "a b c"
    assert restore.post_process_text("line1\n   line2") == "line1\nline2"
    assert restore.post_process_text("  padded  \n") == "padded"


def test_post_process_text_nul_and_empty() -> None:
    assert restore.post_process_text("a\0b") == "a b"
    assert restore.post_process_text("") is None
    assert restore.post_process_text(None) is None
    assert restore.post_process_text("   \n  ") is None  # layout padding -> None


# --- backup discovery ---------------------------------------------------------


def test_find_backups_for_archive_recursive_sorted(tmp_path: Path) -> None:
    _write(tmp_path / "sub" / "a.pdf.bak-20260917-080951", b"1")
    _write(tmp_path / "sub" / "a.pdf.bak-20260917-194157", b"2")
    _write(tmp_path / "a.pdf.bak", b"x")  # no .bak-<stamp> suffix -> ignored
    _write(tmp_path / "other.pdf.bak-20260917-080951", b"y")

    found = restore.find_backups_for_archive(tmp_path, "sub/a.pdf")
    assert [c.path.name for c in found] == [
        "a.pdf.bak-20260917-080951",
        "a.pdf.bak-20260917-194157",
    ]
    assert all(c.archive_name == "sub/a.pdf" for c in found)


def test_find_backups_rejects_wrong_subdirectory(tmp_path: Path) -> None:
    # Same basename, different template sub-directory: not our document.
    _write(tmp_path / "other" / "a.pdf.bak-20260917-080951", b"1")
    assert restore.find_backups_for_archive(tmp_path, "sub/a.pdf") == []


def test_find_backups_missing_dir(tmp_path: Path) -> None:
    assert restore.find_backups_for_archive(tmp_path / "nope", "a.pdf") == []


def test_match_backup_by_exact_and_bare_name(tmp_path: Path) -> None:
    b1 = _write(tmp_path / "a.pdf.bak-20260917-080951", b"1")
    b2 = _write(tmp_path / "sub" / "a.pdf.bak-20260917-194157", b"2")

    exact = restore._match_backup_by_name(tmp_path, "a.pdf.bak-20260917-080951")
    assert [c.path for c in exact] == [b1]
    assert exact[0].archive_name == "a.pdf"  # flat layout -> basename

    bare = restore._match_backup_by_name(tmp_path, "a.pdf")
    assert sorted(c.path for c in bare) == sorted([b1, b2])


def test_match_backup_recovers_archive_subdir(tmp_path: Path) -> None:
    b1 = _write(tmp_path / "Travel" / "2024" / "a.pdf.bak-20260917-080951", b"1")
    matched = restore._match_backup_by_name(tmp_path, "a.pdf.bak-20260917-080951")
    assert [c.path for c in matched] == [b1]
    assert matched[0].archive_name == "Travel/2024/a.pdf"


def test_match_backup_by_glob(tmp_path: Path) -> None:
    b1 = _write(tmp_path / "a.pdf.bak-20260917-080951", b"1")
    matched = restore._match_backup_by_name(tmp_path, "a.pdf.bak-20260917-*")
    assert [c.path for c in matched] == [b1]


def test_resolve_numeric_operand_needs_no_db(tmp_path: Path) -> None:
    from paperless_rearchive.config import DbSettings

    db = DbSettings(host="x", port=1, dbname="x", user="x", password="x")
    assert restore.resolve_operand_to_doc_id("4221", backup_dir=tmp_path, db=db) == 4221


# --- interactive helpers -------------------------------------------------------


def test_choose_backup_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cands = [
        restore.BackupCandidate(path=tmp_path / "a.bak-1", archive_name="a.pdf"),
        restore.BackupCandidate(path=tmp_path / "a.bak-2", archive_name="a.pdf"),
    ]
    monkeypatch.setattr("builtins.input", lambda _: "2")
    assert restore.choose_backup(7, cands) == tmp_path / "a.bak-2"


def test_confirm_defaults_to_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert restore.confirm("Sure?", force=False) is False
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert restore.confirm("Sure?", force=False) is True
    assert restore.confirm("Sure?", force=True) is True  # no prompt needed


# --- restore operations (DB + API faked) ----------------------------------------


class _FakeDb:
    def __init__(self) -> None:
        self.updated: list[tuple[int, str]] = []

    def update(self, _db: object, doc_id: int, checksum: str) -> None:
        self.updated.append((doc_id, checksum))


class _FakeApi:
    def __init__(self) -> None:
        self.patched: list[tuple[int, str]] = []

    def patch_content(self, doc_id: int, content: str) -> None:
        self.patched.append((doc_id, content))


def test_restore_archive_copies_and_updates_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paperless_rearchive import restore as r
    from paperless_rearchive.archive.replacer import checksum_of_file

    backup = _write(tmp_path / "backups" / "a.pdf.bak-20260917-080951", b"old-bytes")
    live = _write(tmp_path / "archives" / "a.pdf", b"new-bytes")
    plan = r.RestorePlan(doc_id=9, backup=backup, archive_path=live)

    fake = _FakeDb()
    monkeypatch.setattr(r.archive_db, "update_archive_checksum", fake.update)

    from types import SimpleNamespace

    assert r.restore_archive(plan, settings=SimpleNamespace(db=None), force=True) is True
    assert live.read_bytes() == b"old-bytes"
    assert fake.updated and fake.updated[0][0] == 9
    assert fake.updated[0][1] == checksum_of_file(live)


def test_restore_archive_declined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from paperless_rearchive import restore as r

    backup = _write(tmp_path / "b.pdf.bak-1", b"old")
    live = _write(tmp_path / "b.pdf", b"live")
    plan = r.RestorePlan(doc_id=1, backup=backup, archive_path=live)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    from types import SimpleNamespace

    assert r.restore_archive(plan, settings=SimpleNamespace(db=None), force=False) is False
    assert live.read_bytes() == b"live"


def test_restore_content_patches_cleaned_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from paperless_rearchive import restore as r

    backup = tmp_path / "c.pdf.bak-1"
    plan = r.RestorePlan(doc_id=3, backup=backup, archive_path=tmp_path / "c.pdf")
    monkeypatch.setattr(r, "extract_text_from_pdf", lambda _p: "clean text")
    api = _FakeApi()
    assert r.restore_content(plan, api, force=True) is True  # type: ignore[arg-type]
    assert api.patched == [(3, "clean text")]


# --- CLI ------------------------------------------------------------------------


def test_cli_flags_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        restore.main(["-a", "-c", "123"])
    assert exc.value.code == 2


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        restore.main(["-v"])
    assert exc.value.code == 0
    assert "restore_backup" in capsys.readouterr().out
