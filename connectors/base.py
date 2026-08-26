"""Abstract SAP connector for the Basis Copilot.

Deliberately minimal: a Basis connector reads tables and calls read-only function
modules. It does NOT expose the user / role / authorization helpers the security
tooling needs, because this server ships none of those tools.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class BaseSAPConnector(ABC):
    """Read-only SAP access surface used by the Basis tools."""

    @abstractmethod
    async def _table_read(self, table: str, fields: Optional[List[str]] = None,
                          where: str = "", max_rows: int = 1000) -> List[Dict]:
        """Read an SAP table via RFC_READ_TABLE."""

    @abstractmethod
    async def _rfc_call(self, function: str, parameters: Optional[Dict] = None) -> Dict:
        """Call one read-only function module."""

    @abstractmethod
    async def _rfc_batch(self, calls: List[Dict]) -> Dict:
        """Run a stateful sequence of function modules in one context."""

    @abstractmethod
    async def get_system_info(self) -> Dict:
        """RFC_SYSTEM_INFO: instance and system identity."""

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying HTTP client."""
