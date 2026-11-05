"""Small, dependency-free client for the SuperSplat API.

Endpoints, schemas and error shapes follow the public OpenAPI document at
https://playcanvas.com/api/supersplat/openapi.json.
"""

from __future__ import annotations

import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterable, Iterator, Protocol

from . import USER_AGENT
from .errors import ApiError, ConfigurationError, ProtocolError, UploadCancelled

DEFAULT_BASE_URL = "https://playcanvas.com/api/supersplat"

# Plain http is accepted only for these hosts, so a local mock server can stand in
# for the API and the signed download URLs. Nothing leaves the machine over it.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def is_secure_url(url: str) -> bool:
    """True for an https URL with a host, or an http URL on a loopback host.

    URLs carrying user info are refused: credentials never travel in a URL.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""
    # Header names are lower-cased.
    headers: dict[str, str] = field(default_factory=dict)


class TransportError(OSError):
    """The request never produced an HTTP response (DNS, TLS, timeout, ...)."""


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | Iterable[bytes] | None,
        timeout: float,
    ) -> HttpResponse:
        """Send one request without following redirects.

        Returns every HTTP status as a response; raises TransportError when no
        response was received.
        """


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTP errors before credentials can be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | Iterable[bytes] | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read(), _lower(response.headers))
        except urllib.error.HTTPError as error:
            with error:
                try:
                    payload = error.read()
                except OSError:
                    payload = b""
                return HttpResponse(error.code, payload or b"", _lower(error.headers))
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as error:
            raise TransportError(str(getattr(error, "reason", error))) from error


def _lower(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    return {str(name).lower(): str(value) for name, value in headers.items()}


class _RangedBody:
    def __init__(
        self,
        path: Path,
        start: int,
        size: int,
        cancel_event: Event,
        on_bytes: Callable[[int], None],
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.path = path
        self.start = start
        self.size = size
        self.cancel_event = cancel_event
        self.on_bytes = on_bytes
        self.chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        remaining = self.size
        with self.path.open("rb") as stream:
            stream.seek(self.start)
            while remaining:
                if self.cancel_event.is_set():
                    raise UploadCancelled("Upload cancelled.")
                data = stream.read(min(self.chunk_size, remaining))
                if not data:
                    raise ProtocolError(
                        f"Export file ended before the expected {self.size} byte range."
                    )
                remaining -= len(data)
                self.on_bytes(len(data))
                yield data


class ApiClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ConfigurationError("Add a PlayCanvas API key first.")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or UrllibTransport()
        self._validate_url(self.base_url, signed=False)

    # getMe
    def get_me(self) -> dict[str, Any]:
        return self._object(self._request_json("GET", "/v1/me"), "account")

    # getSplat
    def get_splat(self, splat_id: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(splat_id, safe="")
        return self._object(self._request_json("GET", f"/v1/splats/{encoded_id}"), "splat")

    # createSplatDownload
    def create_download(self, splat_id: str, target: str = "ply") -> dict[str, Any]:
        encoded_id = urllib.parse.quote(splat_id, safe="")
        return self._object(
            self._request_json("POST", f"/v1/splats/{encoded_id}/downloads", {"target": target}),
            "download",
        )

    # getSplatDownload: the scene's current download of `target`; changes nothing.
    def get_download(self, splat_id: str, target: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(splat_id, safe="")
        encoded_target = urllib.parse.quote(target, safe="")
        return self._object(
            self._request_json("GET", f"/v1/splats/{encoded_id}/downloads/{encoded_target}"),
            "download",
        )

    # createSplatDownloadFile: a signed URL for a ready download.
    def create_download_file(self, splat_id: str, target: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(splat_id, safe="")
        encoded_target = urllib.parse.quote(target, safe="")
        return self._object(
            self._request_json("POST", f"/v1/splats/{encoded_id}/downloads/{encoded_target}/file"),
            "download file",
        )

    # createSplatUpload
    def create_upload(self, body: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._object(
            self._request_json("POST", "/v1/splats/uploads", body, headers),
            "upload session",
        )

    # createSplatUploadPartUrls
    def create_part_urls(self, upload_id: str, part_numbers: list[int]) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(upload_id, safe="")
        return self._object(
            self._request_json(
                "POST",
                f"/v1/splats/uploads/{encoded_id}/part-upload-urls",
                {"parts": part_numbers},
            ),
            "signed URL response",
        )

    # completeSplatUpload
    def complete_upload(self, upload_id: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(upload_id, safe="")
        return self._object(
            self._request_json(
                "POST",
                f"/v1/splats/uploads/{encoded_id}/complete",
                {"parts": parts},
            ),
            "completed upload",
        )

    def put_part(
        self,
        url: str,
        file_path: Path,
        start: int,
        size: int,
        cancel_event: Event,
        on_bytes: Callable[[int], None],
    ) -> str:
        """PUT one byte range to a signed URL. The API key is never sent here."""
        self._validate_url(url, signed=True)
        body = _RangedBody(file_path, start, size, cancel_event, on_bytes)
        headers = {"Content-Length": str(size), "User-Agent": USER_AGENT}
        try:
            response = self.transport.send("PUT", url, headers, body, self.timeout)
        except TransportError as error:
            raise ApiError(f"Part upload could not reach storage: {error}") from error
        if not 200 <= response.status <= 299:
            raise _http_error(response)
        etag = response.headers.get("etag")
        if not etag:
            raise ApiError(
                "The storage service accepted a part but returned no ETag.", status=response.status
            )
        return etag

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
        }
        # A POST without a body still says so, with Content-Length: 0.
        data = b"" if method == "POST" else None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self.transport.send(method, url, headers, data, self.timeout)
        except TransportError as error:
            raise ApiError(f"Could not reach SuperSplat: {error}") from error
        if not 200 <= response.status <= 299:
            raise _http_error(response)
        if not response.body:
            raise ProtocolError(f"SuperSplat returned an empty response for {method} {path}.")
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(f"SuperSplat returned invalid JSON for {method} {path}.") from error

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProtocolError(f"SuperSplat returned an invalid {label} response.")
        return value

    def _validate_url(self, value: str, *, signed: bool) -> None:
        if not is_secure_url(value):
            label = "signed upload URL" if signed else "API base URL"
            raise ConfigurationError(f"The {label} must be a valid HTTPS URL.")


_STATUS_TEXT = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
}

_STATUS_HINTS = {
    401: "Check the PlayCanvas API key in Preferences > Add-ons > SuperSplat.",
    403: "The PlayCanvas API key does not have permission for this SuperSplat request.",
}


def format_bytes(byte_count: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(byte_count, 0))
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{value:.0f} B" if unit == 0 else f"{value:.1f} {units[unit]}"


def _http_error(response: HttpResponse) -> ApiError:
    try:
        payload = json.loads(response.body.decode("utf-8")) if response.body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    return ApiError(
        _error_message(payload, response.status),
        status=response.status,
        body=payload,
        retry_after=_retry_after(response.headers.get("retry-after")),
    )


def _error_message(body: Any, status: int) -> str:
    """Build a user-facing message from an ErrorResponse-shaped body."""
    if isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"]:
        message = body["error"].rstrip(".") + "."
    else:
        message = f"{status} {_STATUS_TEXT.get(status, '')}".strip() + "."
    code = body.get("code") if isinstance(body, dict) else None
    if code == "file_too_large" and isinstance(body.get("limitBytes"), int):
        message += f" The limit for this format is {format_bytes(body['limitBytes'])}."
    elif code == "daily_publish_limit_reached" and isinstance(body.get("resetsAt"), str):
        message += f" The limit resets at {body['resetsAt']}."
    hint = _STATUS_HINTS.get(status)
    if hint:
        message += f" {hint}"
    return message


def _retry_after(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isdigit():
        try:
            delay = float(value)
        except (ValueError, OverflowError):
            return None
        return delay if math.isfinite(delay) else None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
