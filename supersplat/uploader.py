"""Multipart upload to SuperSplat: create, sign, PUT parts, complete."""

from __future__ import annotations

import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import UPLOAD_CLIENT_ID, __version__
from .api import ApiClient, Transport
from .errors import ApiError, ConfigurationError, ProtocolError, UploadCancelled

# Limits from the OpenAPI spec (CompleteUploadRequest, CreateUploadRequest.contentLength,
# title and description).
MAX_MULTIPART_PARTS = 10_000
MAX_FILE_SIZE = 16_106_127_360
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 5000
# Parts uploaded at once; also the number signed per request (the spec allows up to 100).
PARALLEL_PARTS = 4


@dataclass(frozen=True)
class UploadJob:
    file_path: str
    source_format: str
    title: str
    description: str
    idempotency_key: str
    base_url: str


@dataclass(frozen=True)
class UploadOutcome:
    splat_id: str
    edit_url: str
    viewer_url: str


@dataclass(frozen=True)
class Progress:
    uploaded_bytes: int
    total_bytes: int
    bytes_per_second: float
    eta_seconds: float | None

    @property
    def fraction(self) -> float:
        return self.uploaded_bytes / self.total_bytes if self.total_bytes else 0.0


def _ignore(*_args: Any) -> None:
    pass


@dataclass
class UploadCallbacks:
    on_stage: Callable[[str], None] = _ignore
    on_account: Callable[[dict[str, Any]], None] = _ignore
    on_progress: Callable[[Progress], None] = _ignore


class _ProgressTracker:
    """Bytes of finished parts plus bytes of the current attempt of each part in flight."""

    def __init__(self, total_bytes: int) -> None:
        self.total_bytes = total_bytes
        self._done = 0
        self._in_flight: dict[int, int] = {}
        self._lock = threading.Lock()
        self._started_at = time.monotonic()

    def snapshot(self) -> Progress:
        with self._lock:
            return self._snapshot()

    def add(self, part_number: int, byte_count: int) -> Progress:
        with self._lock:
            self._in_flight[part_number] = self._in_flight.get(part_number, 0) + byte_count
            return self._snapshot()

    def reset(self, part_number: int) -> Progress:
        with self._lock:
            self._in_flight.pop(part_number, None)
            return self._snapshot()

    def finish(self, part_number: int, size: int) -> Progress:
        with self._lock:
            self._in_flight.pop(part_number, None)
            self._done += size
            return self._snapshot()

    def _snapshot(self) -> Progress:
        uploaded = min(self._done + sum(self._in_flight.values()), self.total_bytes)
        rate = uploaded / max(time.monotonic() - self._started_at, 1e-6)
        eta = (self.total_bytes - uploaded) / rate if rate > 0 else None
        return Progress(uploaded, self.total_bytes, rate, eta)


def validate_metadata(title: str, description: str) -> None:
    if len(title) > MAX_TITLE_LENGTH:
        raise ConfigurationError(f"The title must be at most {MAX_TITLE_LENGTH} characters.")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ConfigurationError(
            f"The description must be at most {MAX_DESCRIPTION_LENGTH} characters."
        )


