import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from server.app import create_server


class NaneHttpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = str(Path(self.temp.name) / "nane-test.sqlite3")
        self.server = create_server("127.0.0.1", 0, db)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.port
        self.opener = build_opener()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def call(self, method, path, data=None, cookie=None, origin=None):
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, json.loads(response.read().decode("utf-8")), response.headers
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8")), error.headers

    @staticmethod
    def cookie(headers):
        return headers.get("Set-Cookie", "").split(";", 1)[0]

    def test_health_and_static_home(self):
        status, health, _ = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["name"], "NANE")
        response = self.opener.open(self.base + "/", timeout=5)
        self.assertEqual(response.status, 200)
        self.assertIn("NANE", response.read().decode("utf-8"))

    def test_create_and_recover_authenticated_session(self):
        status, created, headers = self.call("POST", "/api/rooms", {"name": "يوسف"})
        self.assertEqual(status, 201)
        self.assertEqual(created["room"]["occupied"], 1)
        cookie = self.cookie(headers)
        self.assertTrue(cookie.startswith("nane_session="))
        status, session, _ = self.call("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(session["invite_code"], created["invite_code"])

    def test_three_players_can_all_be_ready(self):
        status, first, h1 = self.call("POST", "/api/rooms", {"name": "A"})
        self.assertEqual(status, 201)
        code = first["invite_code"]
        cookies = [self.cookie(h1)]
        for name in ("B", "C"):
            status, joined, headers = self.call("POST", "/api/rooms/%s/join" % code, {"name": name})
            self.assertEqual(status, 201)
            cookies.append(self.cookie(headers))
        for token in cookies:
            status, ready, _ = self.call(
                "POST", "/api/rooms/%s/ready" % code, {"ready": True}, cookie=token
            )
            self.assertEqual(status, 200)
        status, room, _ = self.call("GET", "/api/rooms/%s" % code, cookie=cookies[0])
        self.assertEqual(status, 200)
        self.assertEqual(room["room"]["occupied"], 3)
        self.assertTrue(room["room"]["all_ready"])

    def test_cross_origin_write_is_rejected(self):
        status, result, _ = self.call(
            "POST", "/api/rooms", {"name": "A"}, origin="https://evil.example"
        )
        self.assertEqual(status, 403)
        self.assertEqual(result["code"], "origin_rejected")

    def test_diagnostic_feed_is_shared_in_server(self):
        status, posted, _ = self.call("POST", "/api/messages", {"message": "رسالة من الهاتف"})
        self.assertEqual(status, 201)
        status, feed, _ = self.call("GET", "/api/messages?after_id=0")
        self.assertEqual(status, 200)
        self.assertEqual(feed["messages"][0]["message"], "رسالة من الهاتف")
        self.assertEqual(feed["messages"][0]["id"], posted["message"]["id"])


if __name__ == "__main__":
    unittest.main()
