"""
saguarota - Robust OTA updater for MicroPython on ESP32-S3

This module provides the OTAUpdater class for robust over-the-air updates.

Features:
  - Downloads and verifies only updated files using a manifest.
  - Streams files in small chunks for low-memory devices.
  - Backs up replaced files and can revert on failure.
  - All configuration is per-instance and customizable.

Example usage:
    from saguarota import OTAUpdater, OTAState
    updater = OTAUpdater(
        manifest_url="http://example.com/ota/versions.json",
        base_file_url="http://example.com/ota/files"
    )
    updater.check_and_perform_ota()

See README.md for more details.
"""

import os
import gc
try:
    import utime as _time
except ImportError:
    import time as _time

try:
    import ujson as json
except ImportError:
    import json
import ubinascii
import uhashlib
import urequests
import machine


# --- OTA State Enum-like class ---
class OTAState:
    IDLE = "idle"
    INSTALLING = "installing"
    CONFIRM_PENDING = "confirm_pending"


class OTAErrorCode:
    MANIFEST_FETCH = "manifest_fetch_failed"
    MANIFEST_SIGNATURE = "manifest_signature_invalid"
    DOWNLOAD = "download_failed"
    MD5 = "md5_mismatch"
    APPLY = "apply_failed"
    HTTP_FS = "http_fs_failed"
    DELETE_EXTRAS = "delete_extraneous_failed"


class OTADeletePolicy:
    NEVER = "never"
    MANIFEST_EXTENSIONS = "manifest_extensions"
    CUSTOM_EXTENSIONS = "custom_extensions"
    ALL = "all"
    ALL_POLICIES = (
        NEVER,
        MANIFEST_EXTENSIONS,
        CUSTOM_EXTENSIONS,
        ALL,
    )


