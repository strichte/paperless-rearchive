# Test Suite — paperless-rearchive

Inventory of the pytest suite (unit + component tests) and the manual
integration harness. Generated from `pytest --collect-only`; 201 tests in
14 files, all passing as of this writing.

## Running

```bash
cd /home/paperless/paperless-rearchive
source .venv/bin/activate
pytest tests/ -q            # full suite (~4 s, no network or paperless needed)
pytest tests/test_pipeline.py -q          # one file
pytest tests/test_chandra_engine.py::test_mixed_ingest_pass_ocrs_only_pages_needing_ocr
```

The unit suite is self-contained: HTTP calls, `ocrmypdf.ocr`, and the
Chandra client are monkeypatched. The scripts under `tests/integration/`
are the exception — they talk to the live stack and are **not** part of
pytest (see [Integration tooling](#integration-tooling)).

## File overview

| File | Tests | Covers |
| --- | --- | --- |
| `test_chandra_engine.py` | 23 | OCR engine: error semantics, provenance-driven paths, mixed `--pages` pass, per-page action reporting, reachability preflight |
| `test_config.py` | 17 | Settings: dirs, backup-dir validation, provenance knobs |
| `test_ingest_args.py` | 33 | Ingest-parity ocrmypdf args + text helpers (paperless parity layer) |
| `test_model_check.py` | 11 | `/models` preflight (fail fast on unserved model) |
| `test_paperless_api.py` | 16 | REST client: tags, custom fields, notes, pagination |
| `test_pipeline.py` | 5 | End-to-end document processing flow (mocked engine/API) |
| `test_poller.py` | 13 | Poll cycle timing, signals, backoff, failure escalation, outage gate |
| `test_provenance.py` | 8 | Per-page born-digital/scanned/mixed classification |
| `test_replacer.py` | 9 | Archive file replacement + backup handling |
| `test_restore.py` | 16 | `restore.py`: backup matching, restore, content restore |
| `test_runner.py` | 30 | Legacy runner: args, MIME sniffing, Content-Disposition, strategy |
| `test_secrets.py` | 5 | `_FILE`-suffixed secret env vars |
| `test_server_check.py` | 11 | `/models` reachability probe (fail fast on server outage) |
| `test_version.py` | 2 | Version single-sourcing |

---

## test_chandra_engine.py — OCR engine (23)

Error semantics: transport/server errors (`ChandraClientError`) abort the
whole document so the poller keeps the trigger tag and retries; genuine
per-page failures become page errors with a partial result.

- `test_transport_error_aborts_document_sequential` — first page raising `ChandraClientError` aborts; no per-page swallowing.
- `test_transport_error_aborts_document_concurrent` — same under `concurrency=2`.
- `test_empty_page_result_stays_a_page_error` — empty OCR result is a page error, not an abort.
- `test_stamp_model_provenance` — served model name stamped into archive XMP/CreatorTool.
- `test_stamp_model_provenance_without_model_name` — no-op when the server advertises no model.
- `test_content_path_ocrs_only_pages_needing_ocr` — content-only run: only `pages_needing_ocr` hit Chandra; native pages keep pdf-inspector markdown.
- `test_content_path_force_ocrs_every_page` — `force=True` OCRs every page.
- `test_resolve_ingest_mode_uses_provenance` — mode derivation: mixed→skip, scanned→redo, text-based→off, force override.
- `test_model_not_served_aborts_before_any_page` — `/models` probe failure aborts before any page is sent.
- `test_model_preflight_receives_engine_configuration` — probe gets the engine's URL/model/key.
- `test_upstream_generation_error_is_logged_with_model` — upstream bare "Error during VLLM generation" is re-logged with the model name.

### Reachability preflight (2026-09-22 outage, doc 3697)

- `test_unreachable_server_aborts_before_any_page` — unreachable server raises `ServerUnreachableError` (a `ChandraClientError`) before any page render; the poller aborts the cycle without recording failures.
- `test_unreachable_server_not_retried_with_safe_fallback` — the plugin's `MissingDependencyError` (unreachable) propagates as-is; no second, equally doomed ocrmypdf pass (no more doubled traceback).
- `test_other_ocr_failures_still_get_safe_fallback` — non-outage failures keep the paperless-style force_ocr fallback (first pass `redo_ocr`, retry `force_ocr`).
- `test_missing_dependency_discriminator_matches_plugin_messages` — the message regex separates the plugin's unreachable-server error (skip fallback) from its rejected-API-key error (config, fallback unchanged).

### Mixed-provenance ingest pass (document 5830 regression)

- `test_mixed_ingest_pass_ocrs_only_pages_needing_ocr` — mixed docs must not run ocrmypdf with `skip_text` (which skipped *every* text-bearing page → silent no-op). Asserts `--pages 2,3` + `redo_ocr`, no `skip_text`, no `sidecar`, mixed content composition (native + OCR pages).
- `test_mixed_ingest_pass_respects_max_pages_cap` — the mixed `--pages` list is intersected with `REARCHIVE_MAX_PAGES`.
- `test_mixed_ingest_pass_warns_when_all_pages_skipped` — guard: sidecar with a skip placeholder for every page logs a loud "made no changes" warning.

### Per-page action reporting (final pipeline log)

- `test_ingest_pass_reports_page_actions` — ingest pass records `page_actions` ({1: passthrough, 2-3: ocr}).
- `test_page_action_summary_formats_groups` — `_page_action_summary` groups into ranges with labels, ordered ocr/native/passthrough/error/skipped.
- `test_page_action_summary_empty_without_actions` — summary degrades to empty string without `page_actions`.

### Non-OCR-able originals (doc 5080 regression)

- `test_non_ocrable_original_is_skipped_with_marker_tag` — an Office-document original (.xls/.docx/...) resolves cleanly: trigger → `<trigger>-success` + `re-ocr-skipped`, audit note "digital-born, not PDF", engine never invoked.
- `test_dry_run_keeps_trigger_for_non_ocrable_original` — dry-run logs only, no tag changes or notes.
- `test_unrenderable_input_raises_clear_error` — unforeseen unrenderable input fails with a clear `RuntimeError`, not a raw PyMuPDF dump.
- `test_image_original_flows_through_content_path` — raster-image originals (JPG/PNG) render as single-page docs and OCR via the content path (blank-skip applies to PDFs only).

---

## test_config.py — settings (17)

- `test_backup_dir_is_hardwired` — backup dir is derived, not user-settable.
- `test_backup_dir_env_is_ignored` — a `REARCHIVE_BACKUP_DIR` env var is rejected/ignored.
- `test_archive_dir_default` / `test_archive_dir_from_env` — archive dir default + env override.
- `test_validate_rejects_backup_dir_equal_to_archive_dir` — safety: backup must not equal archive.
- `test_validate_rejects_backup_dir_inside_archive_dir` — nor nested inside it.
- `test_validate_sees_through_symlinks` — symlinked paths resolved before comparing.
- `test_validate_accepts_distinct_backup_dir` — valid configuration passes.
- `test_validate_noop_when_unset` — no dirs configured → validation is a no-op.
- `test_validate_rejects_backup_dir_that_is_a_file` — backup dir must be a directory.
- `test_prepare_backup_dir_creates_and_is_idempotent` — creation, rerunnable.
- `test_prepare_backup_dir_run_does_not_raise` / `test_prepare_backup_dir_dry_run_does_not_create` — dry-run creates nothing.
- `test_provenance_defaults` / `test_provenance_settings_from_env` — provenance knobs default + env parsing.
- `test_validate_rejects_unknown_provenance_mode` / `test_validate_rejects_unknown_mixed_mode` — enum validation for `REARCHIVE_PDF_PROVENANCE` / `REARCHIVE_OCR_MIXED_MODE`.

---

## test_ingest_args.py — ingest-parity layer (33)

Mirrors `paperless_chandra.parser` / paperless `parsers/tesseract.py` so a
re-OCR run drives the exact ocrmypdf invocation ingest drives, plus the
text helpers (`post_process_text`, `extract_pdf_text`, sidecar handling).

**Text normalization (paperless-ngx parity)**

- `test_post_process_text_normalises_whitespace` — collapse spaces, drop post-newline indentation.
- `test_post_process_text_matches_paperless_samples` — fixtures matching paperless-ngx behavior.

**ocrmypdf args construction**

- `test_base_args_mirror_ingest` — baseline kwarg set matches ingest.
- `test_redo_drops_deskew_and_keeps_clean` — deskew dropped under redo (ocrmypdf hard constraint), clean kept.
- `test_clean_final_with_redo_becomes_clean` / `test_clean_final_without_redo_is_clean_final` — clean-final downgrade rule.
- `test_safe_fallback_switches_to_force_ocr` / `test_safe_fallback_auto_keeps_deskew` — failure fallback ladder.
- `test_off_mode_uses_skip_text` / `test_skip_mode_uses_skip_text` — mode→flag mapping.
- `test_pages_xors_sidecar` — `--pages` and `--sidecar` are mutually exclusive (incl. the explicit `pages=` param for mixed runs).
- `test_user_args_merged_last_can_override` — `REARCHIVE_OCR_USER_ARGS` merged last, like ingest.
- `test_no_pdfa_no_color_strategy` — color strategy only for PDF/A output.
- `test_jobs_floor_is_one` — `jobs` clamped to ≥ 1.

**Mode resolution**

- `test_resolve_mode_auto_skips_digital_born` / `test_resolve_mode_auto_redoes_ocr_text` / `test_resolve_mode_auto_unchanged_without_text` — legacy `auto` heuristics.
- `test_resolve_mode_explicit_modes_untouched` — force/redo/off pass through.
- `test_resolve_mode_legacy_names_map_to_auto` — `skip`/`skip_noarchive` → `auto`.
- `test_resolve_mode_rejects_unknown` — invalid mode raises.

**Born-digital detection**

- `test_born_digital_threshold` — `PDF_TEXT_MIN_LENGTH` (50) boundary.
- `test_tagged_pdf_counts_as_born_digital` — tagged PDFs count regardless of length.
- `test_pdf_born_digital_text_uses_normalised_length` — threshold applies to normalized text.

**Sidecar**

- `test_sidecar_content_normalises` — sidecar text normalized like ingest.
- `test_sidecar_placeholder_discards_and_uses_pdftotext` — `[OCR skipped on page` → discard sidecar, pdftotext of output.
- `test_sidecar_missing_file_falls_back` — unreadable sidecar → pdftotext fallback.

**Provenance-driven mode (`effective_mode`)**

- `test_effective_mode_explicit_overrides_pass_through` — force/off unchanged.
- `test_effective_mode_text_based_disables_ocr` — text-based → off.
- `test_effective_mode_scanned_follows_configured_mode` — scanned → auto/redo as configured.
- `test_effective_mode_mixed_degrades_redo_to_skip` — mixed + redo → mixed_mode (engine then restricts to `--pages`).
- `test_effective_mode_unknown_falls_back_to_auto` — unknown → auto.
- `test_effective_mode_rejects_invalid_inputs` — bad kind/mode/mixed-mode raise.

**Born-digital text check**

- `test_has_visible_text_content_distinguishes_digital_born` — visible text vs invisible OCR overlays.

---

## test_model_check.py — model preflight (11)

Fail fast on a model the inference server does not serve, before any page
is OCR'd (the upstream retry ladder would otherwise waste ~40 s re-sending
a 404 once per page).

- `test_parse_model_ids_handles_openai_shape` — parse `/models` payload (OpenAI format).
- `test_parse_model_ids_tolerates_unexpected_payloads` — malformed payloads don't crash.
- `test_list_models_returns_none_for_empty_url` — no server URL → probe skipped (returns None).
- `test_ensure_model_served_rejects_unadvertised_model` — unserved model → `ModelNotServedError`.
- `test_model_error_logged_once_but_raised_every_call` — log dedup, exception every call.
- `test_ensure_model_served_accepts_advertised_model` — served model passes.
- `test_ensure_model_served_skips_when_probe_unavailable[None]` / `[models1]` — server without a usable `/models` endpoint → skip (don't block).
- `test_probe_runs_once_per_url_and_model` — result cached per (url, model).
- `test_probe_repeats_for_a_different_model` — cache is keyed by model too.
- `test_cached_models_for_returns_probe_result` — cache accessor.

---

## test_paperless_api.py — REST client (16)

- `test_tag_id_uses_supported_filter_param` / `test_tag_id_ignores_unrelated_results` — tag lookup filters and disambiguation.
- `test_ensure_tag_creates_when_absent` / `test_ensure_tag_reuses_existing` — idempotent tag creation.
- `test_custom_field_id_uses_supported_filter_param` / `test_custom_field_id_ignores_unrelated_results` — custom-field lookup.
- `test_ensure_custom_field_creates_when_absent` / `test_ensure_custom_field_reuses_existing` — idempotent field creation.
- `test_ensure_provenance_fields_covers_all_definitions` — `OCR engine`/`OCR date`/`OCR pages`/`OCR archive ratio` all ensured.
- `test_set_custom_fields_patch_shape` — PATCH body shape for custom fields.
- `test_set_custom_fields_empty_is_noop` — empty values → no request.
- `test_add_note_posts_to_notes_endpoint` — note POST shape.
- `test_add_note_accepts_notes_list_response` / `test_add_note_rejects_error_payload` — response parsing.
- `test_add_note_raises_on_http_error` — HTTP failure surfaces.
- `test_doc_ids_with_tag_paginates` — tag→document listing follows pagination.

---

## test_pipeline.py — document processing flow (5)

- `test_born_digital_is_preserved_without_writes` — born-digital doc: the provenance gate stops the run before anything is written.
- `test_force_tag_bypasses_the_gate` — the force modifier tag overrides the gate; every page OCR'd.
- `test_scanned_document_passes_provenance_to_engine` — scanned doc: verdict forwarded to the engine for mode derivation.
- `test_db_checksum_error_propagates_trigger_kept` — DB checksum failure → document fails, trigger tag kept for retry.
- `test_unrepairable_drift_fails_the_document` — unrepairable checksum drift → failure, not silent adoption.

---

## test_poller.py — poller loop (13)

- `test_signal_during_cycle_forces_immediate_cycle` / `test_signal_during_wait_returns_promptly` — SIGHUP semantics.
- `test_quiet_cycle_waits_full_interval` / `test_next_wait_idle_uses_full_interval` — idle → full poll interval.
- `test_next_wait_model_error_uses_full_interval` — model errors don't shorten the wait.
- `test_next_wait_progress_or_backlog_uses_active_interval` — work found → short active interval.
- `test_next_wait_backs_off_on_no_progress` / `test_next_wait_backoff_capped_at_full_interval` — backoff ladder, capped.
- `test_escalation_after_three_consecutive_failures` — 3 strikes → escalation.
- `test_success_resets_failure_counter` — success clears strikes.
- `test_dry_run_escalation_changes_no_tags` — dry-run writes nothing.
- `test_model_not_served_aborts_cycle_without_failure_strikes` — preflight abort is not a document failure.
- `test_server_outage_aborts_cycle_before_any_document` — unreachable server + queued backlog → cycle aborts before the first document: no attempts, no strikes, nothing escalated.

---

## test_server_check.py — reachability preflight (11)

- `test_outage_true_when_connection_refused` — URLError/ConnectionRefused → outage.
- `test_outage_true_on_timeout` — timeout → outage.
- `test_http_answer_is_not_an_outage` — any HTTP status (401/403/404/500) proves the server is up; not an outage.
- `test_outage_false_for_unusable_url` — empty URL is a config error, not an outage (no network call).
- `test_server_url_normalised_like_the_model_probe` — `http://ai:8000` → probe hits `http://ai:8000/v1/models`.
- `test_ensure_server_reachable_raises_chandra_client_error` — outage raises `ServerUnreachableError` (ChandraClientError subclass) with an actionable message.
- `test_ensure_server_reachable_quiet_when_up` — server answering → no exception.
- `test_ensure_server_reachable_skips_unusable_url` — empty URL → no exception, no probe.

---

## test_provenance.py — PDF provenance classification (8)

- `test_born_digital_is_text_based` — digital-born PDF → `text_based`.
- `test_scanned_is_scanned` — image-only PDF → `scanned`.
- `test_invisible_ocr_overlay_is_still_scanned` — invisible OCR text layer doesn't fool the classifier.
- `test_mixed_provenance` — mixed doc: per-page split, `pages_needing_ocr` + `native_markdown`.
- `test_max_pages_caps_inspection_but_keeps_total` — `provenance_max_pages` limits inspection, not the page count.
- `test_fallback_when_inspector_unavailable` — pdf-inspector missing → heuristic fallback.
- `test_classify_never_raises_on_garbage` — corrupt input → `unknown`, never an exception.
- `test_summary_reports_counts` — human summary string ("2 OCR / 1 native of 3 page(s)").

---

## test_replacer.py — archive replacement (9)

- `test_md5_of_file` — hashing helper (paperless-compatible checksum).
- `test_verify_checksum_ok_and_missing` / `test_verify_checksum_mismatch` — checksum verification paths.
- `test_replace_archive_atomic_and_backup` — atomic replace + old archive backed up first.
- `test_replace_archive_refuses_backup_inside_archive_dir` — safety: backup dir must not live inside the archive tree.
- `test_replace_archive_backup_dir_mirrors_relative_path` — backup mirrors the archive's subdirectory layout.
- `test_replace_archive_backup_dir_flattens_outside_archive_dir` / `test_backup_destination_flat_when_no_archive_dir` — layout fallbacks.
- `test_replace_archive_backup_dir_created_on_demand` — backup dir created if missing.

---

## test_restore.py — restore command (16)

Content restore re-extracts text from the *backed-up* archive with
`pdftotext -q -layout -enc UTF-8` and the same normalization the ingest
path uses (BOM-aware decode, NUL strip, paperless `post_process_text`).

- `test_post_process_text_collapses_spaces_keeps_newlines` / `test_post_process_text_nul_and_empty` — normalization parity (incl. whitespace-only → `""`, matching paperless-ngx).
- `test_find_backups_for_archive_recursive_sorted` / `test_find_backups_rejects_wrong_subdirectory` / `test_find_backups_missing_dir` — backup discovery.
- `test_match_backup_by_exact_and_bare_name` / `test_match_backup_recovers_archive_subdir` / `test_match_backup_by_glob` — backup selection (exact, bare name, glob).
- `test_resolve_numeric_operand_needs_no_db` — numeric document operand handling.
- `test_choose_backup_selection` — selection logic.
- `test_confirm_defaults_to_no` — confirmation prompt is safe by default.
- `test_restore_archive_copies_and_updates_checksum` — archive restore: file copied back, DB checksum updated.
- `test_restore_archive_declined` — declining aborts without changes.
- `test_restore_content_patches_cleaned_text` — content restore writes the re-extracted text.
- `test_cli_flags_mutually_exclusive` / `test_cli_version` — CLI surface.

---

## test_runner.py — legacy runner (30)

The pre-provenance runner, still used for non-PDF inputs and legacy paths.

- `test_build_args_defaults` / `test_build_args_image_dpi` / `test_build_args_max_pages` — argument building.
- `test_auto_prefers_redo_on_existing_text` / `test_redo_mode_drops_deskew` (args + runner level) / `test_force_mode_rasterises_and_keeps_deskew` / `test_no_deskew_when_disabled` — mode/deskew interactions.
- `test_mime_helpers` + `TestSniffMimeType::*` — magic-byte MIME sniffing with extension fallback.
- `test_a4_dpi_wide_image` — DPI math for A4/wide images.
- `test_settings_defaults` / `test_settings_bad_user_args` — settings validation.
- `TestFilenameFromDisposition::*` (5) — Content-Disposition filename parsing (quoted, RFC 5987, directory stripping, empty).
- `TestOcrSkippedAll::*` (4) — sidecar skip-placeholder detection (all-skipped vs real text vs empty).
- `TestSelectOcrStrategy::*` (5) — legacy strategy selection (force/redo/auto × text layer × digital-born × non-PDF).

---

## test_secrets.py — secret loading (5)

- `test_plain_var_when_no_file` — plain env var used when no `_FILE` variant.
- `test_file_var_wins_over_plain` — `VAR_FILE` takes precedence over `VAR`.
- `test_file_content_is_stripped` — trailing whitespace/newline stripped.
- `test_missing_file_raises` — missing secret file is a hard error.
- `test_unset_returns_empty` — unset → empty string.

---

## test_version.py — versioning (2)

- `test_version_is_single_sourced_from_installed_metadata` — version comes from package metadata only.
- `test_version_is_not_the_placeholder` — guards against a stale placeholder version.

---

## Integration tooling

`tests/integration/` is **not** part of pytest — these scripts exercise the
live paperless-ngx / Postgres / Chandra stack. See
`tests/integration/README.md` for the full runbook. Highlights:

| Script | Purpose |
| --- | --- |
| `check_archive_provenance.sh` | Verify an archive's text layer was produced by Chandra (P1 acceptance check). |
| `inspect_pdf.py` | Classify PDFs with pdf-inspector (`--per-page`, `--decide` for the gate decision). |
| `e2e_one.py <doc_id>` | Run the full pipeline for one document, including writes and tag swap. |
| `run_poller_e2e.sh` | One real poll cycle (`REARCHIVE_RUN_ONCE=true`) as deployed. |
| `run_content_e2e.sh` | End-to-end `re-ocr-content`: content updated, archive byte-identical. |
| `reset_baseline.sh` | Restore a test document to a clean pre-run baseline. |
| `verify_state.sh` | Dump DB row, tags, archive dir, checksums for a document. |
| `pdfinfo_compare.sh` / `size_compare.py` | Compare archive metadata/size before vs after OCR. |
| `check_download.py` / `check_tags.py` / `diag_original.py` | API behavior probes. |

---

## Regression notes

Recent behavior changes each carry a dedicated test:

1. **Mixed-provenance no-op fix** (doc 5830): `test_mixed_ingest_pass_ocrs_only_pages_needing_ocr` — mixed docs OCR only `pages_needing_ocr` via `--pages` + redo instead of a whole-document `skip_text` pass that could skip everything (the silent no-op).
2. **Silent no-op guard**: `test_mixed_ingest_pass_warns_when_all_pages_skipped` — a run that changed nothing logs a loud warning instead of reporting success.
3. **paperless-ngx text parity**: `test_post_process_text_*` in `test_ingest_args.py` and `test_restore.py` — `pdftotext -q -layout -enc UTF-8`, BOM-aware decode, NUL stripping, whitespace-only → `""`.
4. **Per-page provenance reporting**: the `OCR pages` custom field and the final log line derive from `page_actions` — `test_ingest_pass_reports_page_actions`, `test_page_action_summary_formats_groups`.
5. **Deskew observability**: the drop under redo (incl. the mixed `--pages` path) is logged; covered by `test_redo_drops_deskew_and_keeps_clean` and `test_redo_mode_drops_deskew`.
6. **Non-OCR-able originals** (doc 5080): Office-document originals skip cleanly with `re-ocr-skipped` instead of crashing 3 cycles — `test_non_ocrable_original_is_skipped_with_marker_tag` et al. Raster-image originals flow through the content path.
