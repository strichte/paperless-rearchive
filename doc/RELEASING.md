# Releasing paperless-rearchive

How to cut a release and what to do afterwards. Versioning follows SemVer;
while the project is pre-1.0, new features bump the **minor** version and
bugfixes the **patch** version.

## Versioning rhythm (trunk-based)

Development happens on `main`. A tag (`v0.1.0`) is only a pointer to a
released commit — it does not branch anything.

1. **Right after tagging a release**, bump `pyproject.toml` to the next
   expected version *with a `.dev0` suffix* (e.g. `0.2.0.dev0`) and open an
   empty `## Unreleased` section in `CHANGELOG.md`. The startup log then
   clearly says `version=0.2.0.dev0` — unreleased `main` can never be
   confused with a production install running `v0.1.0`.
2. **During the cycle**, collect user-facing changes under `## Unreleased`
   in `CHANGELOG.md`.
3. **At release time**, rename `## Unreleased` → `## <version> — <date>`,
   drop the `.dev0` suffix from `version` in `pyproject.toml`, run the
   release check, commit `Release <version>`, tag `v<version>` and push
   (runbook below).

Fix-only releases increment the patch version (`0.1.1`). If a hotfix is
needed while `main` already carries the next feature cycle, branch
`release/v0.1` from the tag, cherry-pick the fix, bump to `0.1.1` and tag
there — otherwise fix-forward on `main` and skip release branches.

## Distribution model

Releases are **source tags**. There is no CI, no registry, no prebuilt
image to publish — deployment builds the image from the checked-out tag
with docker compose, exactly like development does (cached layers make it
a seconds-long operation). The version-pinned `image:` tag in the compose
file keeps previous releases on disk for instant rollback.

`docker/Dockerfile` installs paperless-chandra from upstream git; the
`CHANDRA_REF` build-arg pins the ref (branch, tag or commit SHA) for
reproducible builds:

```bash
docker compose build --build-arg CHANDRA_REF=<master-sha> paperless-rearchive
```

Record the ref used in the release notes when it matters.

## Release runbook

1. **Finalize the changelog**: rename `## Unreleased` → `## <version> — <date>`
   in `CHANGELOG.md`; drop the `.dev0` suffix from `version` in
   `pyproject.toml`.
2. **Run the release guard** (clean tree, tag free, version/tag/changelog
   agreement, full test suite):
   ```bash
   scripts/release-check.sh <version>
   ```
3. **Commit** the release bump: `git commit -am "Release <version>"`.
4. **Tag and push**:
   ```bash
   git tag -a v<version> -m "paperless-rearchive <version>"
   git push origin main v<version>
   ```
5. **Deploy the release** (deploy host):
   - `git fetch --tags && git checkout v<version>`
   - `docker compose build paperless-rearchive && docker compose up -d paperless-rearchive`
6. **Verify**:
   - `docker logs paperless-rearchive` shows `version=<version>` in the
     startup line.
   - `tests/integration/check_archive_provenance.sh <known-doc-id>` reports
     `CHANDRA`, and the archive's `Creator` carries the
     `[model: …]` provenance stamp.
   - Run one smoke document through `re-ocr-all` (audit note + custom fields
     written, backup created in `/archive-backups`).
7. **Open the next cycle**: bump `pyproject.toml` to the next `.dev0`
   version, open a fresh `## Unreleased` changelog section, commit and push.

## Rollback

Redeploy the previous release: `git checkout v<previous>` and
`docker compose build paperless-rearchive && docker compose up -d
paperless-rearchive` (or re-tag the previous image if it is still on
disk). Archives are safe — replaced archives have `.bak` copies in
`/archive-backups`, and the checksum drift adoption logic
reconciles the DB with on-disk reality.
