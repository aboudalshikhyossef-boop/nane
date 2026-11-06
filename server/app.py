"""Clean NANE HTTP application: lobby, sessions, diagnostics and static assets."""
from __future__ import annotations
import argparse, json, os, re, sys, threading, time, unicodedata
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from .store import InvalidInput, NaneStore, StoreError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
COMPAT = ROOT / "compatibility" / "index.html"
COOKIE = "nane_session"
MAX_BODY = 8192

class MessageBus:
    """Temporary cross-device diagnostic feed. Never store private game data here."""
    def __init__(self, limit=100):
        self.items = deque(maxlen=limit)
        self.next_id = 1
        self.lock = threading.Lock()
    def publish(self, message):
        with self.lock:
            item = {"id": self.next_id, "message": message, "created_at": int(time.time())}
            self.next_id += 1
            self.items.append(item)
            return dict(item)
    def since(self, after_id):
        with self.lock:
            items = [dict(item) for item in self.items if item["id"] > after_id]
            latest = self.next_id - 1
        return {"ok": True, "messages": items, "latest_id": latest}

class RateLimiter:
    def __init__(self):
        self.events = defaultdict(deque)
        self.lock = threading.Lock()
    def allow(self, key, bucket, maximum, window):
        now = time.monotonic()
        with self.lock:
            events = self.events[bucket + ":" + key]
            while events and now - events[0] >= window:
                events.popleft()
            if len(events) >= maximum:
                return False
            events.append(now)
            return True

class NaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
        raise InvalidInput("حجم الطلب غير صحيح.")
    if length < 0 or length > MAX_BODY:
        raise InvalidInput("حجم البيانات أكبر من الحد المسموح.")
    if not length:
        return {}
    try:
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InvalidInput("تعذر قراءة بيانات الطلب.")
    if not isinstance(value, dict):
        raise InvalidInput("يجب إرسال بيانات بصيغة JSON object.")
    return value

