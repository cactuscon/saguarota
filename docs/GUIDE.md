# SaguarOTA Guided Usage and Best Practices

Related docs:
- [Project README](../README.md)
- [API reference](API.md)
- [Configuration profiles](CONFIG_PROFILES.md)
- [Profiles: confirmation lifecycle](CONFIG_PROFILES.md#7-confirmation-pattern-recommended)
- [Profiles: prune/delete options](CONFIG_PROFILES.md#6-prunemirror-like-delete-profiles-manifest-mode-only)

## 1. Recommended OTA Flow

At startup:
1. In `boot.py`, run `check_and_perform_ota()` early.
2. Reboot into new code (if update applied).
3. In `main.py`, import and initialize critical modules/app.
4. Only after those imports/startup checks pass, call `confirm_update(cleanup=True)`.

Example split by startup phase:

```python
# boot.py
from saguarota import OTAUpdater

ota = OTAUpdater(
    manifest_url="http://updates.local/ota/versions.json",
    base_file_url="http://updates.local/ota/",
    http_timeout_s=5,
    download_retries=2,
    retry_base_delay_ms=250,
    resume_downloads=True,
    io_chunk_size=512,
    md5_chunk_size=512,
)

# Check/apply updates early in boot.
ota.check_and_perform_ota()
```

```python
# main.py
from saguarota import OTAUpdater

ota = OTAUpdater(
    manifest_url="http://updates.local/ota/versions.json",
    base_file_url="http://updates.local/ota/",
)

# Import/init your app first, then confirm and clean up.
# Example:
# import my_app
# my_app.start()
ota.confirm_update(cleanup=True)
```

Local reference pattern:
- OTA check phase: `../CactusCon14.dev/badge/src/boot.py`
- Post-import confirmation/cleanup phase: `../CactusCon14.dev/badge/src/main.py`

Important:
- Do not call `cleanup_files()` immediately after update/install.
- Keep backup files until your app proves it can import and start correctly.

## 2. Manifest Mode vs HTTP-FS Mode

### Manifest mode (recommended)
- Uses `versions.json` metadata.
- Incremental file updates by version.
- For each changed file: backup existing file, then download/write new file directly to destination.
- MD5 file verification.
- Optional manifest authenticity with HMAC-SHA256.

### HTTP-FS recursive mode
Enable with `recurse_http_fs=True`.
- Crawls directory listing links (`href="..."`) and downloads files.
- Does not use manifest versions.
- Does not perform manifest signature checks or per-file MD5 validation.
- Uses the same retry/backoff download path as manifest mode.
- By default (`strict_http_fs=True`) a single failed file aborts update and triggers rollback.
- Intended for simpler/dev scenarios.

Recommended when using HTTP-FS mode:
- Keep `strict_http_fs=True` unless partial updates are explicitly acceptable.
- Pair with bounded retries (`download_retries`) to avoid long blocking startup loops.

## 3. Integrity and Authenticity Model

- Per-file integrity: MD5 (chosen for speed and low memory overhead on constrained devices).
- Optional manifest authenticity: HMAC-SHA256 using `manifest_auth_key` on device and `auth_key` on builder.
- HMAC signing validates manifest authenticity/integrity, but does not encrypt traffic.

If authenticity is required, configure both sides with the same key.
Do not hardcode shared secrets in public repos or committed firmware sources.

## 4. Backup Policy Defaults

By default, backup skips likely large/static assets:
- Extensions: `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.rgb565`, `.raw`, `.bin`, `.ttf`, `.otf`, `.woff`
- Prefixes: `assets/`, `static/`, `media/`, `images/`, `fonts/`

Override as needed:

```python
ota = OTAUpdater(
    manifest_url=..., base_file_url=...,
    backup_skip_extensions=(".raw", ".rgb565"),
    backup_skip_prefixes=("assets/generated/",),
)
```

## 5. Error Handling and Telemetry

Inspect:
- `ota.last_error_code`
- `ota.last_error_message`

Optional callback:

```python
def on_progress(event, details):
    print(event, details)

ota = OTAUpdater(..., progress_callback=on_progress)
```

Use this for lightweight status reporting and troubleshooting.

Useful tunables:
- `http_timeout_s`: request timeout
- `download_retries`: retry count after first failure
- `retry_base_delay_ms`: exponential backoff base
- `io_chunk_size`: copy/download chunk size
- `md5_chunk_size`: MD5 hashing chunk size

Preflight warning:
- Updater warns before backup creation when free VFS space is below 40%.

## 6. Edge Cases and Recovery Behavior

- Interrupted update (`ota_state == "installing"`): next `check_and_perform_ota()` triggers rollback.
- Pending confirmation (`ota_state == "confirm_pending"`): updater will not start a new update until confirmed or reverted.
- `cleanup_files()` while confirmation is pending: returns `False` and does nothing.
- This is intentional: cleanup should happen only after post-update import/runtime validation.
- Missing MD5 in manifest entry: file is downloaded with warning and no hash validation.

## 7. Deleting Files Missing From Manifest

This behavior is **off by default** and is intentionally explicit because it deletes files.

Use `delete_files_not_in_manifest_policy`:
- `"never"`: do not delete extras.
- `"manifest_extensions"`: delete extras only when extension is both:
  1) present in current manifest file types, and
  2) present in your explicit extension allowlist.
