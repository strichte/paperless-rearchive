#!/usr/bin/env bash
# Release guard — run before tagging a release.
#
#   scripts/release-check.sh <version>     e.g. scripts/release-check.sh 0.1.0
#
# Fails unless:
#   * the working tree is clean (commit the release bump first),
#   * the tag v<version> does not exist yet,
#   * pyproject.toml's project.version == <version>,
#   * CHANGELOG.md has a '## <version>' section,
#   * the full test suite passes (plus ruff, when installed).
#
# See doc/RELEASING.md for the full runbook.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
[ -n "$VERSION" ] || { echo "usage: scripts/release-check.sh <version>  (e.g. 0.1.0)" >&2; exit 2; }

fail() { echo "FAIL: $*" >&2; exit 1; }

[ -z "$(git status --porcelain)" ] || fail "working tree not clean (commit the release bump first)"
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null && fail "tag v$VERSION already exists"

pyproject_version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
[ "$pyproject_version" = "$VERSION" ] || fail "pyproject.toml version ($pyproject_version) != $VERSION"

grep -q "^## $VERSION" CHANGELOG.md || fail "CHANGELOG.md has no '## $VERSION' section"

PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

echo "==> pytest"
"$PYTHON" -m pytest tests/ -q

if "$PYTHON" -m ruff --version >/dev/null 2>&1; then
  echo "==> ruff check"
  "$PYTHON" -m ruff check .
fi

echo "release check OK for v$VERSION — runbook: doc/RELEASING.md"
