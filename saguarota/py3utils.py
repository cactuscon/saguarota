"""
py3utils.py - Host-side utilities for the saguarota OTA update library

This module provides robust, modern Python 3 utilities for managing OTA (Over-The-Air) updates
for MicroPython devices using the saguarota library. It is not intended for use on MicroPython itself.

Features:
    - OTAManifestBuilder: Scans a source directory and generates a manifest (versions.json) with file hashes and versions.
    - OTAManifestServer:  Runs a simple HTTP server to serve OTA manifests and files for development/testing.

Intended Use:
    - Use OTAManifestBuilder to create a manifest describing all updatable files for your device.
    - Use OTAManifestServer to serve both the manifest and files to your MicroPython device during development.
    - These tools are for host-side (Python 3.8+) use only, not for MicroPython.

Example usage:
    from saguarota.py3utils import OTAManifestBuilder, OTAManifestServer

    # Build a manifest from a source directory
    builder = OTAManifestBuilder('src')
    manifest_json = builder.generate_manifest()
    with open('versions.json', 'w') as f:
        f.write(manifest_json)

    # Start a development OTA server
    server = OTAManifestServer('src', host='localhost', port=8000)
    server.start()  # Blocks; use server.start(background=True) for threaded mode

See the main saguarota README for full documentation and device-side usage.
"""

import os
import json
import hashlib
import hmac
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote
import threading
from typing import Optional


class OTAManifestBuilder:
    """
    OTAManifestBuilder scans a source directory and generates a manifest (versions.json)
    describing all files eligible for OTA update. The manifest includes file paths, versions,
    and MD5 hashes for integrity checking.

    Typical usage:
        builder = OTAManifestBuilder('src')
        manifest_json = builder.generate_manifest()

    Args:
        src_dir (str): Path to the source directory to scan for OTA files.
    """

    def __init__(
        self,
        src_dir: str,
        auth_key: Optional[str] = None,
        signature_field: str = "signature",
        allowed_extensions=None,
        exclude_prefixes=None,
        exclude_folders=None,
        followlinks: bool = True,
        json_indent: int = 2,
        version_source: str = "mtime",
        previous_manifest_path: Optional[str] = None,
        reuse_unchanged_versions: bool = False,
        git_executable: str = "git",
    ):
        """
        Initialize the builder with a source directory.
        Args:
            src_dir: Path to the directory containing files to include in the manifest.
            auth_key: Optional shared secret for HMAC-SHA256 manifest signatures.
            signature_field: Field name to store signature hex in output manifest.
            allowed_extensions: Iterable of file extensions to include.
            exclude_prefixes: Iterable of filename prefixes to skip.
            exclude_folders: Iterable of folder names to skip recursively.
            followlinks: Whether os.walk should follow symlinks.
            json_indent: JSON indent used for output manifest.
            version_source: "mtime" (default) or "git_commit_time".
            previous_manifest_path: Optional path to a prior versions.json.
            reuse_unchanged_versions: If True, keep prior per-file version when MD5 is unchanged.
            git_executable: Git binary name/path for git-based versioning.
        """
        self.src_dir = Path(src_dir)
        self.auth_key = auth_key
        self.signature_field = signature_field
        self.allowed_extensions = {
            ext.lower() for ext in (allowed_extensions or (".py", ".mpy", ".raw", ".rgb565", ".c"))
        }
        self.exclude_prefixes = tuple(exclude_prefixes or ("test_",))
        self.exclude_folders = set(exclude_folders or ("__pycache__", "examples", "docs", "tests"))
        self.followlinks = bool(followlinks)
        self.json_indent = json_indent
        self.version_source = version_source
        self.previous_manifest_path = (
            Path(previous_manifest_path) if previous_manifest_path else None
        )
        self.reuse_unchanged_versions = bool(reuse_unchanged_versions)
        self.git_executable = git_executable

    @staticmethod
    def _manifest_signature_payload(version: int, files) -> bytes:
        """Build deterministic payload used for manifest signatures."""
        parts = [f"v={int(version)}"]
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

    def calculate_md5(self, file_path: Path) -> str:
        """
        Calculate the MD5 hash of a file for integrity checking.
        Args:
            file_path: Path to the file.
        Returns:
            str: MD5 hash as a hex string.
        """
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def get_file_version(self, file_path: Path) -> int:
        """
        Get a version number for a file based on configured source.
        Args:
            file_path: Path to the file.
        Returns:
            int: Version number.
        """
        if self.version_source == "git_commit_time":
            return self._get_git_commit_timestamp(file_path)
        return int(file_path.stat().st_mtime)

    def _get_git_commit_timestamp(self, file_path: Path) -> int:
        try:
            out = subprocess.check_output(
                [self.git_executable, "log", "-1", "--format=%ct", str(file_path)],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore").strip()
            return int(out)
        except Exception:
            return int(file_path.stat().st_mtime)

    @staticmethod
    def load_manifest(path: Path) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"version": 0, "files": []}

    def generate_manifest_data(self) -> dict:
        """
        Generate manifest as a Python dict for CI/pipelines.
        """
        previous_files = {}
        if self.previous_manifest_path:
            prev = self.load_manifest(self.previous_manifest_path)
            previous_files = {
                entry.get("path"): entry for entry in prev.get("files", []) if entry.get("path")
            }

        files = []
        for root, dirs, filenames in os.walk(self.src_dir, followlinks=self.followlinks):
            dirs[:] = [d for d in dirs if d not in self.exclude_folders]
            for filename in filenames:
                ext = Path(filename).suffix.lower()
                if ext not in self.allowed_extensions:
                    continue
                if any(filename.startswith(prefix) for prefix in self.exclude_prefixes):
                    continue
                file_path = Path(root) / filename
                rel_path = file_path.relative_to(self.src_dir)
                rel_path_str = str(rel_path).replace("\\", "/")
                md5_hash = self.calculate_md5(file_path)
                prev_entry = previous_files.get(rel_path_str)
                if (
                    self.reuse_unchanged_versions
                    and prev_entry
                    and prev_entry.get("md5") == md5_hash
                ):
                    version = int(prev_entry.get("version", 0))
                else:
                    version = self.get_file_version(file_path)
                files.append(
                    {"path": rel_path_str, "version": version, "md5": md5_hash}
                )
        files.sort(key=lambda entry: entry.get("path", ""))
        global_version = max((entry["version"] for entry in files), default=0)
        manifest = {"version": global_version, "files": files}
        if self.auth_key:
            payload = self._manifest_signature_payload(global_version, files)
            manifest[self.signature_field] = hmac.new(
                self.auth_key.encode("utf-8"), payload, hashlib.sha256
            ).hexdigest()
        return manifest

    def generate_manifest(self) -> str:
        """
        Generate the OTA manifest as a JSON string. Only files with allowed extensions are included.
        Returns:
            str: Manifest JSON string.
        """
        manifest = self.generate_manifest_data()
        return json.dumps(manifest, indent=self.json_indent)

    def write_manifest(self, output_path: str) -> dict:
        """
        Generate and write manifest JSON to disk. Returns manifest dict.
        """
        manifest = self.generate_manifest_data()
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=self.json_indent)
        return manifest


