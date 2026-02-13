# SaguarOTA: OTA Updates For MicroPython

SaguarOTA is an OTA update library for MicroPython projects (especially ESP32-class devices) with host-side tools for manifest generation and local serving. This package was developed as a submodule for the [CactusCon 14](https://github.com/cactuscon/cactuscon14) hardware badge.

It is optimized for constrained environments:
- File integrity uses MD5 (fast, low-memory, small manifests).
- Downloads are streamed in chunks.
- Updates back up existing files first, then write new files directly to destination.
- Optional manifest authenticity uses HMAC-SHA256 (without changing per-file MD5).

## Quick Start

### Install With mpremote/mip

Install directly from GitHub:

```bash
mpremote mip install github:cactuscon/saguarota
```

Install from a local checkout:

```bash
mpremote mip install .
```

Then import on-device:

```python
from saguarota import OTAUpdater
```

### Device (MicroPython)

```python
# boot.py
from saguarota import OTAUpdater

ota = OTAUpdater(
    manifest_url="http://your-server/ota/versions.json",
    base_file_url="http://your-server/ota/",
    download_retries=2,
    resume_downloads=True,
    strict_http_fs=True,
)

# Perform OTA check/install (may reboot on success/failure paths)
ota.check_and_perform_ota()
```

```python
# main.py
from saguarota import OTAUpdater

# After reboot, validate app health (e.g., import critical modules), then confirm and clean backups.
ota = OTAUpdater(
    manifest_url="http://your-server/ota/versions.json",
    base_file_url="http://your-server/ota/",
)
ota.confirm_update(cleanup=True)
```

Do not clean backups until post-update import/runtime validation has passed.

### Host (CPython)

```python
from saguarota.py3utils import OTAManifestBuilder, OTAManifestServer

builder = OTAManifestBuilder("src")
with open("versions.json", "w") as f:
    f.write(builder.generate_manifest())

server = OTAManifestServer("src", host="0.0.0.0", port=8000)
server.start()
```

CI usage without running a server:

```python
from saguarota.py3utils import OTAManifestBuilder

builder = OTAManifestBuilder(
    "releases/badge",
    allowed_extensions={".py", ".mpy", ".raw", ".rgb565", ".c"},
    exclude_folders={"example", "examples", "tests"},
    version_source="git_commit_time",
    previous_manifest_path="releases/badge/versions.json",
    reuse_unchanged_versions=True,
)
builder.write_manifest("releases/badge/versions.json")
```

## Documentation

- [API reference](docs/API.md)
- [Guided usage and best practices](docs/GUIDE.md)
- [Configuration profiles](docs/CONFIG_PROFILES.md)
- [Recommended OTA flow](docs/GUIDE.md#1-recommended-ota-flow)
- [CI manifest generation workflow example](docs/GUIDE.md#10-ci-manifest-generation-without-dev-server)
- [Prune/delete policy guidance](docs/GUIDE.md#7-deleting-files-missing-from-manifest)

Note: mirror/prune-style deletion is supported via explicit
`delete_files_not_in_manifest_policy` options; see `docs/GUIDE.md`.

## Import Surface

- MicroPython:
  - `from saguarota import OTAUpdater, OTAState, OTAErrorCode, OTADeletePolicy`
- CPython host tools:
  - `from saguarota.py3utils import OTAManifestBuilder, OTAManifestServer`

## Project Layout

```text
saguarota/
  __init__.py            # package exports (device/host split)
  saguarota.py           # device-side updater
  py3utils.py            # host-side builder/server
docs/
  API.md
  GUIDE.md
```

## Security Note

`OTAManifestServer` is a dev/test server, not a production update service. For production, use a hardened HTTPS deployment and authentication controls.

Recommended production baseline:
- Use manifest mode (not `recurse_http_fs=True`).
- Set `manifest_auth_key` on device and `auth_key` in manifest generation.
- Keep `strict_http_fs=True` if HTTP-FS mode is ever used.
- Avoid `delete_files_not_in_manifest_policy=OTADeletePolicy.ALL` unless `dest_dir` is a tightly scoped update directory.
- Keep backups until app health checks pass, then call `confirm_update(cleanup=True)`.

## State During CactusCon 14 (Historical)

Before the current hardening pass, the OTA risk profile was more exposed in two practical attacker models:

- Attacker between device and update server:
  - In plain HTTP deployments, manifest/file tampering and replay/downgrade scenarios were more plausible without consistently enforced authenticity controls.
  - Weaker transfer/recovery behavior increased susceptibility to availability attacks (forced update failures, repeated rollback loops).

- Attacker with device console access:
  - Local OTA control files (state/manifest) could be modified to interfere with normal update flow (block updates, force reverts, or induce unsafe timing).
  - Destructive update options were easier to misapply in a way that could remove non-target files if configuration scope was not carefully constrained.

## State Flow (Device)

- `idle` -> normal operation, update checks allowed
- `installing` -> update in progress; interruption triggers rollback on next boot
- `confirm_pending` -> update applied; app must call `confirm_update()` before next OTA cycle

The updater prints a warning before backup creation if free filesystem space is below 40%.
