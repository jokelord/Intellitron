"""Pure-vision computer-use geometry and protocol primitives.

Native capture/input resources are intentionally kept behind separate clients;
this package's public records never grant desktop authority by themselves.
"""

from .config import ComputerUseConfig
from .contracts import (
    Action,
    CanonicalFrame,
    DetailCrop,
    DispatchReceipt,
    GroundingRequest,
    InputBinding,
    ProtocolDescriptor,
)
from .errors import ComputerUseError
from .executable_backend import VisionDesktopGroundingBackend

__all__ = [
    "Action", "CanonicalFrame", "ComputerUseConfig", "ComputerUseError",
    "DetailCrop", "DispatchReceipt", "GroundingRequest", "InputBinding",
    "ProtocolDescriptor",
    "VisionDesktopGroundingBackend",
]
