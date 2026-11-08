"""Device drivers.

Each module exposes one class, named after the manifest in ``device_types/``.
Importing a driver never touches the hardware: the pin objects are created in
``setup()`` so a missing board only marks *that device* unavailable.
"""

from .base import Driver

__all__ = ["Driver"]
