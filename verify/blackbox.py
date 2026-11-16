#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Check a deployed Hume stack from the outside, as a user would.

Prints one PASS, FAIL or SKIP line per check, then exits 1 if any check
failed. Run it from an address on the stack allowlist.

    uv run verify/blackbox.py --stack <stack name>

On a Native stack, the script signs in with POST /login as the administrator.
On an Oidc stack, it asks the Cognito test pool for an access token for one
user, hume-admin by default. The AWS calls read that password from Secrets
Manager and, on an Oidc stack, ask Cognito for the token. The password and the
token stay in memory. Neither is printed, logged or written to a file. The token
goes only to the stack's own hosts, and only over HTTPS. The one plain HTTP call
checks the port 80 redirect and carries no token.
"""

import argparse
import contextlib
import http.client
import http.cookiejar
import json
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# The repository modules imported below live in these directories.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_IMPORT_DIRS = [str(_REPO_ROOT / name) for name in ("stack",)]
sys.path[:0] = [entry for entry in _IMPORT_DIRS if entry not in sys.path]

import cognito
from deploy import (
    DEFAULT_PROFILE,
    DEFAULT_REGION,
    NATIVE_PROVIDER,
    OIDC_PROVIDER,
    HumeHosts,
    hume_hosts,
    make_session,
    newest_stack,
    read_record,
)

HTTPS_PORT = 443
# The listener ports before the move to 443. Nothing may answer on them.
OLD_PORTS = (8080, 8081, 8009)
OLD_PORT_TIMEOUT_SECONDS = 5
TIMEOUT_SECONDS = 20
# The fixed response of the HTTPS listener's default action in the template.
ALB_FORBIDDEN_BODY = b"Forbidden"
NOT_MEDIA_PATH = "/blackbox-not-a-media-path"
# No rule names this host, so the listener's default action must answer it.
UNKNOWN_HOST = "blackbox-unknown-host.invalid"
REDIRECT_PATH = "/blackbox-redirect?check=1"
# The WAF common rule set blocks requests with no User-Agent. This is urllib's value.
USER_AGENT = f"Python-urllib/{urllib.request.__version__}"
PROMETHEUS_PATH = "/actuator/prometheus"
# Media refuses an upload without an upload configuration. Its ID is generated
# per stack, so the check looks it up by name.
UPLOAD_CONFIGS_PATH = "/media/management/uploadConfigs"
DEFAULT_UPLOAD_CONFIG = "DEFAULT"
MEDIA_FILES_PATH = "/media/management/files"
# Keys the upload response may use for the new file's ID.
UPLOAD_ID_KEYS = ("id", "fileId", "mediaId")
# Longest title or detail printed from a rejected upload.
ERROR_TEXT_LIMIT = 300
FIELD_ERROR_MARKER = "Field error"
FIELD_ERROR_LIMIT = 600
SIGNED_IN_CHECKS = (
    "users-me",
    "settings",
    "info",
    "media-init",
    "media-upload",
    "media-download",
    "media-delete",
    "media-download-after-delete",
)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes

    def json(self) -> object | None:
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return a redirect as the response. Following it would resend the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


class Http:
    """HTTPS client that verifies certificates and keeps cookies between calls."""

    def __init__(self, timeout: int = TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            urllib.request.HTTPCookieProcessor(self.cookies),
            NoRedirect(),
        )

    def call(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        request = urllib.request.Request(
            url, data=body, method=method, headers=headers or {}
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as reply:
                return Response(
                    reply.status, reply.headers.get_content_type(), reply.read()
                )
        except urllib.error.HTTPError as err:
            with err:
                return Response(err.code, err.headers.get_content_type(), err.read())

    def status(self, method: str, url: str) -> int:
        """Return only the status code. The body is closed without being read."""
        request = urllib.request.Request(url, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as reply:
                return reply.status
        except urllib.error.HTTPError as err:
            with err:
                return err.code

    def cookie_names(self, domain: str) -> list[str]:
        return sorted({c.name for c in self.cookies if c.domain.lstrip(".") == domain})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def json_headers(token: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    return {**headers, **bearer(token)} if token else headers


def encode_multipart(
    parts: list[tuple[str, str | None, str, bytes]],
) -> tuple[str, bytes]:
    """Encode (field name, file name, content type, data) parts as form data."""
    boundary = f"blackbox-{secrets.token_hex(12)}"
    chunks = []
    for name, file_name, content_type, data in parts:
        disposition = f'form-data; name="{name}"'
        if file_name:
            disposition += f'; filename="{file_name}"'
        chunks += [
            f"--{boundary}\r\n".encode(),
            f"Content-Disposition: {disposition}\r\n".encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


# ---------------------------------------------------------------------------
# Response checks
# ---------------------------------------------------------------------------


def field_names(value: object) -> str:
    """Name the top-level fields of a JSON response, never their values."""
    if isinstance(value, dict):
        return ", ".join(sorted(value)) or "none"
    if value is None:
        return "none, the body is not JSON"
    return f"none, the body is a JSON {type(value).__name__}"


def redact(value: str, hidden: list[str]) -> str:
    for secret in hidden:
        if secret:
            value = value.replace(secret, "[redacted]")
    return " ".join(value.split())


def cut(value: str, limit: int) -> str:
    return value[:limit] + "..." if len(value) > limit else value


def error_text(body: object, hidden: list[str]) -> str:
    """Return the title and detail of an error response, with secrets removed.

    Each value in hidden is replaced before the text is cut, so a cut cannot
    leave part of a secret behind.
    """
    if not isinstance(body, dict):
        return ""
    # Spring lists the field errors after the method signature. When they are
    # there, print only them, so the field names fit inside the limit.
    detail = body.get("detail")
    if isinstance(detail, str) and FIELD_ERROR_MARKER in (
        detail := redact(detail, hidden)
    ):
        errors = detail[detail.index(FIELD_ERROR_MARKER) :]
        return f"Field errors: {cut(errors, FIELD_ERROR_LIMIT)}"
    parts = []
    for key in ("title", "detail"):
        value = body.get(key)
        if isinstance(value, str):
            text = cut(redact(value, hidden), ERROR_TEXT_LIMIT)
            parts.append(f"{key.capitalize()}: {text}")
    return " ".join(parts)


def server_error_text(body: object, hidden: list[str]) -> str:
    """Return the exception class and title of a 5xx response, with secrets removed.

    The detail is left out, because a server error's detail can hold request data.
    """
    if not isinstance(body, dict):
        return ""
    parts = []
    for key, label in (("rootExceptionClassName", "Exception"), ("title", "Title")):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(f"{label}: {cut(redact(value, hidden), ERROR_TEXT_LIMIT)}")
    return " ".join(parts)


def failure_detail(status: int, body: object, hidden: list[str]) -> str:
    """Describe a failed media response with field names and safe error text.

    A 400 names what media expects, so its title and detail are added. A 5xx
    adds its exception class and title. Every other failure names fields only.
    """
    if status == 400:
        reason = error_text(body, hidden)
    elif status >= 500:
        reason = server_error_text(body, hidden)
    else:
        reason = ""
    return f"Fields: {field_names(body)}" + (f". {reason}" if reason else "")


def is_alb_forbidden(response: Response) -> bool:
    return (
        response.status == 403
        and response.content_type == "text/plain"
        and response.body.strip() == ALB_FORBIDDEN_BODY
    )


def fields_equal_to(body: object, value: str) -> list[str]:
    if not isinstance(body, dict):
        return []
    return sorted(key for key, item in body.items() if item == value)


def upload_id(body: object) -> str | None:
    if isinstance(body, dict):
        for key in UPLOAD_ID_KEYS:
            if isinstance(body.get(key), str | int) and str(body[key]):
                return str(body[key])
        return None
    if isinstance(body, str | int) and str(body):
        return str(body)
    return None


def default_upload_config(body: object) -> tuple[str | None, str]:
    """Return the ID of the upload configuration named DEFAULT, or None and why.

    The API returns a Spring page with the entries in content. A plain list is
    read the same way. The reason names fields and page counts only, never values.
    """
    if body is None:
        return None, "the body is not JSON"
    page = body if isinstance(body, dict) else {}
    content = page.get("content") if page else body
    if not isinstance(content, list):
        return None, (
            f"the body is not a page with a content list. Fields: {field_names(body)}"
        )
    entries = [entry for entry in content if isinstance(entry, dict)]
    default = next(
        (entry for entry in entries if entry.get("name") == DEFAULT_UPLOAD_CONFIG),
        None,
    )
    if default is None and page.get("last") is False:
        # The script asks for the first page only. Spring numbers pages from 0.
        def count(key: str) -> str:
            value = page.get(key)
            is_count = isinstance(value, int) and not isinstance(value, bool)
            return str(value) if is_count else "unknown"

        return None, (
            f"{DEFAULT_UPLOAD_CONFIG} not on page {count('number')} of "
            f"{count('totalPages')}"
        )
    if default is None:
        keys = ", ".join(sorted({key for entry in entries for key in entry}))
        return None, (
            f"no entry is named {DEFAULT_UPLOAD_CONFIG}. Entry fields: {keys or 'none'}"
        )
    config_id = default.get("id")
    if not isinstance(config_id, str | int) or not str(config_id):
        return None, (
            f"the {DEFAULT_UPLOAD_CONFIG} entry has no id. "
            f"Fields: {field_names(default)}"
        )
    return str(config_id), ""


def media_file_path(file_id: str) -> str:
    """Return the API path of one media file, with the ID quoted as one segment."""
    return f"{MEDIA_FILES_PATH}/{urllib.parse.quote(file_id, safe='')}"


def media_base_url(settings: object, media_host: str) -> str:
    """Use the media URL from /settings only if it is on the stack's media host."""
    default = f"https://{media_host}/v1"
    media = settings.get("media") if isinstance(settings, dict) else None
    url = media.get("mediaBaseUrl") if isinstance(media, dict) else None
    if not isinstance(url, str):
        return default
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != media_host:
        return default
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    status: str
    detail: str
    value: object = None


