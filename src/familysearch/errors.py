"""Exception hierarchy for the familysearch library."""


class FSError(Exception):
    """Base class for all familysearch errors."""


class TokenExpiredError(FSError):
    """The session token is missing, expired, or rejected."""


class LinkNotFoundError(FSError):
    """A required link/template was not found in the discovery document."""


class FSHTTPError(FSError):
    """An HTTP error response from FamilySearch."""

    def __init__(self, status: int, body: str = "", message: str | None = None):
        self.status = status
        self.body = body
        super().__init__(message or f"HTTP {status}: {body[:200]}")
