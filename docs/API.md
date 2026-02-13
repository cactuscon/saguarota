# SaguarOTA API Reference

Related docs:
- [Project README](../README.md)
- [Guided usage and best practices](GUIDE.md)
- [Configuration profiles](CONFIG_PROFILES.md)
- [Guide: production guidance](GUIDE.md#8-production-guidance)
- [Guide: delete policy behavior](GUIDE.md#7-deleting-files-missing-from-manifest)

## Device-side API (`saguarota.OTAUpdater`)

### Constructor

```python
OTAUpdater(
    manifest_url,
    base_file_url,
    dest_dir=".",
    force_update=False,
    recurse_http_fs=False,
    ota_state_file=None,
    local_manifest_file=None,
    application_name=None,
    http_timeout_s=5,
    backup_skip_extensions=None,
    backup_skip_prefixes=None,
    manifest_auth_key=None,
    manifest_signature_field="signature",
    download_retries=1,
    retry_base_delay_ms=250,
    resume_downloads=False,
    io_chunk_size=512,
    md5_chunk_size=512,
    strict_http_fs=True,
    delete_files_not_in_manifest_policy="never",
    delete_files_not_in_manifest_extensions=None,
    progress_callback=None,
    **kwargs,
)
```

Parameters:
- `manifest_url`: URL to remote manifest (`versions.json`).
- `base_file_url`: base URL for file downloads.
- `dest_dir`: destination root for applied files.
- `force_update`: if `True`, applies remote manifest even if version is not newer.
- `recurse_http_fs`: if `True`, disable manifest mode and crawl HTTP directory listing.
- `ota_state_file`: path to OTA state marker (default `ota_state.txt`).
- `local_manifest_file`: path to local manifest cache (default `versions.json`).
- `application_name`: used to derive backup dir.
- `http_timeout_s`: HTTP timeout seconds (`None` disables).
- `backup_skip_extensions`: tuple/list of extensions to skip backing up.
- `backup_skip_prefixes`: tuple/list of path prefixes to skip backing up.
- `manifest_auth_key`: optional shared secret for manifest HMAC-SHA256 verification.
- `manifest_signature_field`: signature field name in manifest.
- `download_retries`: retries after initial attempt.
- `retry_base_delay_ms`: exponential backoff base delay.
- `resume_downloads`: use `.part` + HTTP range resume in manifest mode downloads.
- `io_chunk_size`: chunk size for file copy/download operations.
- `md5_chunk_size`: chunk size for MD5 verification operations.
- `strict_http_fs`: fail entire HTTP-FS update if any file download fails.
- `delete_files_not_in_manifest_policy`: explicit deletion policy for files present on
  device but missing from manifest:
  - `"never"` (default)
  - `"manifest_extensions"` (delete extras only when extension is both in current manifest entries and your explicit extension allowlist)
  - `"custom_extensions"` (delete extras only for configured extension allowlist)
  - `"all"` (delete any extra file in update scope)
- `delete_files_not_in_manifest_extensions`: explicit extension allowlist used by
  `"manifest_extensions"` and `"custom_extensions"` (e.g. `(".py", ".mpy")`).
  If omitted for those policies, delete behavior is disabled (`"never"`).
- `progress_callback`: optional `callback(event_name, details_dict)`.
- `**kwargs`: unknown options are accepted and ignored for forward compatibility.

Tip:
- Prefer class constants over raw strings where available (`OTADeletePolicy.*`, `OTAErrorCode.*`, `OTAState.*`).
- `manifest_auth_key` protects manifest integrity/authenticity, not transport confidentiality.

Default backup skip extensions:
- `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.rgb565`, `.raw`, `.bin`, `.ttf`, `.otf`, `.woff`

Default backup skip prefixes:
- `assets/`, `static/`, `media/`, `images/`, `fonts/`

Behavior notes:
- If `recurse_http_fs=True`, `manifest_url` is ignored and recursive HTTP listing mode is used.
- HTTP-FS mode does not perform manifest signature checks or per-file MD5 validation.
- `resume_downloads` is best-effort and depends on server support for HTTP Range requests.
- File MD5 verification remains the file integrity mechanism; HMAC-SHA256 is manifest-level authenticity only.
- OTA flow is backup-first, then download/write directly to destination file paths.
- In manifest mode, extra-file deletion (if enabled) runs after downloads/verification and before local manifest write.
- Extra files selected for deletion are copied into backup first, so rollback can restore them until cleanup.
- Before creating backup dir, updater warns if free filesystem space is below 40%.
- `OTADeletePolicy.ALL` is intentionally destructive; use only when `dest_dir` is scoped to OTA-managed content.

### Runtime Attributes

- `last_error_code`: structured error code string or `None`.
- `last_error_message`: detail string or `None`.
- `backup_dir`: derived as `<application_name>_backup`.

### Public Methods

- `check_and_perform_ota()`
  - Main entry point.
  - Handles interrupted-install recovery (`installing` state -> revert).
  - Blocks further update checks when confirmation is pending.
  - May reboot device as part of success/failure flows.

- `confirm_update(cleanup=False) -> bool`
  - Confirms a pending update (`confirm_pending -> idle`).
  - Optional cleanup of backup files.
  - Call only after successful post-update validation (for example: critical imports and app startup checks).

- `cleanup_files() -> bool`
  - Removes backup dir when safe.
  - Returns `False` if confirmation is still pending.
  - Intended to run after validation; avoid calling it immediately after install.

- `release() -> None`
  - Best-effort teardown for constrained devices.
  - Clears/deletes instance attributes and triggers garbage collection.

- `revert_update()`
  - Restores backed-up files, sets state idle, then resets device.

### OTA State Values (`saguarota.OTAState`)

- `OTAState.IDLE` -> `"idle"`
- `OTAState.INSTALLING` -> `"installing"`
- `OTAState.CONFIRM_PENDING` -> `"confirm_pending"`

### Delete Policy Values (`saguarota.OTADeletePolicy`)

- `OTADeletePolicy.NEVER` -> `"never"`
- `OTADeletePolicy.MANIFEST_EXTENSIONS` -> `"manifest_extensions"`
- `OTADeletePolicy.CUSTOM_EXTENSIONS` -> `"custom_extensions"`
- `OTADeletePolicy.ALL` -> `"all"`

### Progress Events

When `progress_callback` is set:
- `update_start`: `{ "mode": "manifest" | "http_fs" }`
- `update_applied`: `{ "mode": "manifest" | "http_fs" }`
- `file_update_start`: manifest mode -> `{ path, index, total, from, to }`; HTTP-FS mode -> `{ path, mode }`
- `file_update_done`: manifest mode -> `{ path, index, total }`; HTTP-FS mode -> `{ path, mode }`
- `file_update_skip`: `{ path, index, total }`
- `file_update_failed`: `{ path, mode, error }` (HTTP-FS mode)
- `download_attempt`: `{ url, path, attempt, attempts }`
- `download_retry`: `{ url, path, attempt, wait_ms }`
- `file_delete_extra`: `{ path, policy }`

### Structured Error Codes

Available as `OTAErrorCode` constants:
- `OTAErrorCode.MANIFEST_FETCH` -> `"manifest_fetch_failed"`
- `OTAErrorCode.MANIFEST_SIGNATURE` -> `"manifest_signature_invalid"`
- `OTAErrorCode.DOWNLOAD` -> `"download_failed"`
- `OTAErrorCode.MD5` -> `"md5_mismatch"`
- `OTAErrorCode.APPLY` -> `"apply_failed"`
- `OTAErrorCode.HTTP_FS` -> `"http_fs_failed"`
- `OTAErrorCode.DELETE_EXTRAS` -> `"delete_extraneous_failed"`

## Host-side API (`saguarota.py3utils`)

### `OTAManifestBuilder`

```python
OTAManifestBuilder(
    src_dir,
    auth_key=None,
    signature_field="signature",
    allowed_extensions=None,
    exclude_prefixes=None,
    exclude_folders=None,
    followlinks=True,
    json_indent=2,
    version_source="mtime",
    previous_manifest_path=None,
    reuse_unchanged_versions=False,
    git_executable="git",
)
```

Methods:
- `generate_manifest_data() -> dict`
  - Returns manifest as a Python dict.
- `generate_manifest() -> str`
  - Scans source tree and emits manifest JSON.
  - Includes `version` and per-file `path`, `version`, `md5`.
  - If `auth_key` set, adds HMAC-SHA256 signature field.
  - File inclusion/exclusion filters are configurable via constructor options.
- `write_manifest(output_path) -> dict`
  - Generates and writes manifest JSON to disk.
  - Returns the manifest dict.

CI-oriented options:
- `version_source`: `"mtime"` (default) or `"git_commit_time"`.
- `previous_manifest_path`: path to prior `versions.json`.
- `reuse_unchanged_versions`: preserve previous per-file version when MD5 is unchanged.
- `git_executable`: git binary/path for git-based version lookup.

### `OTAManifestServer`

```python
OTAManifestServer(src_dir, host="localhost", port=8000, builder=None)
```

Methods:
- `start(background=False)`
- `stop()`

Routes:
- `/ota/versions.json`
- `/ota/<file>`

Notes:
- Traversal outside `src_dir` is blocked.
- Intended for development/testing only.
