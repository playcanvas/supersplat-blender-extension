"""Domain errors raised by the SuperSplat extension."""

from __future__ import annotations

from typing import Any

# 429 codes from the API spec that will not clear by retrying within a publish or import.
_NON_RETRYABLE_429_CODES = {
    "daily_publish_limit_reached",
    "too_many_processing",
    "daily_download_limit_reached",
}


class SuperSplatError(Exception):
    """Base class for user-facing extension errors."""


class ConfigurationError(SuperSplatError):
    """The extension or publish request is not configured correctly."""


class CredentialError(SuperSplatError):
    """The API key could not be read from or written to the credential store."""


class ExportError(SuperSplatError):
    """The selected object could not be exported as a Gaussian splat PLY."""


class ProtocolError(SuperSplatError):
    """The service returned an invalid or inconsistent response."""


class UploadCancelled(SuperSplatError):
    """The current export, upload or import was cancelled by the user."""


class ApiError(SuperSplatError):
    """An HTTP API request failed."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after

    @property
    def code(self) -> str:
        """Machine-readable `code` from FileTooLargeError / TooManyRequestsError."""
        if isinstance(self.body, dict) and isinstance(self.body.get("code"), str):
            return self.body["code"]
        return ""

    @property
    def transient(self) -> bool:
        if self.status is None:
            return True
        if self.status == 429:
            return self.code not in _NON_RETRYABLE_429_CODES
        return self.status in {408, 425} or 500 <= self.status <= 599


class DecodeError(SuperSplatError):
    """A SuperSplat scene could not be downloaded or decoded."""


class ResponseTooLarge(DecodeError):
    """A response body exceeded the byte limit it was fetched with."""


class DownloadHttpError(DecodeError):
    """A file download got an HTTP error status (an expired signed URL, say)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status
