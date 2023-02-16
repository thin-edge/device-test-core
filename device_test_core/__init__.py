import sys

if sys.version_info < (3, 8):
    from importlib_metadata import version, PackageNotFoundError
else:
    from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("device_test_core")
except PackageNotFoundError:
    pass