@dataclass
class Report:
    statuses: list[str] = field(default_factory=list)

    def add(self, name: str, outcome: Outcome) -> Outcome:
        self.statuses.append(outcome.status)
        print(f"{outcome.status} {name}: {outcome.detail}", flush=True)
        return outcome

    def check(self, name: str, run: Callable[[], Outcome]) -> Outcome:
        """Run one check. A network or TLS error fails it, and the run goes on."""
        try:
            outcome = run()
        except (OSError, http.client.HTTPException) as err:
            outcome = Outcome(FAIL, f"{type(err).__name__}: {err}")
        return self.add(name, outcome)

    def skip(self, name: str, reason: str) -> None:
        self.add(name, Outcome(SKIP, reason))

    @property
    def exit_code(self) -> int:
        return 1 if FAIL in self.statuses else 0


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@dataclass
class Target:
    stack: str
    hosts: HumeHosts
    username: str
    password_secret: str
    auth_provider: str = NATIVE_PROVIDER

    def web(self, path: str) -> str:
        return f"https://{self.hosts.web}{path}"

    def api(self, path: str) -> str:
        return f"https://{self.hosts.api}{path}"

    def media(self, path: str) -> str:
        return f"https://{self.hosts.media}{path}"


def check_dns_tls(hosts: HumeHosts) -> Outcome:
    context = ssl.create_default_context()
    expiries = []
    for host in hosts:
        socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        with (
            socket.create_connection(
                (host, HTTPS_PORT), timeout=TIMEOUT_SECONDS
            ) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            expiries.append(ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"]))
    until = time.strftime("%Y-%m-%d", time.gmtime(min(expiries)))
    return Outcome(
        PASS,
        f"{', '.join(hosts)} resolve, and the certificate is valid for each name "
        f"on port {HTTPS_PORT} until {until}.",
    )


def check_http_redirect(hosts: HumeHosts) -> Outcome:
    """Each host answers plain HTTP with a 301 to itself, and with nothing else."""
    for host in hosts:
        with contextlib.closing(
            http.client.HTTPConnection(host, timeout=TIMEOUT_SECONDS)
        ) as connection:
            connection.request("GET", REDIRECT_PATH, headers={"User-Agent": USER_AGENT})
            reply = connection.getresponse()
            location = reply.getheader("Location")
        wanted = (
            f"https://{host}:{HTTPS_PORT}{REDIRECT_PATH}",
            f"https://{host}{REDIRECT_PATH}",
        )
        if reply.status != 301 or location not in wanted:
            return Outcome(FAIL, f"http://{host} returned {reply.status} to {location}")
    return Outcome(PASS, "Each host redirects plain HTTP to itself with a 301")


def check_old_ports_closed(host: str) -> Outcome:
    """The old listener ports refuse the connection or never answer."""
    still_open = []
    for port in OLD_PORTS:
        with contextlib.suppress(OSError):
            address = (host, port)
            with socket.create_connection(address, OLD_PORT_TIMEOUT_SECONDS):
                still_open.append(port)
    if still_open:
        return Outcome(FAIL, f"Ports {still_open} accept connections on {host}")
    return Outcome(PASS, f"Ports {list(OLD_PORTS)} do not answer on {host}")


def check_public(report: Report, client: Http, target: Target) -> None:
    def web_index() -> Outcome:
        response = client.call("GET", target.web("/"))
        if response.status == 200 and response.content_type == "text/html":
            return Outcome(PASS, "/ returned 200 with an HTML page")
        return Outcome(FAIL, f"/ returned {response.status} as {response.content_type}")

    def unauthenticated() -> Outcome:
        response = client.call("GET", target.api("/users/me"))
        if response.status == 401:
            return Outcome(PASS, "/users/me without a token returned 401")
        return Outcome(FAIL, f"/users/me without a token returned {response.status}")

    def prometheus_blocked() -> Outcome:
        # No token, and only the status is read. The metrics body is never
        # read, stored or printed.
        status = client.status("GET", target.api(PROMETHEUS_PATH))
        if status == 403:
            return Outcome(PASS, f"GET {PROMETHEUS_PATH} without a token returned 403")
        return Outcome(
            FAIL,
            f"GET {PROMETHEUS_PATH} returned {status}, expected 403. This is "
            "expected on stacks without ApiPrometheusBlockRule",
        )

    def media_routing() -> Outcome:
        # Media answers a request without a token with 401 and a Bearer
        # challenge. The load balancer's fixed 403 means no rule sent the
        # request to media.
        response = client.call("GET", target.media("/v1/download"))
        if response.status == 401:
            return Outcome(
                PASS, "/v1/download without a token returned 401, so media answered it"
            )
        if is_alb_forbidden(response):
            return Outcome(FAIL, "/v1/download returned the load balancer's fixed 403")
        return Outcome(FAIL, f"/v1/download returned {response.status}")

    def alb_default() -> Outcome:
        response = client.call("GET", target.media(NOT_MEDIA_PATH))
        if is_alb_forbidden(response):
            return Outcome(
                PASS,
                f"{NOT_MEDIA_PATH} on the media host returned the load balancer's "
                "fixed 403",
            )
        return Outcome(
            FAIL,
            f"{NOT_MEDIA_PATH} on the media host returned {response.status} as "
            f"{response.content_type}",
        )

    def alb_unknown_host() -> Outcome:
        # The certificate check still uses the web name. Only the Host header
        # names a host that no rule matches.
        response = client.call("GET", target.web("/"), None, {"Host": UNKNOWN_HOST})
        if is_alb_forbidden(response):
            return Outcome(PASS, "An unknown host got the load balancer's fixed 403")
        return Outcome(
            FAIL,
            f"An unknown host got {response.status} as {response.content_type}",
        )

    report.check("dns-tls", lambda: check_dns_tls(target.hosts))
    report.check("web-index", web_index)
    report.check("api-unauthenticated", unauthenticated)
    report.check("api-prometheus-blocked", prometheus_blocked)
    report.check("media-routing", media_routing)
    report.check("alb-default", alb_default)
    report.check("alb-unknown-host", alb_unknown_host)
    report.check("http-redirect", lambda: check_http_redirect(target.hosts))
    report.check("old-ports-closed", lambda: check_old_ports_closed(target.hosts.web))


def sign_in(
    report: Report,
    client: Http,
    target: Target,
    read_password: Callable[[], str],
    hidden: list[str],
    request_token: Callable[[str, str], str] | None = None,
) -> str | None:
    """Sign in once. Return the token, or None if sign-in failed.

    A Native stack gets POST /login. An Oidc stack gets an access token from
    request_token, which takes the user name and the password. The password and
    the token are added to hidden, so that error text printed later can have
    them removed.
    """

    def run() -> Outcome:
        try:
            password = read_password()
        except (ClientError, BotoCoreError) as err:
            return Outcome(
                FAIL, f"could not read the password secret: {type(err).__name__}"
            )
        hidden.append(password)
        if target.auth_provider == OIDC_PROVIDER:
            return cognito_sign_in(target, password, request_token, hidden)
        body = json.dumps({"username": target.username, "password": password})
        response = client.call(
            "POST", target.api("/login"), body.encode(), json_headers()
        )
        del body
        payload = response.json()
        token = payload.get("token") if isinstance(payload, dict) else None
        if response.status == 200 and isinstance(token, str) and token:
            hidden.append(token)
            return Outcome(
                PASS,
                f"POST /login as {target.username} returned 200 and a token",
                token,
            )
        return Outcome(
            FAIL,
            f"POST /login returned {response.status}. Fields: {field_names(payload)}",
        )

    return report.check("sign-in", run).value


def cognito_sign_in(
    target: Target,
    password: str,
    request_token: Callable[[str, str], str] | None,
    hidden: list[str],
) -> Outcome:
    if request_token is None:
        return Outcome(FAIL, "the stack uses Oidc, but no Cognito sign-in is set up")
    try:
        token = request_token(target.username, password)
    except ClientError as err:
        # The error code names the reason, such as NotAuthorizedException. The
        # message is left out.
        code = err.response.get("Error", {}).get("Code", "unknown")
        return Outcome(FAIL, f"Cognito refused {target.username}: {code}")
    except BotoCoreError as err:
        return Outcome(FAIL, f"could not reach Cognito: {type(err).__name__}")
    hidden.append(token)
    return Outcome(
        PASS, f"Cognito returned an access token for {target.username}", token
    )


def check_signed_in(report: Report, client: Http, target: Target, token: str) -> str:
    """Run the signed-in reads. Return the media base URL to use."""

    def users_me() -> Outcome:
        response = client.call("GET", target.api("/users/me"), None, bearer(token))
        matches = fields_equal_to(response.json(), target.username)
        if response.status == 200 and matches:
            return Outcome(
                PASS, f"/users/me returned 200. Field {matches[0]} is {target.username}"
            )
        return Outcome(
            FAIL,
            f"/users/me returned {response.status}. No field is {target.username}. "
            f"Fields: {field_names(response.json())}",
        )

    def settings() -> Outcome:
        response = client.call("GET", target.api("/settings"), None, bearer(token))
        body = response.json()
        if response.status != 200 or not isinstance(body, dict):
            return Outcome(
                FAIL,
                f"/settings returned {response.status}. Fields: {field_names(body)}",
            )
        url = media_base_url(body, target.hosts.media)
        return Outcome(PASS, f"/settings returned 200. Media calls go to {url}", url)

    def info() -> Outcome:
        response = client.call("GET", target.api("/info"), None, bearer(token))
        body = response.json()
        status = PASS if response.status == 200 else FAIL
        return Outcome(
            status, f"/info returned {response.status}. Fields: {field_names(body)}"
        )

    report.check("users-me", users_me)
    media = report.check("settings", settings).value
    report.check("info", info)
    return media or media_base_url(None, target.hosts.media)


def check_media(
    report: Report,
    client: Http,
    target: Target,
    media: str,
    token: str,
    hidden: list[str],
) -> None:
    file_name = f"hume-blackbox-test-{secrets.token_hex(4)}.txt"
    content = f"Hume black box test {secrets.token_hex(16)}\n".encode()

    def init() -> Outcome:
        body = json.dumps(
            {
                "externalDomain": target.hosts.media,
                "externalPath": "/v1/download",
                "secure": True,
                "validityMinutes": 60,
            }
        )
        response = client.call(
            "POST", f"{media}/download/init", body.encode(), json_headers(token)
        )
        names = client.cookie_names(target.hosts.media)
        status = PASS if 200 <= response.status < 300 else FAIL
        return Outcome(
            status,
            f"POST /download/init returned {response.status}. "
            f"Cookies set: {', '.join(names) or 'none'}",
        )

    def upload() -> Outcome:
        configs = client.call(
            "GET", target.api(UPLOAD_CONFIGS_PATH), None, bearer(token)
        )
        if configs.status != 200:
            return Outcome(FAIL, f"GET {UPLOAD_CONFIGS_PATH} returned {configs.status}")
        config_id, reason = default_upload_config(configs.json())
        if config_id is None:
            return Outcome(
                FAIL, f"GET {UPLOAD_CONFIGS_PATH} returned 200, but {reason}"
            )
        # The ID is never printed, so remove it from any error text too.
        hidden.append(config_id)
        # Media rejects a req without a name (NotNull and NotBlank on req.name),
        # and rejects a file without an upload configuration.
        req = json.dumps(
            {"name": file_name, "selectedUploadConfig": {"uploadConfigId": config_id}}
        ).encode()
        content_type, body = encode_multipart(
            [
                ("file", file_name, "text/plain", content),
                ("req", None, "application/json", req),
            ]
        )
        response = client.call(
            "POST",
            f"{media}/upload",
            body,
            {"Content-Type": content_type, **bearer(token)},
        )
        payload = response.json()
        if not 200 <= response.status < 300:
            return Outcome(
                FAIL,
                f"POST /upload returned {response.status}. "
                f"{failure_detail(response.status, payload, hidden)}",
            )
        file_id = upload_id(payload)
        if file_id is None:
            return Outcome(
                FAIL,
                f"POST /upload returned {response.status} but no file ID. "
                f"Fields: {field_names(payload)}",
            )
        # The ID is never printed, so remove it from any error text too.
        hidden.append(file_id)
        return Outcome(
            PASS,
            f"POST /upload of {file_name} returned {response.status} and an ID",
            file_id,
        )

    def download_url(file_id: str) -> str:
        query = urllib.parse.urlencode({"id": file_id, "download": "true"})
        return f"{media}/download?{query}"

    def download(file_id: str) -> Outcome:
        response = client.call("GET", download_url(file_id), None, bearer(token))
        if response.status == 200 and response.body == content:
            return Outcome(
                PASS,
                f"GET /download returned the {len(content)} bytes uploaded, unchanged",
            )
        return Outcome(
            FAIL,
            f"GET /download returned {response.status} with "
            f"{len(response.body)} bytes. "
            f"Expected {len(content)} bytes that match the upload",
        )

    def delete(file_id: str) -> Outcome:
        # Media removes the file's bytes only when hardDelete is true.
        body = json.dumps({"hardDelete": True}).encode()
        response = client.call(
            "DELETE",
            target.api(media_file_path(file_id)),
            body,
            json_headers(token),
        )
        if response.status == 204:
            return Outcome(
                PASS, f"DELETE of {file_name} with hardDelete returned 204", True
            )
        return Outcome(
            FAIL,
            f"DELETE of {file_name} returned {response.status}. "
            f"{failure_detail(response.status, response.json(), hidden)}. "
            "The test file stays in media storage",
        )

    def gone(file_id: str) -> Outcome:
        response = client.call("GET", download_url(file_id), None, bearer(token))
        if response.status >= 400:
            return Outcome(
                PASS, f"GET /download after the delete returned {response.status}"
            )
        return Outcome(
            FAIL,
            f"GET /download after the delete returned {response.status} with "
            f"{len(response.body)} bytes. Expected the file to be gone",
        )

    report.check("media-init", init)
    file_id = report.check("media-upload", upload).value
    if file_id is None:
        for name in ("media-download", "media-delete", "media-download-after-delete"):
            report.skip(name, "needs a successful upload")
        return
    report.check("media-download", lambda: download(file_id))
    # Delete even if the download failed, so the test file does not stay.
    if not report.check("media-delete", lambda: delete(file_id)).value:
        report.skip("media-download-after-delete", "needs a successful delete")
        return
    report.check("media-download-after-delete", lambda: gone(file_id))


def run_checks(
    target: Target,
    client: Http,
    read_password: Callable[[], str],
    request_token: Callable[[str, str], str] | None = None,
) -> int:
    report = Report()
    hidden: list[str] = []
    check_public(report, client, target)
    token = sign_in(report, client, target, read_password, hidden, request_token)
    if token is None:
        for name in SIGNED_IN_CHECKS:
            report.skip(name, "needs a successful sign-in")
    else:
        media = check_signed_in(report, client, target, token)
        check_media(report, client, target, media, token, hidden)
    report.skip(
        "refused-address",
        "this machine is on the allowlist, so a refused address cannot be tested "
        "from here",
    )
    return report.exit_code


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def load_target(args: argparse.Namespace) -> tuple[Target, boto3.Session]:
    name = args.stack or newest_stack()
    record = read_record(name)
    if "BaseDomainName" not in record:
        sys.exit(
            f"The record for {name} has no BaseDomainName. Is the stack fully created?"
        )
    # A record without AuthProvider comes from a stack with the native login.
    provider = record.get("AuthProvider", NATIVE_PROVIDER)
    if provider == OIDC_PROVIDER:
        username = args.user
        password_secret = cognito.secret_name(args.idp_name, args.user)
    else:
        username = record.get("AdminUsername", "admin")
        password_secret = record.get(
            "AdminPasswordSecretArn", f"hume/{name}/admin-password"
        )
    target = Target(
        stack=name,
        hosts=hume_hosts(record["BaseDomainName"]),
        username=username,
        password_secret=password_secret,
        auth_provider=provider,
    )
    return target, make_session(args, record)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stack", help="Stack name. Default: the only record")
    parser.add_argument("--profile", help=f"AWS profile (default: {DEFAULT_PROFILE})")
    parser.add_argument("--region", help=f"AWS region (default: {DEFAULT_REGION})")
    parser.add_argument(
        "--idp-name",
        default=cognito.DEFAULT_NAME,
        help=f"Cognito test pool for an Oidc stack (default: {cognito.DEFAULT_NAME})",
    )
    parser.add_argument(
        "--user",
        default=cognito.ADMIN_USER,
        help=f"Cognito user for an Oidc stack (default: {cognito.ADMIN_USER})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target, session = load_target(args)
    print(f"Stack {target.stack}, hosts {', '.join(target.hosts)}", flush=True)
    idp: dict[str, str] = {}
    if target.auth_provider == OIDC_PROVIDER:
        idp = cognito.require(cognito.read_cognito_record(args.idp_name), args.idp_name)
    # The Cognito password secrets live in the pool's region.
    secret_region = idp.get("Region")

    def read_password() -> str:
        secrets_manager = session.client("secretsmanager", region_name=secret_region)
        reply = secrets_manager.get_secret_value(SecretId=target.password_secret)
        return reply["SecretString"]

    def request_token(username: str, password: str) -> str:
        cognito_idp = session.client("cognito-idp", region_name=idp["Region"])
        reply = cognito_idp.admin_initiate_auth(
            UserPoolId=idp["UserPoolId"],
            ClientId=idp["HumeClientId"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        # Hume reads the user name from the access token, not the ID token.
        return reply["AuthenticationResult"]["AccessToken"]

    return run_checks(target, Http(), read_password, request_token)


if __name__ == "__main__":
    sys.exit(main())
