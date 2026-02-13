"""
saguarota - MicroPython OTA updater library

Usage:
    from saguarota import OTAUpdater  # On MicroPython
    from saguarota.py3utils import OTAManifestBuilder, OTAManifestServer  # On host Python
"""

import sys

__all__ = []

if sys.implementation.name == "micropython":
    from .saguarota import OTADeletePolicy, OTAErrorCode, OTAState, OTAUpdater

    __all__ += ["OTAUpdater", "OTAState", "OTAErrorCode", "OTADeletePolicy"]
else:
    # Host-side utilities (CPython)
    from .py3utils import OTAManifestBuilder, OTAManifestServer

    __all__ += ["OTAManifestBuilder", "OTAManifestServer"]
