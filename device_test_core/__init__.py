from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("device_test_core")
except PackageNotFoundError:
    pass
