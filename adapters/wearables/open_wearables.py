"""
adapters/wearables/open_wearables.py
======================================
OpenWearables adapter — placeholder for community-contributed integrations.

This module is a contribution stub. It is registered in the adapter registry
so that config files referencing `adapter: "open_wearables"` produce a
clear, actionable error message rather than a cryptic ImportError.

To implement a new wearable adapter:
  1. Copy adapters/wearables/oura.py as a starting point
  2. Implement authenticate(), fetch_context(), is_available()
  3. Register in adapters/registry.py
  4. Add tests in tests/adapters/

See CONTRIBUTING.md (if present) or the adapter template in the repo wiki.

If you are integrating a specific device, consider opening a pull request
or filing a GitHub issue so the community can help.
"""

from __future__ import annotations

from datetime import datetime

from adapters.base import ContextReading, WearableAdapter


class OpenWearablesAdapter(WearableAdapter):
    """
    Placeholder adapter — not yet implemented.

    Raises NotImplementedError with contribution guidance on any call.
    Replace this class with a real implementation for your device.
    """

    ADAPTER_NAME = "open_wearables"

    _HELP = (
        "The 'open_wearables' adapter is a community contribution stub.\n"
        "It is not yet implemented.\n\n"
        "To add support for your device:\n"
        "  1. Copy adapters/wearables/oura.py as a template\n"
        "  2. Implement authenticate(), fetch_context(), is_available()\n"
        "  3. Register the new class in adapters/registry.py\n"
        "  4. Add tests in tests/adapters/\n\n"
        "Alternatively, use the CSV import adapter to load data manually:\n"
        "  wearables:\n"
        "    csv:\n"
        "      enabled: true\n"
        "      csv_path: data/wearable_history.csv"
    )

    async def authenticate(self) -> None:
        raise NotImplementedError(self._HELP)

    async def fetch_context(self, date: datetime) -> ContextReading:
        raise NotImplementedError(self._HELP)

    def is_available(self) -> bool:
        return False
