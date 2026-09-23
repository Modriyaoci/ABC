"""Read the user's shared deployment without starting another official poller."""
from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from http import HTTPStatus
from http.client import HTTPException
from urllib.parse import unquote, urlsplit

from sync_service import SSL_CONTEXT


GET_PATHS = {"/api/schedule", "/api/status", "/api/match", "/api/tournament", "/api/player-photo"}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
UPSTREAM_TIMEOUT = 90


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_upstream_url(value: str) -> str:
    """Accept an origin only; never carry credentials or follow another origin."""
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("上游地址必须是完整的 HTTPS 网址")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("上游地址格式不正确") from error
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not _is_loopback(hostname))
        or port == 0
    ):
        raise ValueError("上游须为不含账号、参数和路径的 HTTPS 地址；本机测试可用 HTTP")
    return f"{parsed.scheme}://{parsed.netloc}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def make_upstream_handler(base_handler, upstream_url: str):
    """Create an API-only forwarding handler; static files and health stay local."""
    origin = validate_upstream_url(upstream_url)
    destination = urlsplit(origin)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
        _NoRedirect(),
    )

    class UpstreamRequestHandler(base_handler):
        upstream_origin = origin

        def _proxy_photo(self, query: str) -> None:
            url = origin + "/api/player-photo" + (f"?{query}" if query else "")
            try:
                request = urllib.request.Request(url, headers={"Accept": "image/*", "User-Agent": "AichiSchedule/1.0"})
                with opener.open(request, timeout=UPSTREAM_TIMEOUT) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                    status = response.status
                    content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            except (OSError, urllib.error.URLError, HTTPException):
                self.send_error(HTTPStatus.BAD_GATEWAY)
                return
            if status != 200 or len(raw) > 2 * 1024 * 1024 or not content_type.startswith("image/"):
                self.send_error(status if status >= 400 else HTTPStatus.BAD_GATEWAY)
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self._security_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _proxy_api(self, method: str, path: str, query: str) -> None:
            destination_port = destination.port or (443 if destination.scheme == "https" else 80)
            if _is_loopback(destination.hostname) and destination_port == self.server.server_port:
                self._send_json({"message": "上游不能指向本地网站自身"}, HTTPStatus.BAD_GATEWAY)
                return

            body = None
            headers = {"Accept": "application/json", "User-Agent": "AichiSchedule/1.0"}
            # Forward the browser validator so an unchanged upstream payload
            # can short-circuit as 304 instead of crossing the service link.
            if self.headers.get("If-None-Match"):
                headers["If-None-Match"] = self.headers["If-None-Match"]
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json({"message": "请求长度不正确"}, HTTPStatus.BAD_REQUEST)
                    return
                if length < 0 or self.headers.get("Transfer-Encoding"):
                    self._send_json({"message": "不支持此请求格式"}, HTTPStatus.BAD_REQUEST)
                    return
                if length > MAX_REQUEST_BYTES:
                    self._send_json({"message": "请求过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                body = self.rfile.read(length)
                headers["Content-Type"] = self.headers.get("Content-Type", "application/json")

            url = origin + path + (f"?{query}" if query else "")
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                try:
                    response = opener.open(request, timeout=UPSTREAM_TIMEOUT)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status = response.status
                    if status == HTTPStatus.NOT_MODIFIED:
                        self.send_response(status)
                        etag = response.headers.get("ETag")
                        if etag:
                            self.send_header("ETag", etag)
                        self.send_header("Cache-Control", "no-cache")
                        self._security_headers()
                        self.end_headers()
                        return
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    retry_after = response.headers.get("Retry-After")
                if 300 <= status < 400:
                    self._send_json({"message": f"上游返回重定向（HTTP {status}），未获取到比赛数据"}, HTTPStatus.BAD_GATEWAY)
                    return
                if len(raw) > MAX_RESPONSE_BYTES:
                    self._send_json({"message": "上游数据过大，未读取新的比赛数据"}, HTTPStatus.BAD_GATEWAY)
                    return
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("Expected a JSON object")
                except (ValueError, UnicodeError):
                    error_status = status if status >= 400 else HTTPStatus.BAD_GATEWAY
                    self._send_json({"message": f"上游未返回有效比赛数据（HTTP {status}）"}, error_status)
                    return
            except (OSError, urllib.error.URLError, HTTPException):
                self._send_json({"message": "暂时无法连接共享比分服务，未获得最新数据"}, HTTPStatus.BAD_GATEWAY)
                return

            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
            # Reuse the local response helper for stable ETags and gzip.  It
            # also handles a client validator when the upstream did not.
            self._send_json(value, HTTPStatus(status), retry_after=retry_after)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path == "/api/player-photo":
                self._proxy_photo(parsed.query)
            elif path in GET_PATHS:
                self._proxy_api("GET", path, parsed.query)
            elif path.startswith("/api/") and path != "/api/health":
                self._send_json({"message": "找不到此接口"}, HTTPStatus.NOT_FOUND)
            else:
                super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if unquote(parsed.path) != "/api/sync":
                self._send_json({"message": "找不到此接口"}, HTTPStatus.NOT_FOUND)
                return
            self._proxy_api("POST", "/api/sync", parsed.query)

    return UpstreamRequestHandler