def session_cookie(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except Exception:
        return None
    morsel = cookie.get(COOKIE)
    return morsel.value if morsel else None

def safe_origin(handler):
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    host = handler.headers.get("Host", "").lower()
    return bool(parsed.netloc) and parsed.netloc.lower() == host and parsed.scheme in ("http", "https")

def make_handler(store, bus, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NaneFamily/1.0"
        sys_version = ""
        def log_message(self, fmt, *args):
            sys.stdout.write("[nane-http] %s - %s\n" % (self.address_string(), fmt % args))
            sys.stdout.flush()
        def rate_key(self):
            value = self.headers.get("CF-Connecting-IP", "").strip()
            return value if value and len(value) <= 64 else self.client_address[0]
        def _send_headers(self, content_type="application/json; charset=utf-8", cache="no-store"):
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        def json_response(self, status, payload, token=None, clear_cookie=False):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._send_headers()
            if token:
                self.send_header("Set-Cookie", "%s=%s; Path=/; Max-Age=604800; HttpOnly; Secure; SameSite=Lax" % (COOKIE, token))
            elif clear_cookie:
                self.send_header("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax" % COOKIE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def error(self, exc):
            self.json_response(exc.status, {"ok": False, "code": exc.code, "error": str(exc)})
        def static(self, path, kind):
            try:
                body = path.read_bytes()
            except OSError:
                self.json_response(500, {"ok": False, "error": "تعذر تحميل ملف الواجهة."})
                return
            self.send_response(200)
            self._send_headers(kind, "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                return self.static(WEB / "index.html", "text/html; charset=utf-8")
            if path in ("/static/main.css",):
                return self.static(WEB / "css" / "main.css", "text/css; charset=utf-8")
            if path in ("/static/app.js",):
                return self.static(WEB / "js" / "app.js", "application/javascript; charset=utf-8")
            if path in ("/compatibility", "/compatibility/"):
                return self.static(COMPAT, "text/html; charset=utf-8")
            if path in ("/api/health", "/api/ping"):
                return self.json_response(200, {"ok": True, "name": "NANE", "python": sys.version.split()[0], "server_time": int(time.time())})
            if path == "/api/messages":
                query = parse_qs(parsed.query)
                try:
                    after = max(0, int(query.get("after_id", ["0"])[0]))
                except (TypeError, ValueError):
                    after = 0
                return self.json_response(200, bus.since(after))
            if path == "/api/me":
                try:
                    session = store.get_session(session_cookie(self))
                    return self.json_response(200, store.public_session_snapshot(session))
                except StoreError as exc:
                    return self.error(exc)
            match = re.fullmatch(r"/api/rooms/([A-Za-z0-9]+)/?", path)
            if match:
                try:
                    state = store.get_room_for_session(match.group(1), session_cookie(self))
                    return self.json_response(200, {"ok": True, "room": state})
                except StoreError as exc:
                    return self.error(exc)
            self.json_response(404, {"ok": False, "code": "not_found", "error": "المسار المطلوب غير موجود."})
        def do_POST(self):
            if not safe_origin(self):
                return self.json_response(403, {"ok": False, "code": "origin_rejected", "error": "تم رفض مصدر الطلب."})
            path = urlparse(self.path).path
            key = self.rate_key()
            if path == "/api/rooms" and not limiter.allow(key, "create", 5, 600):
                return self.json_response(429, {"ok": False, "code": "rate_limited", "error": "محاولات إنشاء الغرف كثيرة. انتظر قليلًا."})
            if re.fullmatch(r"/api/rooms/[A-Za-z0-9]+/join", path) and not limiter.allow(key, "join", 15, 60):
                return self.json_response(429, {"ok": False, "code": "rate_limited", "error": "محاولات الانضمام كثيرة. انتظر قليلًا."})
            try:
                data = read_json(self)
                token = session_cookie(self)
                if path == "/api/messages":
                    message = data.get("message", "")
                    if not isinstance(message, str):
                        raise InvalidInput("الرسالة يجب أن تكون نصًا.")
                    message = "".join(ch for ch in message if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
                    message = " ".join(message.split())
                    if not message or len(message) > 120:
                        raise InvalidInput("اكتب رسالة من حرف واحد إلى 120 حرفًا.")
                    return self.json_response(201, {"ok": True, "stored_by_server": True, "message": bus.publish(message)})
                if path == "/api/rooms":
                    result = store.create_room(data.get("name"), token)
                    return self.json_response(201, {"ok": True, "player_id": result["player_id"], "invite_code": result["invite_code"], "room": result["room"]}, token=result["session_token"])
                join = re.fullmatch(r"/api/rooms/([A-Za-z0-9]+)/join", path)
                if join:
                    result = store.join_room(join.group(1), data.get("name"), token)
                    return self.json_response(201, {"ok": True, "player_id": result["player_id"], "invite_code": result["invite_code"], "room": result["room"]}, token=result["session_token"])
                ready = re.fullmatch(r"/api/rooms/([A-Za-z0-9]+)/ready", path)
                if ready:
                    room = store.set_ready(ready.group(1), token, data.get("ready"))
                    return self.json_response(200, {"ok": True, "room": room})
                leave = re.fullmatch(r"/api/rooms/([A-Za-z0-9]+)/leave", path)
                if leave:
                    store.leave_room(leave.group(1), token)
                    return self.json_response(200, {"ok": True, "left": True}, clear_cookie=True)
                self.json_response(404, {"ok": False, "code": "not_found", "error": "المسار المطلوب غير موجود."})
            except StoreError as exc:
                self.error(exc)
        def do_OPTIONS(self):
            self.json_response(405, {"ok": False, "code": "method_not_allowed", "error": "الطريقة غير مسموحة."})
    return Handler

def create_server(host="127.0.0.1", port=8765, database_path=None):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("The NANE server must bind to loopback only.")
    store = NaneStore(database_path)
    bus = MessageBus()
    limiter = RateLimiter()
    server = NaneHTTPServer((host, port), make_handler(store, bus, limiter))
    server.nane_store = store
    server.message_bus = bus
    server.rate_limiter = limiter
    return server

def main():
    parser = argparse.ArgumentParser(description="Run the NANE local application server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("NANE_PORT", "8765")))
    parser.add_argument("--database", default=None)
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.database)
    print("NANE application server is running.", flush=True)
    print("Local address: http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
