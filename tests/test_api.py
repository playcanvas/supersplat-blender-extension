from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import guards
from fake_api import FAKE_API_KEY, FAKE_BASE_URL, FAKE_STORAGE, FakeSuperSplat
from supersplat.api import (
    ApiClient,
    HttpResponse,
    TransportError,
    UrllibTransport,
    _RejectRedirects,
    _retry_after,
    is_secure_url,
)
from supersplat import USER_AGENT
from supersplat.errors import ApiError, ConfigurationError, ProtocolError


def setUpModule() -> None:
    guards.block_all()


class _Scripted:
    """Transport returning one canned response and recording the request."""

    def __init__(self, response: HttpResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, str], object]] = []

    def send(self, method, url, headers, body, timeout):  # noqa: ANN001
        self.calls.append((method, url, headers, body))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(transport) -> ApiClient:  # noqa: ANN001
    return ApiClient(FAKE_API_KEY, FAKE_BASE_URL, transport=transport)


class ApiClientTests(unittest.TestCase):
    def test_requests_carry_bearer_accept_and_user_agent(self) -> None:
        fake = FakeSuperSplat()
        self.assertEqual(_client(fake).get_me()["id"], "user-0001")
        headers = fake.requests[0].headers
        self.assertEqual(headers["Authorization"], f"Bearer {FAKE_API_KEY}")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["User-Agent"], USER_AGENT)

    def test_create_upload_sends_json_and_idempotency_key(self) -> None:
        fake = FakeSuperSplat()
        session = _client(fake).create_upload(
            {"sourceFormat": "ply", "contentLength": 10}, idempotency_key="key-1"
        )
        self.assertEqual(session["status"], "created")
        request = fake.requests[0]
        self.assertEqual(request.headers["Idempotency-Key"], "key-1")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.json, {"sourceFormat": "ply", "contentLength": 10})
        self.assertEqual(fake.violations, [])

    def test_opaque_ids_are_encoded_as_single_path_segments(self) -> None:
        transport = _Scripted(HttpResponse(200, b'{"id": "x"}'))
        _client(transport).get_splat("splat/one")
        self.assertTrue(transport.calls[0][1].endswith("/v1/splats/splat%2Fone"))

    def test_error_response_message_is_surfaced_with_auth_hint(self) -> None:
        fake = FakeSuperSplat(api_key="another-key")
        with self.assertRaises(ApiError) as raised:
            _client(fake).get_me()
        self.assertEqual(raised.exception.status, 401)
        self.assertIn("Unauthorized.", str(raised.exception))
        self.assertIn("Preferences > Add-ons > SuperSplat", str(raised.exception))
        self.assertFalse(raised.exception.transient)

    def test_status_without_json_body_uses_status_text(self) -> None:
        transport = _Scripted(HttpResponse(503, b"<html>down</html>"))
        with self.assertRaises(ApiError) as raised:
            _client(transport).get_me()
        self.assertEqual(str(raised.exception), "503.")
        self.assertTrue(raised.exception.transient)

    def test_file_too_large_reports_limit(self) -> None:
        body = b'{"error":"File too large","code":"file_too_large","limitBytes":1073741824,"fileBytes":2147483648}'
        with self.assertRaises(ApiError) as raised:
            _client(_Scripted(HttpResponse(413, body))).create_upload({"sourceFormat": "ply"}, idempotency_key="")
        self.assertEqual(raised.exception.code, "file_too_large")
        self.assertIn("1.0 GB", str(raised.exception))

    def test_daily_publish_limit_is_not_retried(self) -> None:
        body = (
            b'{"error":"Daily publishing limit reached","code":"daily_publish_limit_reached",'
            b'"resetsAt":"2099-01-02T00:00:00.000Z"}'
        )
        response = HttpResponse(429, body, {"retry-after": "3600"})
        with self.assertRaises(ApiError) as raised:
            _client(_Scripted(response)).create_upload({"sourceFormat": "ply"}, idempotency_key="")
        self.assertFalse(raised.exception.transient)
        self.assertEqual(raised.exception.retry_after, 3600.0)
        self.assertIn("resets at 2099-01-02", str(raised.exception))

    def test_plain_rate_limit_and_publish_in_progress_are_transient(self) -> None:
        self.assertTrue(ApiError("x", status=429, body={"error": "Too many requests"}).transient)
        self.assertTrue(
            ApiError("x", status=429, body={"error": "busy", "code": "publish_in_progress"}).transient
        )
        self.assertFalse(
            ApiError("x", status=429, body={"error": "busy", "code": "too_many_processing"}).transient
        )

    def test_redirect_status_is_an_error(self) -> None:
        transport = _Scripted(HttpResponse(302, b"", {"location": "https://elsewhere.test/"}))
        with self.assertRaises(ApiError) as raised:
            _client(transport).get_me()
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(len(transport.calls), 1)

    def test_transport_failure_becomes_transient_api_error(self) -> None:
        with self.assertRaises(ApiError) as raised:
            _client(_Scripted(TransportError("no route"))).get_me()
        self.assertIsNone(raised.exception.status)
        self.assertTrue(raised.exception.transient)
        self.assertIn("Could not reach SuperSplat", str(raised.exception))

    def test_invalid_and_empty_json_are_protocol_errors(self) -> None:
        with self.assertRaises(ProtocolError):
            _client(_Scripted(HttpResponse(200, b"not json"))).get_me()
        with self.assertRaises(ProtocolError):
            _client(_Scripted(HttpResponse(200, b""))).get_me()
        with self.assertRaises(ProtocolError):
            _client(_Scripted(HttpResponse(200, b"[]"))).get_me()

    def test_rejects_non_https_base_url_and_empty_key(self) -> None:
        for url in ("http://api.supersplat.test", "http://127.0.0.2:8765/api", "http://localhost.evil.test/api", "ftp://localhost/api"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                ApiClient(FAKE_API_KEY, url, transport=FakeSuperSplat())
        with self.assertRaises(ConfigurationError):
            ApiClient("   ", FAKE_BASE_URL, transport=FakeSuperSplat())

    def test_plain_http_is_allowed_only_on_localhost(self) -> None:
        for url in ("http://127.0.0.1:8765/api/supersplat", "http://localhost:8765/api/supersplat", "http://LOCALHOST/api"):
            with self.subTest(url=url):
                fake = FakeSuperSplat(base_url=url.rstrip("/"))
                self.assertEqual(ApiClient(FAKE_API_KEY, url, transport=fake).get_me()["id"], "user-0001")
        self.assertTrue(is_secure_url("https://storage.example.test/a/meta.json"))
        for url in (
            "http://storage.example.test/meta.json",
            "http://[::1]/meta.json",
            "http://user@127.0.0.1/meta.json",
            "https://user@storage.example.test/meta.json",
            "https:///meta.json",
            "https://storage.example.test:port/meta.json",
            "file:///meta.json",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_secure_url(url))

    def test_downloads_follow_the_spec(self) -> None:
        from fake_api import demo_scenes

        fake = FakeSuperSplat().add_scenes(demo_scenes())
        client = _client(fake)
        created = client.create_download("plydemo1")
        self.assertEqual((created["status"], created["target"], created["version"]), ("queued", "ply", 1))
        self.assertNotIn("id", created)
        self.assertEqual(fake.requests[-1].json, {"target": "ply"})
        with self.assertRaises(ApiError) as raised:
            client.create_download_file("plydemo1", "ply")
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "not_ready"))
        self.assertEqual(client.get_download("plydemo1", "ply")["status"], "processing")
        self.assertTrue(fake.requests[-1].url.endswith("/v1/splats/plydemo1/downloads/ply"))
        self.assertEqual(client.get_download("plydemo1", "ply")["status"], "ready")
        file = client.create_download_file("plydemo1", "ply")
        self.assertEqual((sorted(file), file["version"]), (["bytes", "url", "urlExpiresAt", "version"], 1))
        self.assertTrue(fake.requests[-1].url.endswith("/v1/splats/plydemo1/downloads/ply/file"))
        self.assertEqual(client.create_download("lodsdemo", "ssog")["target"], "ssog")
        self.assertEqual(fake.requests[-1].json, {"target": "ssog"})
        with self.assertRaises(ApiError) as raised:
            client.get_download("sogdemo1", "sog")
        self.assertEqual((raised.exception.status, raised.exception.body), (404, {"error": "download not found"}))
        self.assertEqual(fake.violations, [])

    def test_daily_download_limit_is_not_retried(self) -> None:
        from fake_api import demo_scenes

        fake = FakeSuperSplat(daily_limit_reached=True).add_scenes(demo_scenes())
        client = _client(fake)
        self.assertEqual(client.create_download("plydemo1")["status"], "queued")
        client.get_download("plydemo1", "ply")
        client.get_download("plydemo1", "ply")
        with self.assertRaises(ApiError) as raised:
            client.create_download_file("plydemo1", "ply")
        error = raised.exception
        self.assertEqual((error.status, error.code, error.transient, error.retry_after), (429, "daily_download_limit_reached", False, 22320.0))
        self.assertTrue(str(error).startswith("Your account has reached today's download limit."))
        self.assertEqual(fake.violations, [])

    def test_put_part_streams_range_without_api_key(self) -> None:
        fake = FakeSuperSplat()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            path.write_bytes(b"0123456789")
            seen: list[int] = []
            etag = _client(fake).put_part(
                f"{FAKE_STORAGE}/{fake.upload_id}/part-1?signature=dummy",
                path, 2, 5, threading.Event(), seen.append,
            )
        self.assertEqual(etag, '"etag-1"')
        self.assertEqual(fake.parts[1], b"23456")
        self.assertEqual(sum(seen), 5)
        self.assertNotIn("Authorization", fake.requests[0].headers)
        self.assertEqual(fake.violations, [])

    def test_put_part_without_etag_or_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            path.write_bytes(b"0123456789")
            for response in (HttpResponse(200, b""), HttpResponse(403, b"denied")):
                client = _client(_Scripted(response))
                with self.assertRaises(ApiError):
                    client.put_part(f"{FAKE_STORAGE}/u/part-1?s", path, 0, 10, threading.Event(), lambda _n: None)

    def test_signed_url_must_be_https(self) -> None:
        with self.assertRaises(ConfigurationError):
            _client(FakeSuperSplat()).put_part(
                "http://storage.example.invalid/x", Path("unused"), 0, 1, threading.Event(), lambda _n: None
            )

    def test_retry_after_parsing(self) -> None:
        self.assertEqual(_retry_after("12"), 12.0)
        self.assertIsNone(_retry_after("soon"))
        self.assertIsNone(_retry_after(None))
        self.assertEqual(_retry_after("Thu, 01 Jan 1970 00:00:00 GMT"), 0.0)


class UrllibTransportTests(unittest.TestCase):
    def test_opener_rejects_redirects_before_credentials_can_be_forwarded(self) -> None:
        handler = _RejectRedirects()
        request = urllib.request.Request("https://api.supersplat.test/v1/me")
        self.assertIsNone(
            handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.test/")
        )
        transport = UrllibTransport()
        self.assertTrue(any(isinstance(h, _RejectRedirects) for h in transport._opener.handlers))

    def test_http_errors_become_responses_and_url_errors_raise(self) -> None:
        transport = UrllibTransport()
        headers = Message()
        headers["Retry-After"] = "5"
        error = urllib.error.HTTPError("https://x.test", 429, "Too Many", headers, None)
        with patch.object(transport._opener, "open", side_effect=error):
            response = transport.send("GET", "https://x.test", {}, None, 1)
        self.assertEqual((response.status, response.headers.get("retry-after")), (429, "5"))
        with patch.object(transport._opener, "open", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(TransportError):
                transport.send("GET", "https://x.test", {}, None, 1)


if __name__ == "__main__":
    unittest.main()
