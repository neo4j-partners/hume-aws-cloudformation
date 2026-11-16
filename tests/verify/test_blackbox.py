"""Tests for the black box checks. Every HTTP call goes to a fake server."""

import argparse
import contextlib
import email.parser
import email.policy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError

import blackbox
import deploy

BASE_DOMAIN = "test.example.com"
HOSTS = deploy.hume_hosts(BASE_DOMAIN)
WEB_HOST, API_HOST, MEDIA_HOST = HOSTS
PASSWORD = "pw-never-printed-0123456789"
TOKEN = "token-never-printed-abcdef"
TARGET = blackbox.Target(
    stack="hume-ecs-test",
    hosts=HOSTS,
    username="admin",
    password_secret="hume/hume-ecs-test/admin-password",
)
OIDC_TARGET = blackbox.Target(
    stack="hume-ecs-test",
    hosts=HOSTS,
    username="hume-admin",
    password_secret="hume/hume-test-idp/cognito/hume-admin",
    auth_provider=deploy.OIDC_PROVIDER,
)
DNS_TLS_PASS = blackbox.Outcome(blackbox.PASS, "fake DNS and TLS")
CONFIG_ID = "config-id-never-printed-42"
UPLOAD_CONFIGS = ("GET", API_HOST, blackbox.UPLOAD_CONFIGS_PATH)
# The slash and space check that the ID is quoted as one path segment.
FILE_ID = "file/id never-printed-7"
DELETE_FILE = (
    "DELETE",
    API_HOST,
    "/media/management/files/file%2Fid%20never-printed-7",
)
NEEDS_CONFIG = {
    "title": "Internal Server Error",
    "status": 500,
    "detail": "Upload configuration data is required for this file",
    "rootExceptionClassName": "com.hume.media.protocol.management.uploadconfig."
    "exception.FileRequiresUploadConfiguration",
}


def page(entries: list[dict], last: bool = True, total_pages: int = 1) -> dict:
    """A Spring page with the keys keyz saw from the local API."""
    return {
        "content": entries,
        "empty": not entries,
        "first": True,
        "last": last,
        "number": 0,
        "numberOfElements": len(entries),
        "pageable": {"pageNumber": 0, "pageSize": 20},
        "size": 20,
        "sort": {"sorted": False},
        "totalElements": len(entries),
        "totalPages": total_pages,
    }


def reply(status: int, body: object = b"", content_type: str = "") -> blackbox.Response:
    if isinstance(body, bytes):
        return blackbox.Response(status, content_type or "text/plain", body)
    return blackbox.Response(status, "application/json", json.dumps(body).encode())


