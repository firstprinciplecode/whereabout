"""whereabout — Fast situational awareness tool and protocol (whereabout/v1) for AI agents."""

from .engine import snapshot
from .protocol import SCHEMA_VERSION, VERSION

__all__ = ["VERSION", "SCHEMA_VERSION", "snapshot"]
