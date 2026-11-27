"""Optional provider-backed review sources.

The core never imports this package. The CLI imports only the adapter selected
by its flag, so manual ``review``, ``--from-json`` and ``check`` stay free of
provider merge policy. Provider CLIs are external processes; Atesto never
reads or stores their tokens.
"""

from .base import ProviderError

__all__ = ["ProviderError"]
