import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import server
from upstream_service import make_upstream_handler, validate_upstream_url


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = (200, {"records": [{"id": "TTE:live", "score": "2:1"}]}, {})
        test = self

        class FakeUpstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test.requests.append((self.command, self.path, dict(self.headers), body))
                status, value, headers = test.response
                if status is None:
                    self.close_connection = True
                    return
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            do_GET = respond
            do_POST = respond

        class QuietLocal(server.RequestHandler):
            def log_message(self, *args):
                pass

        self.upstream = self.start_server(FakeUpstream)
        handler = make_upstream_handler(QuietLocal, f"http://127.0.0.1:{self.upstream.server_port}")
        self.local = self.start_server(handler)

    def start_server(self, handler):
        instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            instance.shutdown()
            instance.server_close()
            thread.join(timeout=2)

        self.addCleanup(stop)
        return instance

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.local.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_all_read_apis_forward_queries_and_scores_without_local_official_calls(self):
        with patch.object(server, "get_match_details") as details, patch.object(server, "get_tournament") as tournament:
            for path in (
                "/api/schedule", "/api/status", "/api/match?id=TTE%3Afinal%2B1",
                "/api/tournament?sport=BDM",
            ):
                with self.subTest(path=path):
                    status, headers, body = self.request(path)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), self.response[1])
                    self.assertEqual(self.requests[-1][1], path)
                    self.assertEqual(headers["Cache-Control"], "no-store")
            details.assert_not_called()
            tournament.assert_not_called()

    def test_sync_posts_only_to_upstream_without_forwarding_client_credentials(self):
        self.response = (202, {"accepted": True, "message": "已开始同步"}, {})
        with patch.object(server.STATE, "start_sync") as local_sync:
            status, _, raw = self.request(
                "/api/sync", method="POST", body=b'{}',
                headers={"Content-Type": "application/json", "Authorization": "private-client-value", "Cookie": "private=value"},
            )
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(raw)["accepted"])
        method, path, headers, body = self.requests[0]
        self.assertEqual((method, path, body), ("POST", "/api/sync", b"{}"))
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        local_sync.assert_not_called()

    def test_rate_limit_and_failures_keep_http_status_and_json(self):
        for status in (409, 429, 503):
            with self.subTest(status=status):
                value = {"accepted": False, "message": "来源暂不可用", "retryAfterSeconds": 60}
                self.response = (status, value, {"Retry-After": "60"})
                returned, headers, body = self.request("/api/sync", method="POST")
                self.assertEqual(returned, status)
                self.assertEqual(json.loads(body), value)
                self.assertEqual(headers["Retry-After"], "60")

    def test_invalid_json_and_connection_loss_are_not_successes(self):
        for upstream_status, payload, expected in (
            (200, b"<html>unavailable</html>", 502),
            (200, [], 502),
            (429, b"too many requests", 429),
            (503, b"unavailable", 503),
            (None, b"", 502),
        ):
            with self.subTest(upstream_status=upstream_status):
                self.response = (upstream_status, payload, {})
                status, _, body = self.request("/api/status")
                self.assertEqual(status, expected)
                self.assertIn("message", json.loads(body))
                self.assertNotIn("realtimeAvailable", json.loads(body))

    def test_redirect_is_not_followed_to_a_different_resource(self):
        self.response = (302, {}, {"Location": f"http://127.0.0.1:{self.upstream.server_port}/unexpected"})
        status, _, raw = self.request("/api/schedule")
        self.assertEqual(status, 502)
        self.assertIn("重定向", json.loads(raw)["message"])
        self.assertEqual(len(self.requests), 1)

    def test_health_static_and_unknown_routes_do_not_contact_upstream(self):
        status, _, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        for path in ("/tennis", "/badminton", "/app.js"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertGreater(len(body), 0)
        self.assertEqual(self.request("/api/unknown")[0], 404)
        self.assertEqual(self.request("/api/health", method="POST")[0], 404)
        self.assertEqual(self.requests, [])


class UpstreamConfigurationTests(unittest.TestCase):
    def test_only_https_origins_or_loopback_http_are_accepted(self):
        for value in ("https://example.com", "https://example.com/", "http://127.0.0.1:1234", "http://localhost:1234", "http://[::1]:1234"):
            with self.subTest(value=value):
                self.assertEqual(validate_upstream_url(value), value.rstrip("/"))
        for value in (
            "", "example.com", "http://example.com", "ftp://example.com",
            "https://user:pass@example.com", "https://example.com/api", "https://example.com?token=value",
            "https://example.com#fragment", "https://example.com:bad", "https://example.com:0",
            "https://example.com\n", "http://192.168.1.1", "http://localhost.example.com",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_upstream_url(value)

    def test_upstream_mode_does_not_start_an_official_scheduler(self):
        for upstream in (False, True):
            arguments = ["server.py", "--port", "0"]
            if upstream:
                arguments += ["--upstream", "https://example.com"]
            with self.subTest(upstream=upstream), patch.dict("os.environ", {"SCHEDULE_UPSTREAM_URL": ""}), \
                    patch("sys.argv", arguments), \
                    patch.object(server, "ThreadingHTTPServer") as http_server, \
                    patch.object(server.threading, "Thread") as thread, \
                    patch.object(server.signal, "signal"), patch.object(server, "STATE", Mock()):
                server.main()
                http_server.return_value.serve_forever.assert_called_once()
                if upstream:
                    thread.assert_not_called()
                    self.assertNotEqual(http_server.call_args.args[1], server.RequestHandler)
                else:
                    thread.return_value.start.assert_called_once()
                    self.assertEqual(http_server.call_args.args[1], server.RequestHandler)

    def test_environment_selects_upstream_and_command_line_can_override_it(self):
        for arguments, expected in (
            (["server.py"], "https://environment.example.com"),
            (["server.py", "--upstream", "https://argument.example.com"], "https://argument.example.com"),
        ):
            with self.subTest(arguments=arguments), \
                    patch.dict("os.environ", {"SCHEDULE_UPSTREAM_URL": "https://environment.example.com"}), \
                    patch("sys.argv", arguments), patch.object(server, "ThreadingHTTPServer") as http_server, \
                    patch.object(server.threading, "Thread") as thread, \
                    patch.object(server.signal, "signal"), patch.object(server, "STATE", Mock()):
                server.main()
                self.assertEqual(http_server.call_args.args[1].upstream_origin, expected)
                thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
