"""Background export + upload session, independent of Blender."""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .api import DEFAULT_BASE_URL, Transport, format_bytes
from .axes import ExportTransform, transform_splats
from .credentials import KEY_READ_TIMEOUT, CredentialStore, key_read_status, read_api_key
from .errors import SuperSplatError, UploadCancelled
from .ply import SplatData, write_3dgs_ply
from .uploader import Progress, UploadCallbacks, UploadEngine, UploadJob, validate_metadata

log = logging.getLogger(__name__)

SOURCE_FORMAT = "ply"


def resolve_base_url(environ: Mapping[str, str] | None = None) -> str:
    """SUPERSPLAT_BASE_URL if set, else the public API."""
    environ = os.environ if environ is None else environ
    return (environ.get("SUPERSPLAT_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


@dataclass(frozen=True)
class PublishRequest:
    data: SplatData
    title: str
    description: str
    credentials: CredentialStore
    base_url: str
    staging_dir: Path
    sh_degree: int | None = None
    key_timeout: float = KEY_READ_TIMEOUT
    transform: ExportTransform = ExportTransform()


@dataclass
class PublishState:
    phase: str = "idle"  # idle, authorizing, exporting, uploading, complete, failed, cancelled
    status_text: str = ""
    progress: float = 0.0
    progress_text: str = ""
    error: str = ""
    account: str = ""
    title: str = ""
    file_size: int = 0
    splat_id: str = ""
    edit_url: str = ""
    viewer_url: str = ""

    @property
    def active(self) -> bool:
        return self.phase in {"authorizing", "exporting", "uploading"}


class PublishSession:
    def __init__(
        self,
        request: PublishRequest,
        *,
        transport: Transport | None = None,
        retries: int = 3,
    ) -> None:
        validate_metadata(request.title, request.description)
        self.request = request
        self.transport = transport
        self.retries = retries
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._state = PublishState(
            phase="authorizing", status_text=key_read_status(request.credentials), title=request.title
        )
        self._thread: threading.Thread | None = None

    def snapshot(self) -> PublishState:
        with self._lock:
            return copy.copy(self._state)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="supersplat-publish")
        self._thread.start()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def cancel(self) -> None:
        self.cancel_event.set()
        self._update(status_text="Cancelling...")

    def run(self) -> PublishState:
        request = self.request
        job_id = uuid.uuid4().hex
        path = Path(request.staging_dir) / f"{job_id}.{SOURCE_FORMAT}"
        try:
            self._update(phase="authorizing", status_text=key_read_status(request.credentials), progress=0.0)
            api_key = read_api_key(
                request.credentials, timeout=request.key_timeout, cancel_event=self.cancel_event
            )
            self._update(phase="exporting", status_text="Exporting PLY...", progress=0.0)
            size = write_3dgs_ply(
                path,
                transform_splats(request.data, request.transform),
                request.sh_degree,
                cancel_event=self.cancel_event,
                on_progress=lambda fraction: self._update(progress=fraction),
            )
            job = UploadJob(
                file_path=str(path),
                source_format=SOURCE_FORMAT,
                title=request.title,
                description=request.description,
                idempotency_key=f"supersplat-{job_id}",
                base_url=request.base_url,
            )
            self._update(
                phase="uploading",
                status_text="Preparing upload...",
                progress=0.0,
                progress_text=format_bytes(size),
                file_size=size,
            )
            engine = UploadEngine(retries=self.retries, transport=self.transport)
            outcome = engine.upload(job, api_key, self.cancel_event, self._callbacks())
            self._update(
                phase="complete",
                status_text="Upload accepted by SuperSplat. Processing continues online.",
                progress=1.0,
                splat_id=outcome.splat_id,
                edit_url=outcome.edit_url,
                viewer_url=outcome.viewer_url,
                error="",
            )
        except UploadCancelled:
            self._update(phase="cancelled", status_text="Publish cancelled.", error="")
        except SuperSplatError as error:
            self._fail(str(error) or error.__class__.__name__)
        except Exception as error:  # noqa: BLE001 - surfaced in the panel; the traceback goes to the log
            log.exception("Unexpected error publishing to SuperSplat")
            self._fail(f"Unexpected error: {error.__class__.__name__}: {error}")
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return self.snapshot()

    def _callbacks(self) -> UploadCallbacks:
        def stage(message: str) -> None:
            self._update(status_text=message)

        def account(me: dict[str, Any]) -> None:
            self._update(account=str(me.get("username") or me.get("id") or ""))

        def progress(value: Progress) -> None:
            self._update(
                progress=value.fraction,
                progress_text=(
                    f"{format_bytes(value.uploaded_bytes)} / {format_bytes(value.total_bytes)}"
                    f"  ·  {format_bytes(value.bytes_per_second)}/s"
                    f"  ·  ETA {format_duration(value.eta_seconds)}"
                ),
            )

        return UploadCallbacks(on_stage=stage, on_account=account, on_progress=progress)

    def _fail(self, message: str) -> None:
        self._update(phase="failed", status_text="Publish failed.", error=message)

    def _update(self, **values: Any) -> None:
        with self._lock:
            for name, value in values.items():
                setattr(self._state, name, value)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    minutes, secs = divmod(max(int(seconds + 0.999), 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