class UploadEngine:
    def __init__(
        self,
        *,
        retries: int = 3,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self.retries = max(0, int(retries))
        self.timeout = timeout
        self.transport = transport

    def upload(
        self,
        job: UploadJob,
        api_key: str,
        cancel_event: threading.Event,
        callbacks: UploadCallbacks | None = None,
    ) -> UploadOutcome:
        callbacks = callbacks or UploadCallbacks()
        validate_metadata(job.title, job.description)
        path = Path(job.file_path)
        file_size = _file_size(path)
        self._check_cancel(cancel_event)
        client = ApiClient(api_key, job.base_url, timeout=self.timeout, transport=self.transport)

        callbacks.on_stage("Authenticating with SuperSplat...")
        account = self._retry(client.get_me, cancel_event)
        _required_string(account, "id", "account")
        callbacks.on_account(account)

        callbacks.on_stage("Creating upload session...")
        session = self._retry(
            lambda: client.create_upload(
                create_upload_body(job, file_size), idempotency_key=job.idempotency_key
            ),
            cancel_event,
        )
        upload_id = _required_string(session, "id", "upload session")
        status = _required_string(session, "status", "upload session")
        if status not in {"created", "uploading"}:
            raise ProtocolError(f"The SuperSplat upload session is already {status}.")
        part_size = _required_positive_int(session, "partSize", "upload session")
        total_parts = math.ceil(file_size / part_size)
        if total_parts > MAX_MULTIPART_PARTS:
            raise ProtocolError(
                f"SuperSplat returned a part size that would require {total_parts} parts; "
                f"the upload API supports at most {MAX_MULTIPART_PARTS}."
            )

        tracker = _ProgressTracker(file_size)
        callbacks.on_progress(tracker.snapshot())
        callbacks.on_stage(f"Uploading {total_parts} parts...")
        etags: dict[int, str] = {}
        url_lock = threading.Lock()

        def upload_one(part_number: int, url: str) -> None:
            size = part_size_for(file_size, part_size, part_number)
            for attempt in range(self.retries + 1):
                self._check_cancel(cancel_event)
                try:
                    etags[part_number] = client.put_part(
                        url,
                        path,
                        (part_number - 1) * part_size,
                        size,
                        cancel_event,
                        lambda count: callbacks.on_progress(tracker.add(part_number, count)),
                    )
                    callbacks.on_progress(tracker.finish(part_number, size))
                    return
                except ApiError as error:
                    callbacks.on_progress(tracker.reset(part_number))
                    if attempt >= self.retries or not error.transient:
                        raise
                    self._wait_for_retry(attempt, cancel_event, error.retry_after)
                    # Signed URLs expire; sign the part again before retrying.
                    with url_lock:
                        url = self._signed_urls(
                            client, upload_id, [part_number], part_size, cancel_event
                        )[part_number]

        numbers = list(range(1, total_parts + 1))
        for start in range(0, total_parts, PARALLEL_PARTS):
            batch = numbers[start : start + PARALLEL_PARTS]
            self._check_cancel(cancel_event)
            signed = self._signed_urls(client, upload_id, batch, part_size, cancel_event)
            with ThreadPoolExecutor(len(batch), thread_name_prefix="supersplat-part") as executor:
                futures = [executor.submit(upload_one, number, signed[number]) for number in batch]
                try:
                    for future in as_completed(futures):
                        future.result()
                except Exception:
                    cancel_event.set()
                    raise

        self._check_cancel(cancel_event)
        callbacks.on_stage("Finalizing upload...")
        parts = [{"partNumber": number, "etag": etags[number]} for number in numbers]
        result = self._retry(lambda: client.complete_upload(upload_id, parts), cancel_event)
        if _required_string(result, "uploadId", "completed upload") != upload_id:
            raise ProtocolError("SuperSplat completed a different upload session than requested.")
        if _required_string(result, "status", "completed upload") != "completed":
            raise ProtocolError("SuperSplat did not complete the upload.")
        splat_id = _required_string(result, "splatId", "completed upload")
        edit_url = _required_string(result, "editUrl", "completed upload")
        splat = self._retry(lambda: client.get_splat(splat_id), cancel_event)
        if _required_string(splat, "id", "splat") != splat_id:
            raise ProtocolError("SuperSplat returned a different splat than requested.")
        return UploadOutcome(splat_id, edit_url, _required_string(splat, "viewerUrl", "splat"))

    def _signed_urls(
        self,
        client: ApiClient,
        upload_id: str,
        numbers: list[int],
        part_size: int,
        cancel_event: threading.Event,
    ) -> dict[int, str]:
        response = self._retry(lambda: client.create_part_urls(upload_id, numbers), cancel_event)
        if _required_string(response, "uploadId", "signed URL response") != upload_id:
            raise ProtocolError("SuperSplat returned signed URLs for a different upload session.")
        if _required_positive_int(response, "partSize", "signed URL response") != part_size:
            raise ProtocolError("SuperSplat changed the upload part size during the session.")
        items = response.get("urls")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ProtocolError("SuperSplat did not return signed part URLs.")
        result = {
            _required_positive_int(item, "partNumber", "signed URL"): _required_string(
                item, "url", "signed URL"
            )
            for item in items
        }
        if sorted(result) != sorted(numbers) or len(items) != len(numbers):
            raise ProtocolError("SuperSplat returned signed URLs for the wrong parts.")
        return result

    def _retry(self, operation: Callable[[], Any], cancel_event: threading.Event) -> Any:
        for attempt in range(self.retries + 1):
            self._check_cancel(cancel_event)
            try:
                return operation()
            except ApiError as error:
                if attempt >= self.retries or not error.transient:
                    raise
                self._wait_for_retry(attempt, cancel_event, error.retry_after)
        raise AssertionError("unreachable")

    @staticmethod
    def _wait_for_retry(
        attempt: int, cancel_event: threading.Event, retry_after: float | None = None
    ) -> None:
        delay = min(0.5 * (2**attempt) + random.random() * 0.25, 5.0)
        if retry_after is not None and retry_after >= 0:
            delay = max(delay, retry_after)
        if cancel_event.wait(delay):
            raise UploadCancelled("Upload cancelled.")

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise UploadCancelled("Upload cancelled.")


def create_upload_body(job: UploadJob, file_size: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sourceFormat": job.source_format,
        "contentLength": file_size,
        "uploadClient": {"id": UPLOAD_CLIENT_ID, "version": __version__},
    }
    if job.title:
        body["title"] = job.title
    if job.description:
        body["description"] = job.description
    return body


def part_size_for(file_size: int, part_size: int, part_number: int) -> int:
    start = (part_number - 1) * part_size
    return max(min(part_size, file_size - start), 0)


def _file_size(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ConfigurationError("The staged export is unavailable.") from error
    if size <= 0:
        raise ConfigurationError("The staged export is empty.")
    if size > MAX_FILE_SIZE:
        raise ConfigurationError("SuperSplat accepts uploads of at most 15 GiB.")
    return size


def _required_string(value: dict[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProtocolError(f"SuperSplat returned an invalid {label}: missing {key}.")
    return result


def _required_positive_int(value: dict[str, Any], key: str, label: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ProtocolError(
            f"SuperSplat returned an invalid {label}: {key} must be a positive integer."
        )
    return result
