# SaguarOTA Configuration Profiles

Related docs:
- [Project README](../README.md)
- [API reference](API.md)
- [Guided usage and best practices](GUIDE.md)
- [Guide: production guidance](GUIDE.md#8-production-guidance)
- [Guide: CI workflow example](GUIDE.md#10-ci-manifest-generation-without-dev-server)

These presets are starting points. Tune based on your network quality, flash size, and boot-time budget.
Example URLs use local HTTP endpoints for readability. For production, use HTTPS and trusted auth controls.

## 1. Dev Profile (Fast Iteration)

Use when you want quick feedback and can tolerate less strict behavior.

```python
ota = OTAUpdater(
    manifest_url="http://dev-host/ota/versions.json",
    base_file_url="http://dev-host/ota/",
    http_timeout_s=3,
    force_update=True,
    download_retries=0,
    retry_base_delay_ms=100,
    resume_downloads=False,
    strict_http_fs=False,
    io_chunk_size=1024,
    md5_chunk_size=1024,
)
```

Tradeoffs:
- Faster checks/installs
- Less resilience to transient network failures

## 2. Balanced Profile (Default Recommendation)

Use for most deployments.

```python
ota = OTAUpdater(
    manifest_url="http://updates.local/ota/versions.json",
    base_file_url="http://updates.local/ota/",
    http_timeout_s=5,
    force_update=False,
    download_retries=2,
    retry_base_delay_ms=250,
    resume_downloads=True,
    strict_http_fs=True,
    io_chunk_size=512,
    md5_chunk_size=512,
)
```

Tradeoffs:
- Good reliability without excessive startup delay
- Reasonable memory footprint on ESP32-class devices

## 3. Low-Memory Profile (Tight RAM)

Use when heap pressure is high.

```python
ota = OTAUpdater(
    manifest_url="http://updates.local/ota/versions.json",
    base_file_url="http://updates.local/ota/",
    http_timeout_s=6,
    download_retries=1,
    retry_base_delay_ms=300,
    resume_downloads=True,
    strict_http_fs=True,
    io_chunk_size=256,
    md5_chunk_size=256,
)
```

Tradeoffs:
- Lower peak memory use
- Slower hashing/copy/download operations

## 4. High-Reliability Profile (Unstable Network)

Use when links are noisy and update success is critical.

```python
ota = OTAUpdater(
    manifest_url="http://updates.local/ota/versions.json",
    base_file_url="http://updates.local/ota/",
    http_timeout_s=8,
    download_retries=4,
    retry_base_delay_ms=400,
    resume_downloads=True,
    strict_http_fs=True,
    io_chunk_size=512,
    md5_chunk_size=512,
    manifest_auth_key="shared-secret",
    manifest_signature_field="signature",
)
```

Tradeoffs:
- Much better resilience and authenticity checking
- Longer wait time before giving up on failed networks
- Note: `manifest_auth_key`/manifest signatures protect manifest integrity, not transport confidentiality.

## 5. Backup Policy Variants

If your rollback safety matters more than storage:

```python
ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    backup_skip_extensions=(),
    backup_skip_prefixes=(),
)
```

If your storage is tight and assets are disposable:

```python
ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    backup_skip_extensions=(".raw", ".rgb565", ".bin"),
    backup_skip_prefixes=("assets/", "media/", "images/"),
)
```

## 6. Prune/Mirror-Like Delete Profiles (Manifest Mode Only)

This is always explicit because it deletes files not in the manifest.

Code-focused pruning:

```python
from saguarota import OTADeletePolicy, OTAUpdater

ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    delete_files_not_in_manifest_policy=OTADeletePolicy.CUSTOM_EXTENSIONS,
    delete_files_not_in_manifest_extensions=(".py", ".mpy", ".json"),
)
```

Manifest-extension pruning:

```python
from saguarota import OTADeletePolicy, OTAUpdater

ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    delete_files_not_in_manifest_policy=OTADeletePolicy.MANIFEST_EXTENSIONS,
    delete_files_not_in_manifest_extensions=(".py", ".mpy", ".json"),
)
```

Aggressive full prune (use carefully):

```python
from saguarota import OTADeletePolicy, OTAUpdater

ota = OTAUpdater(
    manifest_url=...,
    base_file_url=...,
    delete_files_not_in_manifest_policy=OTADeletePolicy.ALL,
)
```

Safety note:
- Use `OTADeletePolicy.ALL` only when `dest_dir` is isolated to OTA-managed files.

## 7. Confirmation Pattern (Recommended)

Regardless of profile, keep this lifecycle:

```python
# 1) Try update
ota.check_and_perform_ota()

# 2) After reboot + app health checks
ota.confirm_update(cleanup=True)
```

This preserves rollback safety until your application confirms the update is healthy.
