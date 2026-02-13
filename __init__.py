"""Root package shim for nested submodule layout."""

try:
    import sys as _sys
except ImportError:
    import usys as _sys

_pkg_dir = __file__.rsplit("/", 1)[0]
_inner_pkg_dir = _pkg_dir + "/saguarota"

# Ensure imports like `saguarota.py3utils` resolve to nested package files.
try:
    __path__.append(_inner_pkg_dir)
except Exception:
    pass

from .saguarota import *  # noqa: F401,F403
from .saguarota import __all__ as _inner_all

__all__ = list(_inner_all)

# Keep explicit submodule alias compatibility.
try:
    _py3utils = __import__(
        __name__ + ".saguarota.py3utils", None, None, ("OTAManifestBuilder",), 0
    )
    _sys.modules[__name__ + ".py3utils"] = _py3utils
except Exception:
    pass