class OTAManifestServer:
    """
    OTAManifestServer is a simple HTTP server for serving OTA manifests and files to MicroPython devices
    during development and testing. Not for production use.

    Usage:
        server = OTAManifestServer('src', host='localhost', port=8000)
        server.start()  # Blocks until Ctrl+C

    Args:
        src_dir (str): Path to the directory to serve files from.
        host (str): Hostname or IP to bind the server (default: 'localhost').
        port (int): TCP port to listen on (default: 8000).
    """

    def __init__(
        self,
        src_dir: str,
        host: str = "localhost",
        port: int = 8000,
        builder: Optional[OTAManifestBuilder] = None,
    ):
        """
        Initialize the OTA manifest server.
        Args:
            src_dir: Directory to serve files and manifests from.
            host: Hostname or IP address to bind.
            port: TCP port to listen on.
            builder: Optional preconfigured OTAManifestBuilder instance.
        """
        self.src_dir = Path(src_dir).resolve()
        self.host = host
        self.port = port
        self.builder = builder or OTAManifestBuilder(self.src_dir)
        self.httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    class _Handler(BaseHTTPRequestHandler):
        """
        Internal HTTP handler for OTA requests. Serves /ota/versions.json and files under /ota/.
        """

        def do_GET(self):
            """Handle GET requests for manifest and OTA files."""
            path = unquote(self.path)
            if path == "/ota/versions.json":
                manifest_json = self.server.builder.generate_manifest()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(
                    "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
                )
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(manifest_json.encode("utf-8"))
            elif path.startswith("/ota/"):
                file_path = path[5:]
                full_path = (self.server.src_dir / file_path).resolve()
                # Security: enforce containment under src_dir and block traversal
                try:
                    full_path.relative_to(self.server.src_dir)
                except ValueError:
                    self.send_response(404)
                    self.end_headers()
                    return
                if not full_path.exists() or full_path.is_dir():
                    self.send_response(404)
                    self.end_headers()
                    return
                with open(full_path, "rb") as f:
                    content = f.read()
                # Set content type based on file extension
                content_type = "application/octet-stream"
                if file_path.endswith(".py"):
                    content_type = "text/plain"
                elif file_path.endswith(".json"):
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(
                    "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
                )
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            # Only log non-routine requests (for debugging)
            if not self.path.startswith("/ota/"):
                return
            print(f"{self.client_address[0]} - {format % args}")

    def start(self, background: bool = False) -> None:
        """
        Start the OTA dev server. If background=True, runs in a background thread.
        Args:
            background (bool): If True, run server in a thread (non-blocking).
        """
        self.httpd = HTTPServer((self.host, self.port), self._Handler)
        self.httpd.src_dir = self.src_dir
        self.httpd.builder = self.builder
        print(f"OTAManifestServer running at http://{self.host}:{self.port}")
        print(f"Manifest URL: http://{self.host}:{self.port}/ota/versions.json")
        if background:
            self._thread = threading.Thread(
                target=self.httpd.serve_forever, daemon=True
            )
            self._thread.start()
        else:
            try:
                self.httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nShutting down server...")
                self.httpd.server_close()

    def stop(self) -> None:
        """
        Stop the OTA dev server if running. Waits for background thread to finish.
        """
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._thread:
            self._thread.join()