class FakeHttp:
    """Answers like a healthy stack. Tests replace single routes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.uploaded: bytes | None = None
        self.deleted = False
        self.routes = {
            ("GET", API_HOST, blackbox.PROMETHEUS_PATH): lambda r: reply(
                403, b"Forbidden"
            ),
            ("GET", WEB_HOST, "/"): lambda r: reply(200, b"<html></html>", "text/html"),
            ("GET", MEDIA_HOST, "/v1/download"): self.download,
            ("GET", MEDIA_HOST, blackbox.NOT_MEDIA_PATH): lambda r: reply(
                403, b"Forbidden"
            ),
            ("GET", blackbox.UNKNOWN_HOST, "/"): lambda r: reply(403, b"Forbidden"),
            ("POST", API_HOST, "/login"): self.login,
            ("GET", API_HOST, "/users/me"): self.users_me,
            ("GET", API_HOST, "/settings"): lambda r: reply(
                200, {"media": {"mediaBaseUrl": f"https://{MEDIA_HOST}/v1"}}
            ),
            ("GET", API_HOST, "/info"): lambda r: reply(200, {"version": "3.2.0"}),
            ("POST", MEDIA_HOST, "/v1/download/init"): lambda r: reply(200, {}),
            ("POST", MEDIA_HOST, "/v1/upload"): self.upload,
            UPLOAD_CONFIGS: self.upload_configs,
            DELETE_FILE: self.delete,
        }

    def call(self, method, url, body=None, headers=None) -> blackbox.Response:
        headers = headers or {}
        self.calls.append((method, url, body, headers))
        parts = urllib.parse.urlsplit(url)
        host = headers.get("Host", parts.hostname)
        route = self.routes.get((method, host, parts.path))
        if route is None:
            return reply(404, b"")
        return route((parts, body, headers))

    def status(self, method, url) -> int:
        return self.call(method, url).status

    def cookie_names(self, domain: str) -> list[str]:
        return ["media-session"]

    @staticmethod
    def signed_in(headers: dict[str, str]) -> bool:
        return headers.get("Authorization") == f"Bearer {TOKEN}"

    def login(self, request) -> blackbox.Response:
        credentials = json.loads(request[1])
        if credentials == {"username": "admin", "password": PASSWORD}:
            return reply(200, {"token": TOKEN})
        return reply(401, {"error": "Unauthorized"})

    def users_me(self, request) -> blackbox.Response:
        if not self.signed_in(request[2]):
            return reply(401, b"")
        return reply(200, {"username": "admin", "email": "admin@example.com"})

    def upload_configs(self, request) -> blackbox.Response:
        if not self.signed_in(request[2]):
            return reply(401, b"")
        return reply(
            200,
            page(
                [
                    {"id": "images-id", "name": "Images", "description": "x"},
                    {"id": CONFIG_ID, "name": "DEFAULT", "description": "default"},
                ]
            ),
        )

    def upload(self, request) -> blackbox.Response:
        _, body, headers = request
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + body
        )
        parts = {
            p.get_param("name", header="content-disposition"): p
            for p in message.iter_parts()
        }
        self.uploaded_name = parts["file"].get_filename()
        self.req = json.loads(parts["req"].get_payload(decode=True))
        # Like media, refuse a req without a name.
        if not str(self.req.get("name") or "").strip():
            return reply(
                400,
                {
                    "title": "Bad Request",
                    "detail": "Validation failed: [Field error in object 'req' on "
                    "field 'name': rejected value [null]]",
                },
            )
        # Like media, refuse a file without the DEFAULT upload configuration.
        if self.req.get("selectedUploadConfig") != {"uploadConfigId": CONFIG_ID}:
            return reply(500, NEEDS_CONFIG)
        self.uploaded = parts["file"].get_payload(decode=True)
        return reply(200, {"id": FILE_ID})

    def download(self, request) -> blackbox.Response:
        query = urllib.parse.parse_qs(request[0].query)
        if not self.signed_in(request[2]):
            return reply(401, b"")
        stored = self.uploaded is not None and not self.deleted
        if query.get("id") == [FILE_ID] and stored:
            return reply(200, self.uploaded, "application/octet-stream")
        return reply(404, b"")

    def delete(self, request) -> blackbox.Response:
        _, body, headers = request
        if not self.signed_in(headers):
            return reply(401, b"")
        if headers.get("Content-Type") != "application/json":
            return reply(415, {"title": "Unsupported Media Type"})
        # Like media, a soft delete keeps the bytes.
        if json.loads(body) == {"hardDelete": True}:
            self.deleted = True
        return reply(204, b"")


class RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeHttp()
        self.password_reads = 0
        for name in ("check_dns_tls", "check_http_redirect", "check_old_ports_closed"):
            patcher = mock.patch.object(blackbox, name, return_value=DNS_TLS_PASS)
            patcher.start()
            self.addCleanup(patcher.stop)

    def read_password(self) -> str:
        self.password_reads += 1
        return PASSWORD

    def run_checks(
        self, read_password=None, target=TARGET, request_token=None
    ) -> tuple[int, dict[str, str], str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = blackbox.run_checks(
                target, self.client, read_password or self.read_password, request_token
            )
        lines = out.getvalue().splitlines()
        statuses = {line.split()[1].rstrip(":"): line.split()[0] for line in lines}
        return code, statuses, out.getvalue()


class ResponseChecksTest(unittest.TestCase):
    def test_alb_text_differs_from_the_empty_media_403(self) -> None:
        self.assertTrue(blackbox.is_alb_forbidden(reply(403, b"Forbidden")))
        self.assertTrue(blackbox.is_alb_forbidden(reply(403, b"Forbidden\n")))
        self.assertFalse(blackbox.is_alb_forbidden(reply(403, b"")))
        self.assertFalse(blackbox.is_alb_forbidden(reply(403, {"error": "Forbidden"})))
        self.assertFalse(blackbox.is_alb_forbidden(reply(200, b"Forbidden")))

    def test_field_names_never_show_values(self) -> None:
        text = blackbox.field_names({"token": TOKEN, "user": {"name": "x"}})
        self.assertEqual(text, "token, user")
        self.assertNotIn(TOKEN, text)
        self.assertIn("not JSON", blackbox.field_names(None))
        self.assertIn("list", blackbox.field_names([1, 2]))

    def test_error_text_removes_secrets_then_cuts(self) -> None:
        body = {"title": "Bad\nRequest", "detail": f"a {TOKEN} b {PASSWORD} c"}
        text = blackbox.error_text(body, [TOKEN, PASSWORD, ""])
        self.assertEqual(text, "Title: Bad Request Detail: a [redacted] b [redacted] c")
        limit = blackbox.ERROR_TEXT_LIMIT
        # The token starts just before the cut, so cutting first would leave
        # its first characters behind.
        long_detail = "x" * (limit - 5) + TOKEN + "y" * 50
        text = blackbox.error_text({"detail": long_detail}, [TOKEN])
        self.assertNotIn(TOKEN[:5], text)
        self.assertTrue(text.endswith("..."))
        self.assertEqual(len(text), len("Detail: ") + limit + len("..."))
        self.assertEqual(blackbox.error_text({"detail": 7}, [TOKEN]), "")
        self.assertEqual(blackbox.error_text(None, [TOKEN]), "")

    def test_error_text_prints_only_the_spring_field_errors(self) -> None:
        signature = "Validation failed for argument [1] in public " + "x" * 300
        errors = (
            "Field error in object 'req' on field 'name': rejected value [null]; "
            "default message [must not be null]. "
            f"Field error in object 'req' on field 'size': token {TOKEN}"
        )
        body = {"title": "Bad Request", "detail": f"{signature}: [{errors}]"}
        text = blackbox.error_text(body, [TOKEN, PASSWORD])
        self.assertTrue(text.startswith("Field errors: Field error in object 'req'"))
        self.assertIn("on field 'name'", text)
        self.assertIn("on field 'size'", text)
        self.assertIn("[must not be null]", text)
        self.assertIn("token [redacted]", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Validation failed", text)
        self.assertNotIn("Bad Request", text)
        limit = blackbox.FIELD_ERROR_LIMIT
        long_body = {"detail": "Field error " + "z" * 1000}
        text = blackbox.error_text(long_body, [])
        self.assertEqual(len(text), len("Field errors: ") + limit + len("..."))

    def test_server_error_text_prints_class_and_title_without_secrets(self) -> None:
        body = {
            "title": f"Internal\nServer Error {TOKEN}",
            "detail": "not printed",
            "rootExceptionClassName": f"com.example.Failure{PASSWORD}",
        }
        text = blackbox.server_error_text(body, [TOKEN, PASSWORD])
        self.assertEqual(
            text,
            "Exception: com.example.Failure[redacted] "
            "Title: Internal Server Error [redacted]",
        )
        limit = blackbox.ERROR_TEXT_LIMIT
        text = blackbox.server_error_text({"title": "t" * (limit + 50)}, [])
        self.assertEqual(len(text), len("Title: ") + limit + len("..."))
        self.assertEqual(blackbox.server_error_text({"title": 7}, []), "")
        self.assertEqual(blackbox.server_error_text(None, []), "")

    def test_default_upload_config_picks_the_exact_name(self) -> None:
        configs = [
            {"id": "a", "name": "default"},
            {"id": "b", "name": "DEFAULT "},
            {"id": 7, "name": "DEFAULT"},
        ]
        self.assertEqual(blackbox.default_upload_config(page(configs)), ("7", ""))
        # A plain list is read the same way as a page's content.
        self.assertEqual(blackbox.default_upload_config(configs), ("7", ""))
        config_id, reason = blackbox.default_upload_config(None)
        self.assertIsNone(config_id)
        self.assertEqual(reason, "the body is not JSON")
        config_id, reason = blackbox.default_upload_config({"items": [], "total": 0})
        self.assertIsNone(config_id)
        self.assertEqual(
            reason, "the body is not a page with a content list. Fields: items, total"
        )
        _, reason = blackbox.default_upload_config("DEFAULT")
        self.assertEqual(
            reason,
            "the body is not a page with a content list. "
            "Fields: none, the body is a JSON str",
        )
        _, reason = blackbox.default_upload_config(
            page([{"name": "DEFAULT", "uuid": "u"}])
        )
        self.assertEqual(reason, "the DEFAULT entry has no id. Fields: name, uuid")
        _, reason = blackbox.default_upload_config(page([]))
        self.assertEqual(reason, "no entry is named DEFAULT. Entry fields: none")
        _, reason = blackbox.default_upload_config([])
        self.assertEqual(reason, "no entry is named DEFAULT. Entry fields: none")

    def test_default_upload_config_on_a_later_page_names_counts_only(self) -> None:
        first = page([{"id": "hidden-id", "name": "Images"}], last=False, total_pages=3)
        config_id, reason = blackbox.default_upload_config(first)
        self.assertIsNone(config_id)
        self.assertEqual(reason, "DEFAULT not on page 0 of 3")
        self.assertNotIn("hidden-id", reason)
        self.assertNotIn("Images", reason)
        odd = {**first, "number": "zero", "totalPages": True}
        _, reason = blackbox.default_upload_config(odd)
        self.assertEqual(reason, "DEFAULT not on page unknown of unknown")

    def test_upload_id_reads_known_keys_or_a_bare_value(self) -> None:
        self.assertEqual(blackbox.upload_id({"id": "a"}), "a")
        self.assertEqual(blackbox.upload_id({"fileId": 7}), "7")
        self.assertEqual(blackbox.upload_id("b"), "b")
        self.assertIsNone(blackbox.upload_id({"name": "c"}))
        self.assertIsNone(blackbox.upload_id({"id": ""}))
        self.assertIsNone(blackbox.upload_id(None))

    def test_media_file_path_quotes_the_id_as_one_segment(self) -> None:
        self.assertEqual(
            blackbox.media_file_path("a/b c?d"),
            "/media/management/files/a%2Fb%20c%3Fd",
        )

    def test_media_url_must_stay_on_the_media_host(self) -> None:
        default = f"https://{MEDIA_HOST}/v1"

        def settings(url: object) -> dict:
            return {"media": {"mediaBaseUrl": url}}

        own = f"https://{MEDIA_HOST}/v1/"
        self.assertEqual(blackbox.media_base_url(settings(own), MEDIA_HOST), default)
        for other in (
            "https://evil.example.com/v1",
            f"http://{MEDIA_HOST}/v1",
            f"https://{MEDIA_HOST}.evil.example.com/v1",
            f"https://{API_HOST}/v1",
            None,
        ):
            self.assertEqual(
                blackbox.media_base_url(settings(other), MEDIA_HOST), default
            )
        self.assertEqual(blackbox.media_base_url(None, MEDIA_HOST), default)


class PublicRoutingTest(unittest.TestCase):
    """The checks that talk to sockets instead of the HTTPS client."""

    def redirect_outcome(self, replies: dict[str, tuple[int, str]]) -> blackbox.Outcome:
        self.opened = []

        def connection(host: str, timeout: int) -> mock.MagicMock:
            status, location = replies[host]
            opened = mock.MagicMock()
            opened.getresponse.return_value.status = status
            opened.getresponse.return_value.getheader.return_value = location
            self.opened.append(opened)
            return opened

        with mock.patch("http.client.HTTPConnection", side_effect=connection) as made:
            outcome = blackbox.check_http_redirect(HOSTS)
        self.made = made
        return outcome

    def redirects(self) -> dict[str, tuple[int, str]]:
        return {
            host: (301, f"https://{host}:443{blackbox.REDIRECT_PATH}") for host in HOSTS
        }

    def test_each_host_redirects_to_itself(self) -> None:
        outcome = self.redirect_outcome(self.redirects())
        self.assertEqual(outcome.status, blackbox.PASS, outcome.detail)
        self.assertEqual(self.made.call_count, 3)

    def test_each_request_sends_a_user_agent(self) -> None:
        self.redirect_outcome(self.redirects())
        self.assertEqual(len(self.opened), 3)
        for opened in self.opened:
            opened.request.assert_called_once_with(
                "GET",
                blackbox.REDIRECT_PATH,
                headers={"User-Agent": blackbox.USER_AGENT},
            )

    def test_a_location_without_the_port_passes(self) -> None:
        replies = {
            host: (301, f"https://{host}{blackbox.REDIRECT_PATH}") for host in HOSTS
        }
        outcome = self.redirect_outcome(replies)
        self.assertEqual(outcome.status, blackbox.PASS, outcome.detail)

    def test_a_redirect_to_another_host_fails(self) -> None:
        replies = self.redirects()
        replies[API_HOST] = (301, f"https://{WEB_HOST}:443{blackbox.REDIRECT_PATH}")
        outcome = self.redirect_outcome(replies)
        self.assertEqual(outcome.status, blackbox.FAIL)
        self.assertIn(API_HOST, outcome.detail)

    def test_a_plain_http_answer_fails(self) -> None:
        replies = self.redirects()
        replies[WEB_HOST] = (200, None)
        outcome = self.redirect_outcome(replies)
        self.assertEqual(outcome.status, blackbox.FAIL)
        self.assertIn("returned 200", outcome.detail)

    def test_old_ports_that_refuse_or_time_out_pass(self) -> None:
        errors = [ConnectionRefusedError(61, "refused"), TimeoutError(), OSError()]
        with mock.patch("socket.create_connection", side_effect=errors) as connect:
            outcome = blackbox.check_old_ports_closed(WEB_HOST)
        self.assertEqual(outcome.status, blackbox.PASS, outcome.detail)
        self.assertEqual(
            [call.args[0] for call in connect.call_args_list],
            [(WEB_HOST, port) for port in blackbox.OLD_PORTS],
        )

    def test_an_old_port_that_connects_fails_and_is_named(self) -> None:
        def connect(address: tuple[str, int], timeout: int) -> mock.MagicMock:
            if address[1] == 8081:
                return mock.MagicMock()
            raise TimeoutError

        with mock.patch("socket.create_connection", side_effect=connect):
            outcome = blackbox.check_old_ports_closed(WEB_HOST)
        self.assertEqual(outcome.status, blackbox.FAIL)
        self.assertIn("8081", outcome.detail)


class RedirectTest(unittest.TestCase):
    def test_client_returns_a_redirect_instead_of_following_it(self) -> None:
        handler = blackbox.NoRedirect()
        request = urllib.request.Request(
            f"https://{API_HOST}/users/me", headers=blackbox.bearer(TOKEN)
        )
        followed = handler.redirect_request(
            request, None, 302, "Found", {}, "https://elsewhere.example.com/"
        )
        self.assertIsNone(followed)
        client = blackbox.Http()
        self.assertTrue(
            any(isinstance(h, blackbox.NoRedirect) for h in client.opener.handlers)
        )


class StatusOnlyTest(unittest.TestCase):
    """The status call must close the body without reading it."""

    def body(self) -> mock.MagicMock:
        fp = mock.MagicMock()
        fp.read.side_effect = AssertionError("the body was read")
        fp.closed = False
        return fp

    def test_success_body_is_never_read(self) -> None:
        response = self.body()
        response.status = 200
        response.__enter__.return_value = response
        client = blackbox.Http()
        with mock.patch.object(client.opener, "open", return_value=response) as opened:
            self.assertEqual(client.status("GET", f"https://{API_HOST}/x"), 200)
        response.read.assert_not_called()
        response.__exit__.assert_called_once()
        request = opened.call_args.args[0]
        self.assertIsNone(request.data)
        self.assertFalse(request.has_header("Authorization"))

    def test_error_body_is_never_read(self) -> None:
        fp = self.body()
        error = urllib.error.HTTPError(
            f"https://{API_HOST}/x", 403, "Forbidden", {}, fp
        )
        client = blackbox.Http()
        with mock.patch.object(client.opener, "open", side_effect=error):
            self.assertEqual(client.status("GET", f"https://{API_HOST}/x"), 403)
        fp.read.assert_not_called()
        fp.close.assert_called()


class MultipartTest(unittest.TestCase):
    def test_parts_parse_back_with_names_and_bytes(self) -> None:
        content_type, body = blackbox.encode_multipart(
            [
                ("file", "a.txt", "text/plain", b"hello\r\n"),
                ("req", None, "application/json", b"{}"),
            ]
        )
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + body
        )
        parts = list(message.iter_parts())
        self.assertEqual(
            [p.get_param("name", header="content-disposition") for p in parts],
            ["file", "req"],
        )
        self.assertEqual(parts[0].get_filename(), "a.txt")
        self.assertEqual(parts[0].get_payload(decode=True), b"hello\r\n")
        self.assertIsNone(parts[1].get_filename())
        self.assertEqual(parts[1].get_content_type(), "application/json")


class RunChecksTest(RunCase):
    def test_healthy_stack_passes_and_never_prints_secrets(self) -> None:
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 0, out)
        skipped = {"refused-address"}
        for name, status in statuses.items():
            self.assertEqual(status, "SKIP" if name in skipped else "PASS", name)
        self.assertEqual(len(statuses), 19)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.password_reads, 1)
        logins = [c for c in self.client.calls if c[1].endswith("/login")]
        self.assertEqual(len(logins), 1)

    def test_upload_req_names_a_small_test_file(self) -> None:
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 0, out)
        self.assertEqual(statuses["media-upload"], "PASS")
        name = self.client.uploaded_name
        self.assertRegex(name, r"^hume-blackbox-test-[0-9a-f]{8}\.txt$")
        self.assertEqual(
            self.client.req,
            {"name": name, "selectedUploadConfig": {"uploadConfigId": CONFIG_ID}},
        )
        self.assertLess(len(self.client.uploaded), 100)
        self.assertIn(name, out)
        self.assertNotIn(CONFIG_ID, out)
        lookups = [c for c in self.client.calls if c[1].endswith("/uploadConfigs")]
        self.assertEqual(len(lookups), 1)
        method, url, body, headers = lookups[0]
        self.assertEqual((method, body), ("GET", None))
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(
            (parts.hostname, parts.path, parts.query),
            (API_HOST, "/media/management/uploadConfigs", ""),
        )
        self.assertEqual(headers, blackbox.bearer(TOKEN))

    def test_upload_without_the_config_gets_the_media_500(self) -> None:
        # The fake media answers like the stack when the config is missing.
        self.client.routes[UPLOAD_CONFIGS] = lambda r: reply(
            200, page([{"id": "other-id", "name": "DEFAULT"}])
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-upload"], "FAIL")
        line = next(x for x in out.splitlines() if "media-upload" in x)
        self.assertIn("returned 500", line)
        self.assertIn("FileRequiresUploadConfiguration", line)
        self.assertNotIn("other-id", out)

    def test_a_bad_upload_config_answer_fails_upload_without_its_content(self) -> None:
        self.client.routes[UPLOAD_CONFIGS] = lambda r: reply(
            403, {"detail": "body never printed"}
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertIn(
            "FAIL media-upload: GET /media/management/uploadConfigs returned 403\n",
            out,
        )
        self.assertNotIn("body never printed", out)
        self.assertEqual(statuses["media-download"], "SKIP")
        self.assertEqual(statuses["media-delete"], "SKIP")
        self.assertIn("SKIP media-download: needs a successful upload", out)
        self.assertFalse([c for c in self.client.calls if c[1].endswith("/upload")])

        self.client.routes[UPLOAD_CONFIGS] = lambda r: reply(
            200, page([{"id": "secret-looking-id", "name": "Images"}])
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-upload"], "FAIL")
        self.assertIn("no entry is named DEFAULT. Entry fields: id, name", out)
        self.assertNotIn("secret-looking-id", out)
        self.assertNotIn("Images", out)
        self.assertEqual(statuses["media-download"], "SKIP")

        self.client.routes[UPLOAD_CONFIGS] = lambda r: reply(
            200, b"<html>not json</html>", "text/html"
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertIn("returned 200, but the body is not JSON", out)
        self.assertNotIn("not json", out)
        self.assertEqual(statuses["media-delete"], "SKIP")

    def test_config_id_is_redacted_from_upload_errors(self) -> None:
        self.client.routes[("POST", MEDIA_HOST, "/v1/upload")] = lambda r: reply(
            500, {**NEEDS_CONFIG, "title": f"Bad config {CONFIG_ID}"}
        )
        _, _, out = self.run_checks()
        self.assertIn("Title: Bad config [redacted]", out)
        self.assertNotIn(CONFIG_ID, out)

    def test_token_goes_only_to_the_stack_domain(self) -> None:
        self.client.routes[("GET", API_HOST, "/settings")] = lambda r: reply(
            200, {"media": {"mediaBaseUrl": "https://elsewhere.example.com/v1"}}
        )
        code, _, out = self.run_checks()
        self.assertEqual(code, 0, out)
        for _, url, _, headers in self.client.calls:
            if "Authorization" in headers:
                self.assertIn(urllib.parse.urlsplit(url).hostname, HOSTS)

    def test_rejected_upload_fails_with_status_and_field_names(self) -> None:
        self.client.routes[("POST", MEDIA_HOST, "/v1/upload")] = lambda r: reply(
            400, {"error": "Bad Request", "message": "req.secretDetail is required"}
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-upload"], "FAIL")
        self.assertIn("returned 400. Fields: error, message", out)
        self.assertNotIn("secretDetail", out)
        self.assertEqual(statuses["media-download"], "SKIP")
        self.assertEqual(statuses["media-delete"], "SKIP")

        self.client.routes[("POST", MEDIA_HOST, "/v1/upload")] = lambda r: reply(
            415, {"title": "Unsupported Media Type", "detail": "not printed"}
        )
        _, statuses, out = self.run_checks()
        self.assertEqual(statuses["media-upload"], "FAIL")
        self.assertIn("returned 415. Fields: detail, title", out)
        self.assertNotIn("not printed", out)
        self.assertNotIn("Unsupported", out)

        self.client.routes[("POST", MEDIA_HOST, "/v1/upload")] = lambda r: reply(
            500, {}
        )
        _, statuses, _ = self.run_checks()
        for name in ("media-download", "media-delete", "media-download-after-delete"):
            self.assertEqual(statuses[name], "SKIP", name)
        self.assertFalse([c for c in self.client.calls if c[0] == "DELETE"])

    def test_upload_400_prints_title_and_detail_without_secrets(self) -> None:
        self.client.routes[("POST", MEDIA_HOST, "/v1/upload")] = lambda r: reply(
            400,
            {
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": f"req.fileName must not be null. token={TOKEN} pw={PASSWORD}",
                "instance": "/v1/upload",
                "rootExceptionClassName": "ValidationException",
            },
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-upload"], "FAIL")
        line = next(x for x in out.splitlines() if "media-upload" in x)
        self.assertIn("Fields: detail, instance, rootExceptionClassName", line)
        self.assertIn("Title: Bad Request", line)
        self.assertIn("Detail: req.fileName must not be null.", line)
        self.assertIn("token=[redacted] pw=[redacted]", line)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn("ValidationException", out)

    def test_upload_5xx_prints_exception_class_and_title_without_secrets(self) -> None:
        self.client.routes[("POST", MEDIA_HOST, "/v1/upload")] = lambda r: reply(
            500,
            {
                "type": "about:blank",
                "title": "Internal Server Error",
                "status": 500,
                "detail": f"not printed token={TOKEN}",
                "instance": "/v1/upload",
                "rootExceptionClassName": f"com.example.NeedsConfig pw={PASSWORD}",
            },
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-upload"], "FAIL")
        line = next(x for x in out.splitlines() if "media-upload" in x)
        self.assertIn("returned 500. Fields: detail, instance", line)
        self.assertIn("Exception: com.example.NeedsConfig pw=[redacted]", line)
        self.assertIn("Title: Internal Server Error", line)
        self.assertNotIn("not printed", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(PASSWORD, out)
        self.assertEqual(statuses["media-download"], "SKIP")

    def test_prometheus_gets_one_untokened_get_and_403_passes(self) -> None:
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 0, out)
        self.assertEqual(statuses["api-prometheus-blocked"], "PASS")
        self.assertIn("returned 403", out)
        calls = [
            c
            for c in self.client.calls
            if urllib.parse.urlsplit(c[1]).path == blackbox.PROMETHEUS_PATH
        ]
        self.assertEqual(len(calls), 1)
        method, url, body, headers = calls[0]
        self.assertEqual((method, body), ("GET", None))
        self.assertEqual(urllib.parse.urlsplit(url).hostname, API_HOST)
        self.assertNotIn("Authorization", headers)

    def test_open_prometheus_fails_and_names_the_rule(self) -> None:
        self.client.routes[("GET", API_HOST, blackbox.PROMETHEUS_PATH)] = lambda r: (
            reply(200, b"jvm_memory_used_bytes 123")
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["api-prometheus-blocked"], "FAIL")
        line = next(x for x in out.splitlines() if "api-prometheus-blocked" in x)
        self.assertIn("returned 200, expected 403", line)
        self.assertIn("expected on stacks without ApiPrometheusBlockRule", line)
        self.assertNotIn("jvm_memory", out)

    def test_changed_download_fails(self) -> None:
        self.client.routes[("GET", MEDIA_HOST, "/v1/download")] = lambda r: (
            reply(200, b"other bytes", "application/octet-stream")
            if FakeHttp.signed_in(r[2])
            else reply(401, b"")
        )
        code, statuses, _ = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-download"], "FAIL")
        # The test file is still deleted, so it does not stay in storage.
        self.assertEqual(statuses["media-delete"], "PASS")

    def test_delete_removes_the_test_file_and_download_then_fails(self) -> None:
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 0, out)
        self.assertEqual(statuses["media-delete"], "PASS")
        self.assertEqual(statuses["media-download-after-delete"], "PASS")
        self.assertIn("with hardDelete returned 204", out)
        self.assertIn("GET /download after the delete returned 404", out)
        self.assertTrue(self.client.deleted)
        deletes = [c for c in self.client.calls if c[0] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        _, url, body, headers = deletes[0]
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.hostname, API_HOST)
        self.assertEqual(parts.path, DELETE_FILE[2])
        self.assertEqual(json.loads(body), {"hardDelete": True})
        self.assertEqual(headers, blackbox.json_headers(TOKEN))
        self.assertNotIn(FILE_ID, out)
        self.assertNotIn("file%2Fid", out)

    def test_refused_delete_fails_and_skips_the_after_delete_check(self) -> None:
        self.client.routes[DELETE_FILE] = lambda r: reply(
            403, {"title": "Forbidden", "detail": f"no right on {FILE_ID}"}
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        line = next(x for x in out.splitlines() if "media-delete" in x)
        self.assertIn("returned 403. Fields: detail, title", line)
        self.assertIn("The test file stays in media storage", line)
        self.assertNotIn(FILE_ID, out)
        self.assertEqual(statuses["media-download-after-delete"], "SKIP")
        self.assertIn(
            "SKIP media-download-after-delete: needs a successful delete", out
        )

    def test_delete_5xx_prints_the_cause_with_the_id_redacted(self) -> None:
        self.client.routes[DELETE_FILE] = lambda r: reply(
            500,
            {
                "title": f"Cannot delete {FILE_ID}",
                "detail": "not printed",
                "rootExceptionClassName": "com.example.FileIsUploading",
            },
        )
        _, statuses, out = self.run_checks()
        self.assertEqual(statuses["media-delete"], "FAIL")
        self.assertIn("Exception: com.example.FileIsUploading", out)
        self.assertIn("Title: Cannot delete [redacted]", out)
        self.assertNotIn(FILE_ID, out)
        self.assertNotIn("not printed", out)

    def test_file_still_served_after_delete_fails(self) -> None:
        self.client.routes[DELETE_FILE] = lambda r: reply(204, b"")
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-delete"], "PASS")
        self.assertEqual(statuses["media-download-after-delete"], "FAIL")
        self.assertIn("after the delete returned 200 with 53 bytes", out)

    def test_failed_sign_in_skips_the_signed_in_checks(self) -> None:
        code, statuses, out = self.run_checks(lambda: "wrong-password")
        self.assertEqual(code, 1)
        self.assertEqual(statuses["sign-in"], "FAIL")
        self.assertIn("POST /login returned 401", out)
        for name in blackbox.SIGNED_IN_CHECKS:
            self.assertEqual(statuses[name], "SKIP", name)
        self.assertEqual(len([c for c in self.client.calls if "/login" in c[1]]), 1)

    def test_unreadable_secret_fails_without_the_error_text(self) -> None:
        def denied() -> str:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "detail"}},
                "GetSecretValue",
            )

        code, statuses, out = self.run_checks(denied)
        self.assertEqual(code, 1)
        self.assertEqual(statuses["sign-in"], "FAIL")
        self.assertIn("ClientError", out)
        self.assertNotIn("detail", out)
        self.assertFalse([c for c in self.client.calls if "/login" in c[1]])

    def test_oidc_stack_signs_in_through_cognito_not_login(self) -> None:
        requests = []

        def request_token(username: str, password: str) -> str:
            requests.append((username, password))
            return TOKEN

        def users_me(request) -> blackbox.Response:
            if not self.client.signed_in(request[2]):
                return reply(401, b"")
            return reply(200, {"username": "hume-admin"})

        self.client.routes[("GET", API_HOST, "/users/me")] = users_me
        code, statuses, out = self.run_checks(
            target=OIDC_TARGET, request_token=request_token
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(statuses["sign-in"], "PASS")
        self.assertIn("Cognito returned an access token for hume-admin", out)
        for name in blackbox.SIGNED_IN_CHECKS:
            self.assertEqual(statuses[name], "PASS", name)
        self.assertEqual(requests, [("hume-admin", PASSWORD)])
        self.assertFalse([c for c in self.client.calls if c[1].endswith("/login")])
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(TOKEN, out)

    def test_cognito_refusal_fails_with_the_code_only(self) -> None:
        def refused(username: str, password: str) -> str:
            raise ClientError(
                {"Error": {"Code": "NotAuthorizedException", "Message": "detail"}},
                "AdminInitiateAuth",
            )

        code, statuses, out = self.run_checks(target=OIDC_TARGET, request_token=refused)
        self.assertEqual(code, 1)
        self.assertEqual(statuses["sign-in"], "FAIL")
        self.assertIn("NotAuthorizedException", out)
        self.assertNotIn("detail", out)
        for name in blackbox.SIGNED_IN_CHECKS:
            self.assertEqual(statuses[name], "SKIP", name)
        self.assertFalse([c for c in self.client.calls if c[1].endswith("/login")])

    def test_oidc_stack_without_a_cognito_sign_in_fails(self) -> None:
        code, statuses, out = self.run_checks(target=OIDC_TARGET)
        self.assertEqual(code, 1)
        self.assertEqual(statuses["sign-in"], "FAIL")
        self.assertIn("no Cognito sign-in", out)

    def test_unknown_host_gets_the_fixed_403(self) -> None:
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 0, out)
        self.assertEqual(statuses["alb-unknown-host"], "PASS")
        calls = [
            c for c in self.client.calls if c[3].get("Host") == blackbox.UNKNOWN_HOST
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(urllib.parse.urlsplit(calls[0][1]).hostname, WEB_HOST)
        self.assertNotIn("Authorization", calls[0][3])

    def test_unknown_host_that_reaches_a_service_fails(self) -> None:
        self.client.routes[("GET", blackbox.UNKNOWN_HOST, "/")] = lambda r: reply(
            200, b"<html></html>", "text/html"
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["alb-unknown-host"], "FAIL")
        self.assertIn("got 200 as text/html", out)

    def test_the_new_public_checks_run_and_report(self) -> None:
        _, statuses, _ = self.run_checks()
        self.assertEqual(statuses["http-redirect"], "PASS")
        self.assertEqual(statuses["old-ports-closed"], "PASS")

    def test_media_401_without_a_token_passes_routing(self) -> None:
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 0, out)
        self.assertEqual(statuses["media-routing"], "PASS")
        self.assertIn("returned 401, so media answered it", out)

    def test_a_media_403_from_the_service_or_the_alb_fails_routing(self) -> None:
        self.client.routes[("GET", MEDIA_HOST, "/v1/download")] = lambda r: reply(
            403, {"error": "Forbidden"}
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-routing"], "FAIL")
        self.assertIn("/v1/download returned 403", out)

        self.client.routes[("GET", MEDIA_HOST, "/v1/download")] = lambda r: reply(
            403, b"Forbidden"
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["media-routing"], "FAIL")
        self.assertIn("load balancer's fixed 403", out)

    def test_network_error_fails_one_check_and_the_run_goes_on(self) -> None:
        def refused(request):
            raise ConnectionRefusedError(61, "Connection refused")

        self.client.routes[("GET", WEB_HOST, "/")] = refused
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["web-index"], "FAIL")
        self.assertIn("ConnectionRefusedError", out)
        self.assertEqual(statuses["api-unauthenticated"], "PASS")
        self.assertEqual(statuses["media-download"], "PASS")

    def test_wrong_user_fails_users_me(self) -> None:
        self.client.routes[("GET", API_HOST, "/users/me")] = lambda r: reply(
            200, {"username": "someone-else"}
        )
        code, statuses, out = self.run_checks()
        self.assertEqual(code, 1)
        self.assertEqual(statuses["users-me"], "FAIL")
        self.assertIn("Fields: username", out)
        self.assertNotIn("someone-else", out)


class LoadTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in self.tmp.iterdir()])
        patcher = mock.patch.object(deploy, "DEPLOY_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        session = mock.patch.object(blackbox, "make_session", return_value="session")
        session.start()
        self.addCleanup(session.stop)

    def args(self, stack: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            stack=stack,
            profile=None,
            region=None,
            idp_name="hume-test-idp",
            user="hume-admin",
        )

    def test_reads_hosts_user_and_secret_from_the_only_record(self) -> None:
        (self.tmp / "hume-ecs-1.txt").write_text(
            "StackName = hume-ecs-1\nBaseDomainName = a.example.com\n"
            "AdminUsername = admin\n"
            "AdminPasswordSecretArn = arn:aws:secretsmanager:x:1:secret:hume/p-AbCdEf\n"
        )
        target, session = blackbox.load_target(self.args())
        self.assertEqual(target.hosts, deploy.hume_hosts("a.example.com"))
        self.assertEqual(
            target.password_secret, "arn:aws:secretsmanager:x:1:secret:hume/p-AbCdEf"
        )
        self.assertEqual(session, "session")

    def test_named_stack_without_outputs_exits(self) -> None:
        (self.tmp / "hume-ecs-1.txt").write_text("StackName = hume-ecs-1\n")
        (self.tmp / "hume-ecs-2.txt").write_text("StackName = hume-ecs-2\n")
        with self.assertRaises(SystemExit) as raised:
            blackbox.load_target(self.args())
        self.assertIn("Pass the stack name", str(raised.exception))
        with self.assertRaises(SystemExit) as raised:
            blackbox.load_target(self.args("hume-ecs-2"))
        self.assertIn("no BaseDomainName", str(raised.exception))

    def test_secret_name_defaults_to_the_stack_prefix(self) -> None:
        (self.tmp / "hume-ecs-3.txt").write_text("BaseDomainName = b.example.com\n")
        target, _ = blackbox.load_target(self.args("hume-ecs-3"))
        self.assertEqual(target.password_secret, "hume/hume-ecs-3/admin-password")
        self.assertEqual(target.username, "admin")
        self.assertEqual(target.auth_provider, deploy.NATIVE_PROVIDER)

    def test_oidc_record_uses_the_cognito_user_and_secret(self) -> None:
        (self.tmp / "hume-ecs-4.txt").write_text(
            "BaseDomainName = c.example.com\nAuthProvider = Oidc\n"
        )
        target, _ = blackbox.load_target(self.args("hume-ecs-4"))
        self.assertEqual(target.auth_provider, deploy.OIDC_PROVIDER)
        self.assertEqual(target.username, "hume-admin")
        self.assertEqual(
            target.password_secret, "hume/hume-test-idp/cognito/hume-admin"
        )


if __name__ == "__main__":
    unittest.main()
