"""Safe, typed exceptions raised by the Bale client."""

from __future__ import annotations

from typing import Any


class BaleError(Exception):
    """Base class for Bale API failures."""

    def __init__(self, method: str, message: str) -> None:
        self.method = method
        super().__init__(f"{method}: {message}")


class BaleAPIError(BaleError):
    """An error envelope returned by Bale."""

    def __init__(
        self,
        method: str,
        *,
        error_code: int | None,
        description: str | None,
        parameters: dict[str, Any] | None = None,
        retry_after: float | None = None,
        rate_limited: bool = False,
    ) -> None:
        safe_description = description or "API request failed"
        self.error_code = error_code
        self.description = safe_description
        self.parameters = parameters or {}
        self.retry_after = retry_after
        self.rate_limited = rate_limited
        code = f" (code {error_code})" if error_code is not None else ""
        super().__init__(method, f"{safe_description}{code}")


class BaleHTTPError(BaleError):
    """An HTTP failure without a usable Bale error envelope."""

    def __init__(self, method: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(method, f"HTTP status {status_code}")


class BaleNetworkError(BaleError):
    """A network error after the configured retry attempts were exhausted."""

    def __init__(self, method: str, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(method, f"network request failed after {attempts} attempt(s)")


class BaleProtocolError(BaleError):
    """A malformed or unexpected Bale API response."""

    def __init__(self, method: str, message: str) -> None:
        super().__init__(method, message)