- `"custom_extensions"`: delete extras only for your explicit extension allowlist.
- `"all"`: delete any extra file in scope (most aggressive).

Important:
- Extension-scoped policies (`"manifest_extensions"` and `"custom_extensions"`) require
  `delete_files_not_in_manifest_extensions`. If omitted, delete behavior is disabled.
- Files selected for deletion are backed up first. If install fails or you run `revert_update()`
  before confirmation cleanup, those deleted files are restored from backup.

Example: safe code-only pruning

```python
from saguarota import OTADeletePolicy, OTAUpdater

ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    delete_files_not_in_manifest_policy=OTADeletePolicy.CUSTOM_EXTENSIONS,
    delete_files_not_in_manifest_extensions=(".py", ".mpy", ".json"),
)
```

Example: rsync-like pruning for manifest-managed file types

```python
from saguarota import OTADeletePolicy, OTAUpdater

ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    delete_files_not_in_manifest_policy=OTADeletePolicy.MANIFEST_EXTENSIONS,
    delete_files_not_in_manifest_extensions=(".py", ".mpy", ".json"),
)
```

Why extension policies matter:
- Many deployments intentionally do not include large binary assets in the manifest.
- Extension-scoped pruning avoids deleting those unmanifested binaries.

## 8. Production Guidance

- Serve updates over trusted transport and authentication.
- Prefer HTTPS for production update endpoints.
- Keep manifests small and deterministic.
- Test power-loss scenarios (during download/apply/reboot).
- Keep retries bounded for predictable startup behavior.
- Use `dest_dir` consistently with your deployed filesystem layout.
- Keep backup skip policy aligned with your rollback requirements (`backup_skip_extensions`, `backup_skip_prefixes`).
- Only call `confirm_update(cleanup=True)` after runtime validation that the new code works
  (for example: successful import of critical modules and main app startup checks).
- Prefer manifest mode for production; treat `recurse_http_fs=True` as development-oriented.
- Avoid `OTADeletePolicy.ALL` unless your destination directory is OTA-only and isolated.

## 9. Host Tooling Example (Signed Manifest)

```python
from saguarota.py3utils import OTAManifestBuilder

builder = OTAManifestBuilder(
    "src",
    auth_key="shared-secret",
    signature_field="signature",
    allowed_extensions={".py", ".mpy", ".raw", ".rgb565"},
    exclude_folders={"tests", "docs"},
)

with open("versions.json", "w") as f:
    f.write(builder.generate_manifest())
```

## 10. CI Manifest Generation (Without Dev Server)

For CI pipelines, use `OTAManifestBuilder` directly and write `versions.json` as an artifact/output.

Generalized GitHub Actions workflow example:

```yaml
name: Build OTA Manifest

on:
  push:
    branches: [ main ]
    paths:
      - "ota_payload/**"
      - ".github/workflows/build-ota-manifest.yml"
  workflow_dispatch:

jobs:
  manifest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install SaguarOTA
        run: pip install saguarota

      - name: Generate versions.json
        run: |
          python - <<'PY'
          from pathlib import Path
          from saguarota.py3utils import OTAManifestBuilder

          payload_dir = Path("ota_payload")
          manifest_path = payload_dir / "versions.json"

          builder = OTAManifestBuilder(
              src_dir=str(payload_dir),
              allowed_extensions={".py", ".mpy", ".json"},
              exclude_folders={"tests", "examples", "docs"},
              version_source="git_commit_time",
              previous_manifest_path=str(manifest_path),
              reuse_unchanged_versions=True,
          )
          manifest = builder.write_manifest(str(manifest_path))
          print("Manifest version:", manifest["version"])
          print("File count:", len(manifest["files"]))
          PY

      - name: Upload OTA payload artifact
        uses: actions/upload-artifact@v4
        with:
          name: ota-payload
          path: ota_payload/
```

Adjust `payload_dir`, extension allowlist, and excluded folders to match your project layout.

Example follow-up deployment job for a self-hosted runner:

```yaml
  deploy:
    needs: manifest
    runs-on: self-hosted
    steps:
      - name: Download OTA artifact
        uses: actions/download-artifact@v4
        with:
          name: ota-payload
          path: ./ota-payload

      - name: Publish to HTTP directory
        run: |
          OTA_HTTP_DIR="/var/www/ota"
          mkdir -p "${OTA_HTTP_DIR}"
          rsync -av --delete ./ota-payload/ "${OTA_HTTP_DIR}/"
```

Notes:
- Use a runner that has write access to your HTTP publish directory.
- Keep the publish path stable so device URLs do not change.
- `--delete` mirrors exactly; remove it if you do not want server-side pruning.