class OTAUpdater:

    OTA_STATE_FILE = "ota_state.txt"
    LOCAL_MANIFEST_FILE = "versions.json"
    APPLICATION_NAME = "saguarota"
    DEFAULT_BACKUP_SKIP_EXTENSIONS = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".rgb565",
        ".raw",
        ".bin",
        ".ttf",
        ".otf",
        ".woff"
    )
    DEFAULT_BACKUP_SKIP_PREFIXES = (
        "assets/",
        "static/",
        "media/",
        "images/",
        "fonts/",
    )
    def __init__(
        self,
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
    ):
        """
        manifest_url: URL to the JSON manifest on the server.
        base_file_url: Base URL from which to download files; file paths from the manifest are appended.
        ota_state_file, local_manifest_file, application_name: override class defaults if desired.
        dest_dir: Directory on the device where updated files will be placed (default: current directory).
        http_timeout_s: Socket timeout in seconds for HTTP requests (None to disable).
        manifest_auth_key: Optional shared secret for manifest HMAC verification.
        manifest_signature_field: Manifest field name containing signature hex.
        download_retries: Number of retries after an initial download attempt.
        retry_base_delay_ms: Base delay used for exponential backoff between retries.
        resume_downloads: Attempt HTTP range resume using `.part` files.
        io_chunk_size: Chunk size for copy/download operations.
        md5_chunk_size: Chunk size for MD5 verification.
        strict_http_fs: If True, fail HTTP-FS update when any file download fails.
        delete_files_not_in_manifest_policy: Policy for deleting files that are present on
            device but missing from current manifest. Values:
            "never", "manifest_extensions", "custom_extensions", "all".
        delete_files_not_in_manifest_extensions: Extensions used when policy is
            "custom_extensions".
        progress_callback: Optional callback(event_name, details_dict).
        kwargs: Extra future/unknown options are accepted and ignored for compatibility.
        """
        self.manifest_url = manifest_url
        self.base_file_url = base_file_url
        self.force_update = force_update
        self.ota_state_file = ota_state_file or self.OTA_STATE_FILE
        self.local_manifest_file = local_manifest_file or self.LOCAL_MANIFEST_FILE
        self.application_name = application_name or self.APPLICATION_NAME
        self.http_timeout_s = http_timeout_s
        self.backup_dir = f"{self.application_name}_backup"
        self.dest_dir = dest_dir or "."
        self.backup_skip_extensions = tuple(
            backup_skip_extensions or self.DEFAULT_BACKUP_SKIP_EXTENSIONS
        )
        self.backup_skip_prefixes = tuple(
            backup_skip_prefixes or self.DEFAULT_BACKUP_SKIP_PREFIXES
        )
        self.manifest_auth_key = manifest_auth_key
        self.manifest_signature_field = manifest_signature_field
        self.download_retries = self._coerce_non_negative_int(download_retries, 1)
        self.retry_base_delay_ms = self._coerce_positive_int(retry_base_delay_ms, 250)
        self.resume_downloads = bool(resume_downloads)
        self.io_chunk_size = self._coerce_positive_int(io_chunk_size, 512)
        self.md5_chunk_size = self._coerce_positive_int(md5_chunk_size, 512)
        self.strict_http_fs = bool(strict_http_fs)
        self.delete_files_not_in_manifest_policy = self._coerce_delete_policy(
            delete_files_not_in_manifest_policy
        )
        self.delete_files_not_in_manifest_extensions = self._normalize_extensions(
            delete_files_not_in_manifest_extensions
        )
        if (
            self.delete_files_not_in_manifest_policy in (
                OTADeletePolicy.MANIFEST_EXTENSIONS,
                OTADeletePolicy.CUSTOM_EXTENSIONS,
            )
            and not self.delete_files_not_in_manifest_extensions
        ):
            print(
                "Warning: delete_files_not_in_manifest_policy={} requires explicit "
                "delete_files_not_in_manifest_extensions; disabling delete policy.".format(
                    self.delete_files_not_in_manifest_policy
                )
            )
            self.delete_files_not_in_manifest_policy = OTADeletePolicy.NEVER
        self.progress_callback = progress_callback
        self.last_error_code = None
        self.last_error_message = None
        if recurse_http_fs:
            self.manifest_url = None

    # --- Utility Functions as static methods ---
    @staticmethod
    def exists(path: str) -> bool:
        """Return True if a file or directory exists, False otherwise."""
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    @staticmethod
    def read_text_file(path: str) -> str | None:
        """Read a text file and return its contents, or None if not found."""
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def write_text_file(path: str, content: str) -> None:
        """Write text content to a file."""
        with open(path, "w") as f:
            f.write(content)

    @staticmethod
    def read_json_file(path: str):
        """Read a JSON file and return its contents, or None if not found."""
        try:
            with open(path, "r") as f:
                return json.load(f)
        except OSError:
            return None

    @staticmethod
    def write_json_file(path: str, data) -> None:
        """Write a JSON-serializable object to a file."""
        with open(path, "w") as f:
            json.dump(data, f)

    @staticmethod
    def ensure_dir(path: str) -> None:
        """Recursively create directories like os.makedirs."""
        parts = path.split("/")
        curr = ""
        for part in parts:
            if not part:
                continue
            curr = curr + "/" + part if curr else part
            try:
                os.mkdir(curr)
            except OSError:
                # Ignore only if directory already exists.
                if not OTAUpdater.exists(curr):
                    raise

    @staticmethod
    def remove_dir_recursive(path: str) -> None:
        """Recursively delete a directory and all its contents."""
        if not OTAUpdater.exists(path):
            return
        for entry in os.ilistdir(path):
            name = entry[0]
            full_path = path + "/" + name
            if entry[1] == 0x4000:
                OTAUpdater.remove_dir_recursive(full_path)
            else:
                try:
                    os.remove(full_path)
                except OSError:
                    if OTAUpdater.exists(full_path):
                        raise
        try:
            os.rmdir(path)
        except OSError:
            if OTAUpdater.exists(path):
                raise

    @staticmethod
    def _apply_socket_timeout(timeout_s):
        if timeout_s is None:
            return None
        try:
            import usocket as socket
        except ImportError:
            try:
                import socket
            except ImportError:
                return None
        try:
            prev = socket.getdefaulttimeout()
        except Exception:
            prev = None
        try:
            socket.setdefaulttimeout(timeout_s)
            return (socket, prev)
        except Exception:
            return None

    @staticmethod
    def _restore_socket_timeout(token):
        if not token:
            return
        socket, prev = token
        try:
            socket.setdefaulttimeout(prev)
        except Exception:
            pass

    @staticmethod
    def _requests_get(url: str, timeout_s: int | None = None, headers=None):
        if timeout_s is None:
            if headers:
                return urequests.get(url, headers=headers)
            return urequests.get(url)
        try:
            if headers:
                return urequests.get(url, timeout=timeout_s, headers=headers)
            return urequests.get(url, timeout=timeout_s)
        except TypeError:
            if headers:
                return urequests.get(url, headers=headers)
            return urequests.get(url)

    @staticmethod
    def _is_stream_eof_error(exc: Exception) -> bool:
        """
        Some MicroPython HTTP/socket stacks raise OSError on final read after peer close
        instead of returning b"". Treat those as EOF and rely on MD5 verification to catch
        truncation.
        """
        try:
            errno = exc.args[0] if exc.args else None
        except Exception:
            errno = None
        if errno in (128, 104, 107):
            return True
        msg = str(exc).upper()
        return ("ENOTCONN" in msg) or ("ECONNRESET" in msg) or ("ENOTSOCK" in msg)

    @staticmethod
    def _sha256(data: bytes) -> bytes:
        """Return SHA256 digest bytes for data."""
        sha = uhashlib.sha256()
        sha.update(data)
        return sha.digest()

    @staticmethod
    def _hmac_sha256_hex(key: bytes, message: bytes) -> str:
        """Compute RFC2104 HMAC-SHA256 and return hex digest."""
        block_size = 64
        if len(key) > block_size:
            key = OTAUpdater._sha256(key)
        if len(key) < block_size:
            key = key + (b"\x00" * (block_size - len(key)))
        o_key_pad = bytes((b ^ 0x5C) for b in key)
        i_key_pad = bytes((b ^ 0x36) for b in key)
        inner = OTAUpdater._sha256(i_key_pad + message)
        mac = OTAUpdater._sha256(o_key_pad + inner)
        return ubinascii.hexlify(mac).decode("utf-8")

    @staticmethod
    def _manifest_signature_payload(version: int, files) -> bytes:
        """
        Build deterministic signature payload from version + files metadata.
        Each file line is `path<TAB>version<TAB>md5`, sorted by path.
        """
        parts = ["v={}".format(int(version))]
        ordered = sorted(files, key=lambda entry: entry.get("path", ""))
        for entry in ordered:
            parts.append(
                "{}\t{}\t{}".format(
                    entry.get("path", ""),
                    int(entry.get("version", 0)),
                    entry.get("md5", ""),
                )
            )
        return "\n".join(parts).encode("utf-8")

    @staticmethod
    def _sleep_ms(delay_ms: int) -> None:
        if delay_ms <= 0:
            return
        if hasattr(_time, "sleep_ms"):
            _time.sleep_ms(delay_ms)
        else:
            _time.sleep(delay_ms / 1000.0)

    def _emit_progress(self, event_name: str, details=None) -> None:
        cb = self.progress_callback
        if not cb:
            return
        try:
            cb(event_name, details or {})
        except Exception:
            pass

    def _set_error(self, code: str, message: str = "") -> None:
        self.last_error_code = code
        self.last_error_message = message
        if message:
            print("OTA error [{}]: {}".format(code, message))
        else:
            print("OTA error [{}]".format(code))

    @staticmethod
    def _coerce_positive_int(value, default_value: int) -> int:
        try:
            coerced = int(value)
            if coerced > 0:
                return coerced
        except Exception:
            pass
        return default_value

    @staticmethod
    def _coerce_non_negative_int(value, default_value: int) -> int:
        try:
            coerced = int(value)
            if coerced >= 0:
                return coerced
        except Exception:
            pass
        return default_value

    def _coerce_delete_policy(self, policy) -> str:
        policy_s = str(policy or OTADeletePolicy.NEVER).lower()
        if policy_s not in OTADeletePolicy.ALL_POLICIES:
            return OTADeletePolicy.NEVER
        return policy_s

    @staticmethod
    def _normalize_extensions(exts):
        if not exts:
            return ()
        norm = []
        for ext in exts:
            s = str(ext).strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s
            norm.append(s)
        return tuple(norm)

    @staticmethod
    def _path_extension(path: str) -> str:
        if "." not in path:
            return ""
        return "." + path.rsplit(".", 1)[1].lower()

    def _is_internal_ota_path(self, rel_path: str) -> bool:
        if self.dest_dir != ".":
            return False
        if rel_path in (self.ota_state_file, self.local_manifest_file):
            return True
        if rel_path.startswith(self.backup_dir + "/"):
            return True
        return False

    def _should_delete_extraneous_file(self, rel_path: str, manifest_extensions) -> bool:
        policy = self.delete_files_not_in_manifest_policy
        if policy == OTADeletePolicy.NEVER:
            return False
        if self._is_internal_ota_path(rel_path):
            return False
        ext = self._path_extension(rel_path)
        if policy == OTADeletePolicy.ALL:
            return True
        if policy == OTADeletePolicy.MANIFEST_EXTENSIONS:
            if not ext:
                return False
            return (
                ext in manifest_extensions
                and ext in self.delete_files_not_in_manifest_extensions
            )
        if policy == OTADeletePolicy.CUSTOM_EXTENSIONS:
            return ext in self.delete_files_not_in_manifest_extensions if ext else False
        return False

    def _delete_files_not_in_manifest(self, remote_files) -> None:
        policy = self.delete_files_not_in_manifest_policy
        if policy == OTADeletePolicy.NEVER:
            return
        active_root = self.dest_dir if self.dest_dir != "." else "."
        if not self.exists(active_root):
            return
        manifest_paths = set()
        manifest_extensions = set()
        for file_obj in remote_files:
            path = file_obj.get("path")
            if not path:
                continue
            manifest_paths.add(path)
            ext = self._path_extension(path)
            if ext:
                manifest_extensions.add(ext)

        active_paths = self.collect_file_paths(active_root)
        for rel_path in active_paths:
            if rel_path in manifest_paths:
                continue
            if not self._should_delete_extraneous_file(rel_path, manifest_extensions):
                continue
            active_file = self._active_path(rel_path)
            if not self.exists(active_file):
                continue
            backup_file = self.backup_dir + "/" + rel_path
            self.ensure_dir("/".join(backup_file.split("/")[:-1]))
            self.copy_file(active_file, backup_file, chunk_size=self.io_chunk_size)
            os.remove(active_file)
            self._emit_progress("file_delete_extra", {"path": rel_path, "policy": policy})
            print(
                "Deleted file not present in manifest (policy={}): {}".format(
                    policy, active_file
                )
            )

    def _load_local_manifest_versions(self):
        """Load local manifest and return (manifest_dict, local_versions_map)."""
        local_manifest = self.read_json_file(self.local_manifest_file)
        if local_manifest is None:
            local_manifest = {"version": 0, "files": []}
        local_versions = {
            entry.get("path"): int(entry.get("version", 0))
            for entry in local_manifest.get("files", [])
            if entry.get("path")
        }
        return local_manifest, local_versions

    def _download_with_retries(self, url: str, dest_path: str, expected_md5: str) -> None:
        attempts = max(0, self.download_retries) + 1
        last_error = Exception("unknown download failure")
        for attempt in range(attempts):
            try:
                self._emit_progress(
                    "download_attempt",
                    {"url": url, "path": dest_path, "attempt": attempt + 1, "attempts": attempts},
                )
                self.download_file(
                    url,
                    dest_path,
                    chunk_size=self.io_chunk_size,
                    timeout_s=self.http_timeout_s,
                    resume=self.resume_downloads,
                )
                if expected_md5 and not self.verify_file_md5(
                    dest_path, expected_md5, chunk_size=self.md5_chunk_size
                ):
                    self._set_error(OTAErrorCode.MD5, dest_path)
                    raise Exception("MD5 verification failed for {}".format(dest_path))
                return
            except Exception as e:
                last_error = e
                if attempt >= attempts - 1:
                    break
                delay = self.retry_base_delay_ms * (2 ** attempt)
                try:
                    jitter = _time.ticks_ms() & 0x7F
                except Exception:
                    jitter = 0
                wait_ms = delay + jitter
                print(
                    "Download attempt {} failed for {}: {}. Retrying in {} ms.".format(
                        attempt + 1, url, e, wait_ms
                    )
                )
                self._emit_progress(
                    "download_retry",
                    {"url": url, "path": dest_path, "attempt": attempt + 1, "wait_ms": wait_ms},
                )
                self._sleep_ms(wait_ms)
        if self.last_error_code != OTAErrorCode.MD5:
            self._set_error(OTAErrorCode.DOWNLOAD, "{} ({})".format(url, last_error))
        raise last_error

    def _warn_if_low_free_space(self):
        """
        Warn when free space is below 40% of the filesystem.
        This is a simple heuristic to highlight potential update-space exhaustion.
        """
        try:
            root = self.dest_dir if self.dest_dir != "." else "."
            st = os.statvfs(root)
            total_blocks = st[2]
            free_blocks = st[4] if len(st) > 4 else st[3]
            if total_blocks <= 0:
                return
            free_ratio = free_blocks / total_blocks
            if free_ratio < 0.40:
                used_pct = int((1.0 - free_ratio) * 100)
                free_pct = int(free_ratio * 100)
                print(
                    "Warning: low free space before OTA backup (used={}%, free={}%).".format(
                        used_pct, free_pct
                    )
                )
        except Exception as e:
            print("Warning: unable to compute filesystem free-space ratio:", e)

    def _prepare_backup_dir(self):
        self._warn_if_low_free_space()
        if self.exists(self.backup_dir):
            self.remove_dir_recursive(self.backup_dir)
        self.ensure_dir(self.backup_dir)

    def _backup_file_if_needed(self, path, remote_file_ver=None, local_file_ver=0):
        if not path:
            return
        path_l = path.lower()
        if any(path_l.endswith(ext) for ext in self.backup_skip_extensions):
            print("Skipping backup for binary asset: {}".format(path))
            return
        if path.startswith(self.backup_skip_prefixes):
            print("Skipping backup for asset directory prefix: {}".format(path))
            return

        needs_backup = remote_file_ver is None or int(remote_file_ver) > int(local_file_ver)
        active_file = self._active_path(path)
        if needs_backup and self.exists(active_file):
            backup_file = self.backup_dir + "/" + path
            self.ensure_dir("/".join(backup_file.split("/")[:-1]))
            self.copy_file(active_file, backup_file, chunk_size=self.io_chunk_size)
            print("Backed up {} to {}".format(active_file, backup_file))

    @staticmethod
    def copy_file(src: str, dst: str, chunk_size: int = 512) -> None:
        """Copy a file in small chunks."""
        dest_dir = "/".join(dst.split("/")[:-1])
        if dest_dir:
            OTAUpdater.ensure_dir(dest_dir)
        with open(src, "rb") as fsrc:
            with open(dst, "wb") as fdst:
                while True:
                    chunk = fsrc.read(chunk_size)
                    if not chunk:
                        break
                    fdst.write(chunk)

    def _active_path(self, rel_path: str) -> str:
        """Resolve a manifest-relative file path to its active destination path."""
        rel = rel_path.lstrip("/")
        if self.dest_dir == ".":
            return rel
        return self.dest_dir.rstrip("/") + "/" + rel

    @staticmethod
    def download_file(
        url: str,
        dest_path: str,
        chunk_size: int = 512,
        timeout_s: int | None = None,
        resume: bool = False,
    ) -> None:
        """Download a file via HTTP in chunks and write it to dest_path."""
        print("Downloading:", url, "->", dest_path)
        gc.collect()
        part_path = dest_path + ".part" if resume else dest_path
        existing_size = 0
        headers = None
        if resume and OTAUpdater.exists(part_path):
            try:
                existing_size = int(os.stat(part_path)[6])
            except Exception:
                existing_size = 0
            if existing_size > 0:
                headers = {"Range": "bytes={}-".format(existing_size)}
        r = OTAUpdater._requests_get(url, timeout_s, headers=headers)
        try:
            if r.status_code not in (200, 206):
                raise Exception(f"HTTP error {r.status_code} while downloading {url}")

            dest_dir = "/".join(dest_path.split("/")[:-1])
            if dest_dir:
                OTAUpdater.ensure_dir(dest_dir)

            mode = "wb"
            if resume and existing_size > 0 and r.status_code == 206:
                mode = "ab"
            with open(part_path, mode) as f:
                # Prefer true streaming reads for constrained devices.
                if hasattr(r, "raw"):
                    while True:
                        try:
                            chunk = r.raw.read(chunk_size)
                        except OSError as e:
                            if OTAUpdater._is_stream_eof_error(e):
                                break
                            raise
                        if not chunk:
                            break
                        f.write(chunk)
                elif hasattr(r, "iter_content"):
                    try:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                    except OSError as e:
                        if not OTAUpdater._is_stream_eof_error(e):
                            raise
                elif hasattr(r, "read"):
                    while True:
                        try:
                            chunk = r.read(chunk_size)
                        except OSError as e:
                            if OTAUpdater._is_stream_eof_error(e):
                                break
                            raise
                        if not chunk:
                            break
                        f.write(chunk)
                elif hasattr(r, "text"):
                    f.write(r.text.encode("utf-8"))
                elif hasattr(r, "content"):
                    f.write(r.content)
                else:
                    raise Exception("Unable to read response content")

            if resume:
                if OTAUpdater.exists(dest_path):
                    os.remove(dest_path)
                os.rename(part_path, dest_path)
        finally:
            try:
                r.close()
            except Exception:
                pass
        gc.collect()

    @staticmethod
    def verify_file_md5(
        filepath: str, expected_md5: str, chunk_size: int = 512
    ) -> bool:
        """Calculate the MD5 hash of a file in small chunks and compare with expected_md5."""
        md5 = uhashlib.md5()
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    md5.update(chunk)
        except OSError as e:
            print("Error reading file for MD5 verification:", e)
            return False
        file_md5 = ubinascii.hexlify(md5.digest()).decode("utf-8")
        print(
            "Verifying",
            filepath,
            "expected MD5:",
            expected_md5,
            "calculated MD5:",
            file_md5,
        )
        return file_md5 == expected_md5.lower()

    def _get_remote_via_manifest(self):
        """Main OTA update logic using a manifest file."""
        self.last_error_code = None
        self.last_error_message = None
        self._emit_progress("update_start", {"mode": "manifest"})
        manifest = self.fetch_manifest()
        if manifest is None:
            print("Failed to fetch manifest. Aborting OTA update.")
            return

        remote_version = int(manifest.get("version", 0))
        remote_files = manifest.get("files", [])

        local_manifest, local_versions = self._load_local_manifest_versions()
        local_version = int(local_manifest.get("version", 0))

        if not self.force_update and remote_version <= local_version:
            print(
                "No update needed. Local version: {}, Remote version: {}".format(
                    local_version, remote_version
                )
            )
            return

        print(
            "New OTA update available. Local version: {}, Remote version: {}".format(
                local_version, remote_version
            )
        )
        # Mark update in progress
        self.write_text_file(self.ota_state_file, OTAState.INSTALLING)

        try:
            self._prepare_backup_dir()
            self._download_and_verify_files(remote_files, local_versions)
            try:
                self._delete_files_not_in_manifest(remote_files)
            except Exception as delete_err:
                self._set_error(OTAErrorCode.DELETE_EXTRAS, str(delete_err))
                raise
            self.write_json_file(self.local_manifest_file, manifest)
            self.write_text_file(self.ota_state_file, OTAState.CONFIRM_PENDING)
            self._emit_progress("update_applied", {"mode": "manifest"})
            print("OTA update completed successfully. Rebooting...")
            machine.reset()
        except Exception as e:
            if not self.last_error_code:
                self._set_error(OTAErrorCode.APPLY, str(e))
            print("OTA update failed:", e)
            self.revert_update()

    def _download_and_verify_files(self, remote_files, local_versions):
        """Backup, download, and verify changed files (or all files when force_update=True)."""
        total = len(remote_files)
        for idx, file_obj in enumerate(remote_files, 1):
            path = file_obj.get("path")
            if not path:
                print("Skipping invalid manifest entry with empty path")
                continue
            file_ver = int(file_obj.get("version", 0))
            expected_md5 = file_obj.get("md5", "").lower()
            local_file_ver = int(local_versions.get(path, 0))

            should_update = self.force_update or (file_ver > local_file_ver)
            if should_update:
                self._emit_progress(
                    "file_update_start",
                    {"path": path, "index": idx, "total": total, "from": local_file_ver, "to": file_ver},
                )
                if self.force_update and file_ver <= local_file_ver:
                    print(
                        "Force updating {}: local version {} -> remote version {}".format(
                            path, local_file_ver, file_ver
                        )
                    )
                    backup_remote_ver = None
                else:
                    print(
                        "Updating {}: local version {} -> remote version {}".format(
                            path, local_file_ver, file_ver
                        )
                    )
                    backup_remote_ver = file_ver
                self._backup_file_if_needed(path, backup_remote_ver, local_file_ver)
                remote_file_url = "{}/{}".format(self.base_file_url.rstrip("/"), path)
                active_path = self._active_path(path)
                self._download_with_retries(remote_file_url, active_path, expected_md5)
                if not expected_md5:
                    print("Warning: No MD5 provided for {}. Skipping verification.".format(path))
                self._emit_progress(
                    "file_update_done", {"path": path, "index": idx, "total": total}
                )
            else:
                print("File {} is up-to-date (version {})".format(path, local_file_ver))
                self._emit_progress(
                    "file_update_skip", {"path": path, "index": idx, "total": total}
                )

    def _get_remote_via_http_fs(self):
        """
        Recursively download all files from base_file_url.
        Assumes Nginx-style directory listings (HTML with <a href="...">).
        Ignores parent dirs, query strings, and malformed links.
        """
        import re

        def list_directory(url):
            try:
                print("Listing:", url)
                r = self._requests_get(url, self.http_timeout_s)
                if r.status_code != 200:
                    print("Failed to list", url, "-", r.status_code)
                    r.close()
                    return []
                html = r.text
                r.close()
                return re.findall(r'href="([^"]+)"', html)
            except Exception as e:
                print("Failed to list", url, "-", e)
                return []

        def crawl_and_download(base_url, base_path=""):
            entries = list_directory(base_url)
            for entry in entries:
                # Skip parent dirs and malformed links
                if entry.startswith("../") or "?" in entry or "#" in entry:
                    continue

                full_remote_url = "{}/{}".format(base_url.rstrip("/"), entry)
                rel_path = base_path + entry
                active_path = self._active_path(rel_path)

                if entry.endswith("/"):
                    # Directory: recurse into it
                    crawl_and_download(full_remote_url, base_path + entry)
                elif "/" not in entry:
                    # File: download
                    print("Downloading:", full_remote_url, "->", active_path)
                    try:
                        self._emit_progress(
                            "file_update_start",
                            {"path": rel_path, "mode": "http_fs"},
                        )
                        self._backup_file_if_needed(rel_path, None, 0)
                        self._download_with_retries(full_remote_url, active_path, "")
                        self._emit_progress(
                            "file_update_done",
                            {"path": rel_path, "mode": "http_fs"},
                        )
                    except Exception as e:
                        print("Failed to download {}: {}".format(entry, e))
                        self._emit_progress(
                            "file_update_failed",
                            {"path": rel_path, "mode": "http_fs", "error": str(e)},
                        )
                        if self.strict_http_fs:
                            raise

        print("Starting recursive HTTP FS update from:", self.base_file_url)
        self.write_text_file(self.ota_state_file, OTAState.INSTALLING)
        self.last_error_code = None
        self.last_error_message = None
        self._emit_progress("update_start", {"mode": "http_fs"})
        try:
            self._prepare_backup_dir()
            crawl_and_download(self.base_file_url)
            print("Download complete.")

            self.write_text_file(self.ota_state_file, OTAState.CONFIRM_PENDING)
            self._emit_progress("update_applied", {"mode": "http_fs"})
            print("OTA update via HTTP FS completed. Rebooting...")
            machine.reset()

        except Exception as e:
            self._set_error(OTAErrorCode.HTTP_FS, str(e))
            print("Recursive HTTP FS OTA update failed:", e)
            self.revert_update()

    def collect_file_paths(self, root_dir):
        """
        Recursively collect relative file paths from a directory.
        """
        import os

        paths = []

        def walk(path, rel_base=""):
            try:
                for fname in os.listdir(path):
                    full_path = path + "/" + fname
                    rel_path = rel_base + fname
                    if os.stat(full_path)[0] & 0x4000:  # is dir
                        walk(full_path, rel_path + "/")
                    else:
                        paths.append(rel_path)
            except Exception as e:
                print("Failed to list", path, "-", e)

        walk(root_dir)
        return paths

    def check_and_perform_ota(self):
        """
        Entry point for OTA update.
          1. Checks OTA_STATE_FILE. If an update was interrupted (state == "installing"),
             reverts to backup.
          2. Fetches the remote manifest and compares its version to the local version.
          3. For each file in the manifest that has a higher version than installed, backs up
             the current file then downloads the new file directly to destination and verifies MD5.
          4. Applies optional delete-extras policy for manifest mode.
          6. Writes the new manifest as the local version manifest, clears OTA state, and reboots.
        """
        state = self.read_text_file(self.ota_state_file)
        if state == OTAState.INSTALLING:
            print("Incomplete OTA detected; reverting to backup.")
            self.revert_update()
            return
        if state == OTAState.CONFIRM_PENDING:
            print("Pending OTA confirmation detected. Confirm or revert before next update.")
            return

        timeout_token = self._apply_socket_timeout(self.http_timeout_s)
        try:
            if self.manifest_url:
                self._get_remote_via_manifest()
            else:
                self._get_remote_via_http_fs()
        finally:
            self._restore_socket_timeout(timeout_token)

    def fetch_manifest(self):
        """Fetch the JSON manifest from the OTA server."""
        print("Fetching manifest from:", self.manifest_url)
        try:
            r = self._requests_get(self.manifest_url, self.http_timeout_s)
            if r.status_code != 200:
                print("Manifest HTTP error:", r.status_code)
                r.close()
                return None
            manifest = r.json()
            r.close()
            if self.manifest_auth_key:
                sig = manifest.get(self.manifest_signature_field)
                if not isinstance(sig, str):
                    self._set_error(OTAErrorCode.MANIFEST_SIGNATURE, "missing signature field")
                    print("Manifest signature missing or invalid type.")
                    return None
                payload = self._manifest_signature_payload(
                    manifest.get("version", 0), manifest.get("files", [])
                )
                expected_sig = self._hmac_sha256_hex(
                    self.manifest_auth_key.encode("utf-8"), payload
                )
                if sig.lower() != expected_sig:
                    self._set_error(OTAErrorCode.MANIFEST_SIGNATURE, "signature mismatch")
                    print("Manifest signature verification failed.")
                    return None
            return manifest
        except Exception as e:
            self._set_error(OTAErrorCode.MANIFEST_FETCH, str(e))
            print("Error fetching manifest:", e)
            return None

    def revert_update(self):
        """
        In case of a failed update, restore files from BACKUP_DIR to active destination paths,
        clear the OTA state and reboot.
        """
        print("Reverting update from backup...")
        if self.exists(self.backup_dir):
            for rel_path in self.collect_file_paths(self.backup_dir):
                src_path = self.backup_dir + "/" + rel_path
                dst_path = self._active_path(rel_path)
                self.ensure_dir("/".join(dst_path.split("/")[:-1]))
                self.copy_file(src_path, dst_path, chunk_size=self.io_chunk_size)
            self.remove_dir_recursive(self.backup_dir)
            print("Reversion complete.")
        self.write_text_file(self.ota_state_file, OTAState.IDLE)
        machine.reset()

    def cleanup_files(self):
        """
        Remove the backup directory if it exists.
        Call this after your main application has verified the update is working.
        Returns bool indicating if cleanup was done.
        """
        state = self.read_text_file(self.ota_state_file)
        if state == OTAState.CONFIRM_PENDING:
            print("Cleanup blocked: OTA confirmation is still pending.")
            return False
        changes_made = False
        if self.exists(self.backup_dir):
            self.remove_dir_recursive(self.backup_dir)
            print(f"Removed backup directory: {self.backup_dir}")
            changes_made = True
        return changes_made

    def confirm_update(self, cleanup=False):
        """
        Mark a pending OTA update as confirmed by the running application.
        Optionally clean up backup files after confirmation.
        """
        state = self.read_text_file(self.ota_state_file)
        if state != OTAState.CONFIRM_PENDING:
            return False
        self.write_text_file(self.ota_state_file, OTAState.IDLE)
        if cleanup:
            self.cleanup_files()
        return True

    def release(self):
        """
        Best-effort memory teardown for constrained devices.

        - Clears/deletes instance attributes to drop references.
        - Triggers garbage collection.
        """
        # Prefer __dict__ when available for precise instance-attr teardown.
        try:
            keys = tuple(self.__dict__.keys())
        except Exception:
            keys = None

        if keys is not None:
            for key in keys:
                try:
                    delattr(self, key)
                except Exception:
                    try:
                        setattr(self, key, None)
                    except Exception:
                        pass
        else:
            # Fallback for runtimes without normal instance dict behavior.
            for attr in dir(self):
                if attr.startswith("__"):
                    continue
                try:
                    value = getattr(self, attr)
                except Exception:
                    continue
                if callable(value):
                    continue
                try:
                    delattr(self, attr)
                except Exception:
                    try:
                        setattr(self, attr, None)
                    except Exception:
                        pass

        gc.collect()
        return None
