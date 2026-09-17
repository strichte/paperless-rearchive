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
   drop the `.dev0` suffix from `pyproject.toml`, run the release check,
   commit `Release <version>`, tag `v<version>` and push (runbook below).

Fix-only releases increment the patch version (`0.1.1`). If a hotfix is
needed while `main` already carries the next feature cycle, branch
`release/v0.1` from the tag, cherry-pick the fix, bump to `0.1.1` and tag
there — otherwise fix-forward on `main` and skip release branches.

## One-time setup

- **act_runner**: register a Gitea act_runner with this instance so the
  workflows in `.gitea/workflows/` execute. The jobs assume a runner that
  can run `docker` (docker-in-docker or a host/exec runner with the docker
  CLI) and have network access to github.com (checkout/setup actions and
  the `paperless-chandra` git dependency).
- **Repository secrets** (Repo → Settings → Actions → Secrets):
  - `REGISTRY_USER` — Gitea username with package-write access.
  - `REGISTRY_TOKEN` — Gitea access token (package:write) for pushing to
    the instance container registry (`git.zsh.nz/steffen/...`).

## Release runbook

1. **Finalize the changelog**: rename `## Unreleased` → `## <version> — <date>`
   in `CHANGELOG.md`; drop the `.dev0` suffix from `version` in
   `pyproject.toml`.
2. **Pin the plugin ref** used for the release image (reproducibility):
   note the current `paperless-chandra` master SHA and pass it as
   `CHANDRA_REF` (the CI release workflow and the runbook command below do
   this; the Dockerfile default remains `master`).
3. **Run the release guard** (clean tree, tag free, version/tag/changelog
   agreement, full test suite):
   ```bash
   scripts/release-check.sh <version>
   ```
4. **Commit** the release bump: `git commit -am "Release <version>"`.
5. **Tag and push** (this triggers the CI release workflow — tests, image
   build + push, Gitea release page):
   ```bash
   git tag -a v<version> -m "paperless-rearchive <version>"
   git push origin main v<version>
   ```
6. **Verify CI**: Actions → release workflow green; Packages shows
   `steffen/paperless-rearchive:<version>` and `:latest`; Releases page
   shows the tag with the changelog body. Without a runner, do those steps
   manually (build/push commands are in `.gitea/workflows/release.yml`).

## After the release

1. **Deploy the release** (deploy host):
   - `git fetch && git checkout v<version>` and build locally, **or** point
     the compose service at the registry image
     `git.zsh.nz/steffen/paperless-rearchive:v<version>`.
   - `docker compose up -d paperless-rearchive`.
2. **Verify**:
   - `docker logs paperless-rearchive` shows `version=<version>` in the
     startup line.
   - `tests/integration/check_archive_provenance.sh <known-doc-id>` reports
     `CHANDRA`.
   - Run one smoke document through `re-ocr-all` (Creator metadata carries
     `[model: …]`, audit note + custom fields written, backup created in
     `REARCHIVE_BACKUP_DIRECTORY`).
3. **Open the next cycle**: bump `pyproject.toml` to the next `.dev0`
   version, open a fresh `## Unreleased` changelog section, commit and push.
4. **Rollback** (if needed): redeploy the previous image tag or
   `git checkout v<previous>`. Archives are safe — replaced archives have
   `.bak` copies in `REARCHIVE_BACKUP_DIRECTORY`, and the checksum drift
   adoption logic reconciles the DB with on-disk reality.
