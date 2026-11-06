"""SQLite persistence and room/session rules for NANE."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PLAYER_COLORS = ("violet", "rose", "mint", "gold")
PLAYER_AVATARS = ("نجمة", "نار", "برق", "قناع")
ROOM_CODE_LENGTH = 10
ROOM_TTL_SECONDS = 24 * 60 * 60
ONLINE_WINDOW_SECONDS = 25


class StoreError(Exception):
    """Base class for expected application errors."""
    status = 400
    code = "bad_request"


class InvalidInput(StoreError):
    status = 400
    code = "invalid_input"


class Unauthorized(StoreError):
    status = 401
    code = "unauthorized"


class NotFound(StoreError):
    status = 404
    code = "not_found"


class RoomFull(StoreError):
    status = 409
    code = "room_full"


class NameTaken(StoreError):
    status = 409
    code = "name_taken"


class RoomNotOpen(StoreError):
    status = 409
    code = "room_not_open"


def _now() -> int:
    return int(time.time())


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _has_active_match(db: sqlite3.Connection, room_id: str) -> bool:
    table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'matches'"
    ).fetchone()
    if table is None:
        return False
    return db.execute(
        "SELECT 1 FROM matches WHERE room_id = ? AND status = 'active' LIMIT 1",
        (room_id,),
    ).fetchone() is not None


def _normalize_code(value: str) -> str:
    code = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    if len(code) != ROOM_CODE_LENGTH:
        raise InvalidInput("رمز الغرفة يجب أن يتكون من 10 أحرف وأرقام.")
    return code


def validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidInput("اكتب اسم اللاعب.")
    name = unicodedata.normalize("NFC", value)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    name = " ".join(name.split()).strip()
    if not name:
        raise InvalidInput("اكتب اسم اللاعب.")
    if len(name) > 18:
        raise InvalidInput("الاسم طويل جدًا. الحد الأقصى 18 حرفًا.")
    return name


def default_database_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "nane-family" / "nane.sqlite3"


class NaneStore:
    """Thread-safe SQLite repository. Each operation uses its own connection."""

    def __init__(self, database_path: Optional[str] = None) -> None:
        raw_path = database_path or os.environ.get("NANE_DB_PATH")
        self.database_path = Path(raw_path).expanduser() if raw_path else default_database_path()
        # Only tighten permissions on the application's own default data folder.
        # A caller-supplied parent may be shared (for example /tmp) and must not
        # have its permissions modified as a side effect.
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not raw_path:
            try:
                os.chmod(str(self.database_path.parent), 0o700)
            except OSError:
                pass
        self._initialize()
        if self.database_path.exists():
            try:
                os.chmod(str(self.database_path), 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    invite_code TEXT NOT NULL UNIQUE,
                    phase TEXT NOT NULL DEFAULT 'lobby' CHECK (phase IN ('lobby')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    seat INTEGER NOT NULL CHECK (seat BETWEEN 1 AND 4),
                    display_name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    color TEXT NOT NULL,
                    avatar TEXT NOT NULL,
                    ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    UNIQUE (room_id, seat),
                    UNIQUE (room_id, name_key)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_players_room ON players(room_id, seat);
                CREATE INDEX IF NOT EXISTS idx_sessions_player ON sessions(player_id);
                CREATE INDEX IF NOT EXISTS idx_rooms_created ON rooms(created_at);
                """
            )
            db.execute("DELETE FROM rooms WHERE created_at < ?", (_now() - ROOM_TTL_SECONDS,))

    @staticmethod
    def _new_code() -> str:
        return "".join(secrets.choice(CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))

    @staticmethod
    def _new_session() -> str:
        return secrets.token_urlsafe(32)

    def _revoke_existing_session(self, db: sqlite3.Connection, token: Optional[str]) -> None:
        if not token:
            return
        token_hash = _hash_token(token)
        row = db.execute(
            "SELECT p.id, p.room_id FROM sessions s JOIN players p ON p.id = s.player_id WHERE s.token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return
        if self._has_active_match(db, row["room_id"]):
            raise RoomNotOpen("لا يمكنك مغادرة الغرفة أو نقل جلستك أثناء المباراة.")
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        db.execute("DELETE FROM players WHERE id = ?", (row["id"],))
        db.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (_now(), row["room_id"]))

    def create_room(self, display_name: Any, current_token: Optional[str] = None) -> Dict[str, Any]:
        name = validate_name(display_name)
        now = _now()
        session_token = self._new_session()
        player_id = uuid.uuid4().hex
        room_id = uuid.uuid4().hex

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._revoke_existing_session(db, current_token)
            db.execute("DELETE FROM rooms WHERE created_at < ?", (now - ROOM_TTL_SECONDS,))

            invite_code = None
            for _ in range(20):
                candidate = self._new_code()
                try:
                    db.execute(
                        "INSERT INTO rooms (id, invite_code, phase, created_at, updated_at) VALUES (?, ?, 'lobby', ?, ?)",
                        (room_id, candidate, now, now),
                    )
                    invite_code = candidate
                    break
                except sqlite3.IntegrityError:
                    continue
            if invite_code is None:
                raise StoreError("تعذر إنشاء رمز غرفة فريد. حاول مرة أخرى.")

            db.execute(
                "INSERT INTO players (id, room_id, seat, display_name, name_key, color, avatar, ready, created_at, last_seen) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, 0, ?, ?)",
                (player_id, room_id, name, name.casefold(), PLAYER_COLORS[0], PLAYER_AVATARS[0], now, now),
            )
            db.execute(
                "INSERT INTO sessions (token_hash, player_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
                (_hash_token(session_token), player_id, now, now),
            )
            db.commit()

        return {
            "session_token": session_token,
            "invite_code": invite_code,
            "player_id": player_id,
            "room": self.get_room(invite_code, player_id=player_id),
        }

    def join_room(self, invite_code: Any, display_name: Any, current_token: Optional[str] = None) -> Dict[str, Any]:
        code = _normalize_code(invite_code if isinstance(invite_code, str) else "")
        name = validate_name(display_name)
        now = _now()
        session_token = self._new_session()
        player_id = uuid.uuid4().hex

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            room = db.execute(
                "SELECT id, phase FROM rooms WHERE invite_code = ? AND created_at >= ?",
                (code, now - ROOM_TTL_SECONDS),
            ).fetchone()
            if room is None:
                raise NotFound("لم نعثر على هذه الغرفة. تحقق من الرمز.")
            if room["phase"] != "lobby" or _has_active_match(db, room["id"]):
                raise RoomNotOpen("بدأت المباراة. لا يمكن الانضمام إلى الغرفة الآن.")

            old_session = None
            if current_token:
                old_session = db.execute(
                    "SELECT p.id AS player_id, p.room_id FROM sessions s JOIN players p ON p.id = s.player_id WHERE s.token_hash = ?",
                    (_hash_token(current_token),),
                ).fetchone()

            duplicate = db.execute(
                "SELECT id FROM players WHERE room_id = ? AND name_key = ?",
                (room["id"], name.casefold()),
            ).fetchone()
            if duplicate and not (old_session and duplicate["id"] == old_session["player_id"]):
                raise NameTaken("هذا الاسم مستخدم في الغرفة. اختر اسمًا مختلفًا.")

            current_player_in_target = bool(old_session and old_session["room_id"] == room["id"])
            occupied_count = db.execute(
                "SELECT COUNT(*) AS count FROM players WHERE room_id = ?", (room["id"],)
            ).fetchone()["count"]
            if occupied_count >= 4 and not current_player_in_target:
                raise RoomFull("الغرفة مكتملة. لا يمكن دخول لاعب خامس.")

            self._revoke_existing_session(db, current_token)
            occupied = {
                row["seat"] for row in db.execute("SELECT seat FROM players WHERE room_id = ?", (room["id"],)).fetchall()
            }
            seat = next((number for number in range(1, 5) if number not in occupied), None)
            if seat is None:
                raise RoomFull("الغرفة مكتملة. لا يمكن دخول لاعب خامس.")

            db.execute(
                "INSERT INTO players (id, room_id, seat, display_name, name_key, color, avatar, ready, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (player_id, room["id"], seat, name, name.casefold(), PLAYER_COLORS[seat - 1], PLAYER_AVATARS[seat - 1], now, now),
            )
            db.execute(
                "INSERT INTO sessions (token_hash, player_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
                (_hash_token(session_token), player_id, now, now),
            )
            db.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room["id"]))
            db.commit()

        return {
            "session_token": session_token,
            "invite_code": code,
            "player_id": player_id,
            "room": self.get_room(code, player_id=player_id),
        }

    def _touch_session(self, db: sqlite3.Connection, token: str) -> sqlite3.Row:
        token_hash = _hash_token(token)
        row = db.execute(
            "SELECT p.id AS player_id, p.room_id, r.invite_code "
            "FROM sessions s JOIN players p ON p.id = s.player_id JOIN rooms r ON r.id = p.room_id "
            "WHERE s.token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise Unauthorized("انتهت جلسة اللاعب. أدخل رمز الغرفة للانضمام من جديد.")
        now = _now()
        db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, token_hash))
        db.execute("UPDATE players SET last_seen = ? WHERE id = ?", (now, row["player_id"]))
        db.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, row["room_id"]))
        return row

    def get_session(self, token: Optional[str]) -> Dict[str, Any]:
        if not token:
            raise Unauthorized("لا توجد جلسة لاعب نشطة.")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._touch_session(db, token)
            db.commit()
        return {
            "player_id": row["player_id"],
            "invite_code": row["invite_code"],
            "room": self.get_room(row["invite_code"], player_id=row["player_id"]),
        }

    def get_room_for_session(self, invite_code: str, token: Optional[str]) -> Dict[str, Any]:
        if not token:
            raise Unauthorized("سجّل الدخول إلى الغرفة أولًا.")
        code = _normalize_code(invite_code)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            session = self._touch_session(db, token)
            if session["invite_code"] != code:
                raise Unauthorized("هذه الجلسة لا تنتمي إلى الغرفة المطلوبة.")
            db.commit()
            player_id = session["player_id"]
        return self.get_room(code, player_id=player_id)

    def set_ready(self, invite_code: str, token: Optional[str], ready: Any) -> Dict[str, Any]:
        if not isinstance(ready, bool):
            raise InvalidInput("حالة الاستعداد غير صحيحة.")
        if not token:
            raise Unauthorized("سجّل الدخول إلى الغرفة أولًا.")
        code = _normalize_code(invite_code)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            session = self._touch_session(db, token)
            if session["invite_code"] != code:
                raise Unauthorized("هذه الجلسة لا تنتمي إلى الغرفة المطلوبة.")
            if _has_active_match(db, session["room_id"]):
                raise RoomNotOpen("لا يمكن تغيير الاستعداد بعد بدء المباراة.")
            db.execute("UPDATE players SET ready = ? WHERE id = ?", (1 if ready else 0, session["player_id"]))
            db.commit()
            player_id = session["player_id"]
        return self.get_room(code, player_id=player_id)

    def leave_room(self, invite_code: str, token: Optional[str]) -> None:
        if not token:
            raise Unauthorized("لا توجد جلسة لاعب نشطة.")
        code = _normalize_code(invite_code)
        token_hash = _hash_token(token)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute(
                "SELECT p.id AS player_id, p.room_id, r.invite_code "
                "FROM sessions s JOIN players p ON p.id = s.player_id JOIN rooms r ON r.id = p.room_id "
                "WHERE s.token_hash = ?",
                (token_hash,),
            ).fetchone()
            if session is None or session["invite_code"] != code:
                raise Unauthorized("هذه الجلسة لا تنتمي إلى الغرفة المطلوبة.")
            if _has_active_match(db, session["room_id"]):
                raise RoomNotOpen("لا يمكنك مغادرة المباراة الجارية. أغلق المتصفح مؤقتًا أو أكمل المباراة.")
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            db.execute("DELETE FROM players WHERE id = ?", (session["player_id"],))
            db.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (_now(), session["room_id"]))
            db.commit()

    def get_room(self, invite_code: str, player_id: Optional[str] = None) -> Dict[str, Any]:
        code = _normalize_code(invite_code)
        now = _now()
        with closing(self._connect()) as db:
            room = db.execute(
                "SELECT id, invite_code, phase, created_at, updated_at FROM rooms WHERE invite_code = ? AND created_at >= ?",
                (code, now - ROOM_TTL_SECONDS),
            ).fetchone()
            if room is None:
                raise NotFound("لم نعثر على هذه الغرفة.")
            rows = db.execute(
                "SELECT id, seat, display_name, color, avatar, ready, last_seen FROM players WHERE room_id = ? ORDER BY seat",
                (room["id"],),
            ).fetchall()

        players = []
        for row in rows:
            players.append({
                "id": row["id"],
                "seat": row["seat"],
                "name": row["display_name"],
                "color": row["color"],
                "avatar": row["avatar"],
                "ready": bool(row["ready"]),
                "connected": (now - row["last_seen"]) <= ONLINE_WINDOW_SECONDS,
            })
        return {
            "code": room["invite_code"],
            "phase": room["phase"],
            "created_at": room["created_at"],
            "updated_at": room["updated_at"],
            "occupied": len(players),
            "capacity": 4,
            "all_ready": 3 <= len(players) <= 4 and all(player["ready"] and player["connected"] for player in players),
            "players": players,
            "you": next((player for player in players if player["id"] == player_id), None),
        }

    @staticmethod
    def public_session_snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "player_id": session["player_id"],
            "invite_code": session["invite_code"],
            "room": session["room"],
        }
