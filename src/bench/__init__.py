from importlib.metadata import PackageNotFoundError, version

try:
    # from the installed package, so `cz bump` (which updates pyproject.toml) is enough
    __version__ = version("bench")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"
