from abc import ABC, abstractmethod
from typing import Tuple

from core.models import AlertRecord


class BaseNotifier(ABC):
    @abstractmethod
    def send(self, record: AlertRecord) -> bool:
        """Send the alert. Returns True on success, False on failure."""
        ...

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str]:
        """Test connectivity. Returns (success, message)."""
        ...
