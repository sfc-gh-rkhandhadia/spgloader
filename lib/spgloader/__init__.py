# spgloader — Python library package
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("spgloader")
except PackageNotFoundError:
    __version__ = "0.0.0"
