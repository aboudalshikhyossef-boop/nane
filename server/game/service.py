"""Authenticated profile and five-phase match lifecycle service."""
from __future__ import annotations

import json
import secrets
import time
import uuid
from contextlib import closing
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from ..content import ContentEngine, ContentSelectionError, load_catalogs
from ..store import (
    InvalidInput, NaneStore, NotFound, RoomNotOpen, StoreError, Unauthorized,
    _hash_token, _normalize_code, _has_active_match,
)
from .engine import PHASES, PHASE_LABELS, MatchPlanningError, build_phase_tasks


class MatchConflict(StoreError):
    status = 409
    code = "match_conflict"


PHASE_TIMINGS = {
    "knowledge": 85,
    "mystery": 32,
    "player_answer": 65,
    "player_guess": 65,
    "live_answer": 65,
    "live_guess": 37,
    "finale": 80,
    "reveal": 12,
}


class MatchService:
    """Owns profile privacy, match state transitions, scoring and durable recovery."""

    def __init__(self, store: NaneStore, catalog=None,
                 clock: Optional[Callable[[], float]] = None,
                 timings: Optional[Mapping[str, int]] = None):
        self.store = store
        self.catalog = catalog or load_catalogs()
        self.clock = clock or time.time
        self.timings = dict(PHASE_TIMINGS)
        if timings:
            for key, value in timings.items():
                if key not in self.timings or not isinstance(value, int) or value < 1:
                    raise ValueError("Invalid match timing override.")
                self.timings[key] = value

    def _now(self) -> int:
        return int(self.clock())

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> Dict[str, Any]:
        data = json.loads(value)
        if not isinstance(data, dict):
            raise StoreError("تعذر قراءة حالة المباراة المحفوظة.")
        return data

    def _actor(self, db, invite_code: str, token: Optional[str]):
        if not token:
            raise Unauthorized("سجّل الدخول إلى الغرفة أولًا.")
        code = _normalize_code(invite_code)
        session = self.store._touch_session(db, token)
        if session["invite_code"] != code:
            raise Unauthorized("هذه الجلسة لا تنتمي إلى الغرفة المطلوبة.")
        return session["player_id"], session["room_id"]

    def _profile_row(self, db, player_id: str):
        return db.execute(
            "SELECT answers_json, completed_count, version, updated_at "
            "FROM player_profiles WHERE player_id = ?", (player_id,)
        ).fetchone()

    def get_profile(self, invite_code: str, token: Optional[str]) -> Dict[str, Any]:
        with closing(self.store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            player_id, _ = self._actor(db, invite_code, token)
            row = self._profile_row(db, player_id)
            db.commit()
        answers = json.loads(row["answers_json"]) if row else {}
        questions = []
        for item in self.catalog.profile_questions:
            selected = answers.get(item["field_id"])
            questions.append({
                "id": item["id"],
                "field_id": item["field_id"],
                "text_ar": item["text_ar"],
                "category": item["category"],
                "options": item["options"],
                "selected_option_id": selected,
            })
        completed = len(answers)
        return {
            "ok": True,
            "profile": {
                "answers": answers,
                "completed_count": completed,
                "required_count": 21,
                "is_complete": completed == 21,
                "version": row["version"] if row else 1,
                "updated_at": row["updated_at"] if row else None,
                "questions": questions,
            },
        }

    def save_profile(self, invite_code: str, token: Optional[str],
                     answers: Any) -> Dict[str, Any]:
        if not isinstance(answers, dict):
            raise InvalidInput("إجابات الملف الشخصي يجب أن تكون كائنًا.")
        known = {item["field_id"]: item for item in self.catalog.profile_questions}
        unknown = set(answers.keys()) - set(known.keys())
        if unknown:
            raise InvalidInput("تحتوي الإجابات على سؤال غير معروف.")
        cleaned = {}
        for field_id, option_id in answers.items():
            if option_id is None or option_id == "":
                continue
            if not isinstance(option_id, str):
                raise InvalidInput("اختر إجابة من الخيارات المتاحة.")
            allowed = {option["id"] for option in known[field_id]["options"]}
            if option_id not in allowed:
                raise InvalidInput("إحدى الإجابات لا تطابق خيارات السؤال.")
            cleaned[field_id] = option_id

        now = self._now()
        with closing(self.store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            player_id, room_id = self._actor(db, invite_code, token)
            if _has_active_match(db, room_id):
                raise RoomNotOpen("لا يمكن تعديل ملف الشخصية أثناء مباراة جارية.")
            existing = self._profile_row(db, player_id)
            merged = json.loads(existing["answers_json"]) if existing else {}
            for field_id in answers:
                if answers[field_id] in (None, ""):
                    merged.pop(field_id, None)
                else:
                    merged[field_id] = cleaned[field_id]
            count = len(merged)
            version = (existing["version"] + 1) if existing else 1
            db.execute(
                "INSERT INTO player_profiles (player_id, answers_json, completed_count, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(player_id) DO UPDATE SET answers_json = excluded.answers_json, "
                "completed_count = excluded.completed_count, version = excluded.version, updated_at = excluded.updated_at",
                (player_id, self._json(merged), count, version,
                 existing["updated_at"] if existing else now, now),
            )
            db.commit()
        return self.get_profile(invite_code, token)

    def _players_and_profiles(self, db, room_id: str):
        rows = db.execute(
            "SELECT id, seat, display_name, ready, last_seen "
            "FROM players WHERE room_id = ? ORDER BY seat", (room_id,)
        ).fetchall()
        if not 3 <= len(rows) <= 4:
            raise MatchConflict("تحتاج المباراة إلى ثلاثة أو أربعة لاعبين.")
        now = self._now()
        for row in rows:
            if not row["ready"] or now - row["last_seen"] > 25:
                raise MatchConflict("يجب أن يكون جميع اللاعبين متصلين ومستعدين.")
        players = [{
            "id": row["id"], "seat": row["seat"],
            "display_name": row["display_name"],
        } for row in rows]
        profiles = {}
        for player in players:
            row = self._profile_row(db, player["id"])
            if row is None:
                raise MatchConflict("أكملوا ملفات الشخصية الـ21 قبل بدء المباراة.")
            answers = json.loads(row["answers_json"])
            if len(answers) != 21 or set(answers.keys()) != set(self.catalog.profile_fields):
                raise MatchConflict("يجب أن يجيب كل لاعب عن الأسئلة الشخصية الـ21.")
            profiles[player["id"]] = answers
        return players, profiles

    def start_match(self, invite_code: str, token: Optional[str]) -> Dict[str, Any]:
        now = self._now()
        match_id = uuid.uuid4().hex
        seed = secrets.randbits(31)
        with closing(self.store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            _, room_id = self._actor(db, invite_code, token)
            room = db.execute(
                "SELECT id FROM rooms WHERE id = ? AND invite_code = ?",
                (room_id, _normalize_code(invite_code)),
            ).fetchone()
            if room is None:
                raise NotFound("الغرفة غير موجودة.")
            active = db.execute(
                "SELECT id FROM matches WHERE room_id = ? AND status = 'active' LIMIT 1",
                (room_id,),
            ).fetchone()
            if active:
                db.commit()
                return self.get_match(invite_code, token)
            players, profiles = self._players_and_profiles(db, room_id)
            state = {
                "phase_index": 0,
                "phase": PHASES[0],
                "current_task_index": 0,
                "next_task_sequence": 0,
                "tasks": [],
                "revealed_fields": {},
                "profile_field_counts": {},
                "used_question_ids": [],
                "used_template_ids": [],
                "target_counts": {},
                "last_target_id": None,
                "pair_stats": {},
                "last_result": None,
                "created_at": now,
            }
            try:
                state["tasks"] = build_phase_tasks(
                    PHASES[0], players, profiles, state, seed, self.catalog
                )
                self._assign_task_sequences(state)
            except (MatchPlanningError, ContentSelectionError) as exc:
                raise MatchConflict(str(exc))
            self._activate_task(state, now)
            db.execute(
                "INSERT INTO matches (id, room_id, status, phase, state_json, seed, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
                (match_id, room_id, PHASES[0], self._json(state), seed, now, now),
            )
            for player in players:
                db.execute(
                    "INSERT INTO match_players (match_id, player_id, seat, display_name, score) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (match_id, player["id"], player["seat"], player["display_name"]),
                )
            db.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
            db.commit()
        return self.get_match(invite_code, token)

    @staticmethod
    def _assign_task_sequences(state: Dict[str, Any]) -> None:
        """Give every task a match-wide key; local phase indexes restart at zero."""
        start = int(state.get("next_task_sequence", 0))
        for offset, task in enumerate(state["tasks"]):
            task["sequence_no"] = start + offset
        state["next_task_sequence"] = start + len(state["tasks"])

    def _activate_task(self, state: Dict[str, Any], now: int) -> None:
        task = state["tasks"][state["current_task_index"]]
        task["status"] = "active"
        if task["kind"] == "player_question":
            task.setdefault("subphase", "answer")
            seconds = self.timings["player_answer"] if task["subphase"] == "answer" else self.timings["player_guess"]
        elif task["kind"] == "live_choice":
            task.setdefault("subphase", "answer")
            seconds = self.timings["live_answer"] if task["subphase"] == "answer" else self.timings["live_guess"]
        elif task["kind"] == "mystery":
            task.setdefault("clue_level", 1)
            seconds = self.timings["mystery"]
        elif task["kind"] == "knowledge":
            seconds = self.timings["knowledge"]
        else:
            seconds = self.timings["finale"]
        task["deadline_at"] = now + seconds

    def _current_task(self, state: Dict[str, Any]) -> Dict[str, Any]:
        index = state["current_task_index"]
        if index < 0 or index >= len(state["tasks"]):
            raise StoreError("مؤشر الجولة المحفوظ غير صالح.")
        return state["tasks"][index]

    def _submissions(self, db, match_id: str, task_index: int,
                     kind: Optional[str] = None, attempt_no: Optional[int] = None):
        query = (
            "SELECT player_id, kind, attempt_no, payload_json FROM match_submissions "
            "WHERE match_id = ? AND task_index = ?"
        )
        values: List[Any] = [match_id, task_index]
        if kind is not None:
            query += " AND kind = ?"
            values.append(kind)
        if attempt_no is not None:
            query += " AND attempt_no = ?"
            values.append(attempt_no)
        query += " ORDER BY created_at, player_id"
        return db.execute(query, values).fetchall()

    def _has_submission(self, db, match_id: str, task_index: int,
                         player_id: str, kind: str, attempt_no: int = 0) -> bool:
        row = db.execute(
            "SELECT 1 FROM match_submissions WHERE match_id = ? AND task_index = ? "
            "AND player_id = ? AND kind = ? AND attempt_no = ?",
            (match_id, task_index, player_id, kind, attempt_no),
        ).fetchone()
        return row is not None

    def _save_submission(self, db, match_id: str, task_index: int,
                         player_id: str, kind: str, attempt_no: int,
                         value: Any, now: int) -> None:
        payload = {"value": value}
        existing = db.execute(
            "SELECT payload_json FROM match_submissions WHERE match_id = ? AND task_index = ? "
            "AND player_id = ? AND kind = ? AND attempt_no = ?",
            (match_id, task_index, player_id, kind, attempt_no),
        ).fetchone()
        if existing:
            if json.loads(existing["payload_json"]) != payload:
                raise MatchConflict("تم تسجيل اختيار مختلف لهذا الدور؛ لا يمكن تغييره.")
            return
        db.execute(
            "INSERT INTO match_submissions (id, match_id, task_index, player_id, kind, attempt_no, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, match_id, task_index, player_id, kind,
             attempt_no, self._json(payload), now),
        )

    @staticmethod
    def _submission_value(row) -> Any:
        return json.loads(row["payload_json"]).get("value")

    def _eligible_submissions_complete(self, db, match_id: str, task_index: int,
                                       player_ids: Iterable[str], kind: str,
                                       attempt_no: int = 0) -> bool:
        return all(
            self._has_submission(db, match_id, task_index, player_id, kind, attempt_no)
            for player_id in player_ids
        )

    def _record_pair(self, state: Dict[str, Any], guesser: str,
                     target: str, correct: bool) -> None:
        if guesser == target:
            return
        key = guesser + "|" + target
        values = state.setdefault("pair_stats", {}).setdefault(key, [])
        values.append(bool(correct))
        del values[:-10]

    def _award(self, db, match_id: str, player_id: str, points: int) -> None:
        if points <= 0:
            return
        db.execute(
            "UPDATE match_players SET score = score + ?, correct_guesses = correct_guesses + 1 "
            "WHERE match_id = ? AND player_id = ?",
            (points, match_id, player_id),
        )

    def _name_map(self, db, match_id: str) -> Dict[str, str]:
        return {
            row["player_id"]: row["display_name"]
            for row in db.execute(
                "SELECT player_id, display_name FROM match_players WHERE match_id = ?",
                (match_id,),
            ).fetchall()
        }

    def _complete_task(self, db, match_id: str, state: Dict[str, Any],
                       task: Dict[str, Any], solution: Dict[str, Any], now: int) -> None:
        task["status"] = "reveal"
        task["result"] = solution
        task["deadline_at"] = now + self.timings["reveal"]
        state["last_result"] = solution
        db.execute(
            "UPDATE matches SET state_json = ?, updated_at = ? WHERE id = ?",
            (self._json(state), now, match_id),
        )

    def _resolve_knowledge(self, db, match_id: str, state: Dict[str, Any],
                           task: Dict[str, Any], task_index: int, now: int) -> None:
        private = task["private"]
        expected = private["correct_option_id"]
        names = self._name_map(db, match_id)
        changes = []
        rows = self._submissions(db, match_id, task_index, "guess", 0)
        for row in rows:
            correct = self._submission_value(row) == expected
            self._record_pair(state, row["player_id"], private["target_player_id"], correct)
            if correct:
                self._award(db, match_id, row["player_id"], 2)
                changes.append({"player_id": row["player_id"], "player_name": names[row["player_id"]], "points": 2})
        solution = {
            "type": "knowledge",
            "target_name": task["public"]["target_name"],
            "question": task["public"]["question"]["text_ar"],
            "answer": private["correct_answer_text"],
            "score_changes": changes,
        }
        self._complete_task(db, match_id, state, task, solution, now)

    def _resolve_mystery(self, db, match_id: str, state: Dict[str, Any],
                         task: Dict[str, Any], task_index: int, now: int) -> None:
        mystery = task["private"]["mystery"]
        target = mystery["target_player_id"]
        names = self._name_map(db, match_id)
        earliest: Dict[str, int] = {}
        for clue_level in (1, 2, 3):
            for row in self._submissions(db, match_id, task_index, "guess", clue_level):
                if self._submission_value(row) == target:
                    earliest[row["player_id"]] = min(clue_level, earliest.get(row["player_id"], 4))
        changes = []
        all_guessers = task["eligible_guesser_ids"]
        for player_id in all_guessers:
            level = earliest.get(player_id)
            correct = level is not None
            self._record_pair(state, player_id, target, correct)
            if level is not None:
                points = 4 - level
                self._award(db, match_id, player_id, points)
                changes.append({"player_id": player_id, "player_name": names[player_id], "points": points, "clue_level": level})
        solution = {
            "type": "mystery",
            "target_name": names[target],
            "title_ar": mystery["title_ar"],
            "story_ar": mystery["story_ar"],
            "clues": [{"level": clue["level"], "text_ar": clue["text_ar"]} for clue in mystery["clues"]],
            "facts": list(mystery["private_answers"].values()),
            "score_changes": changes,
        }
        self._complete_task(db, match_id, state, task, solution, now)

    def _resolve_player_question(self, db, match_id: str, state: Dict[str, Any],
                                 task: Dict[str, Any], task_index: int, now: int) -> None:
        private = task["private"]
        target = private["target_player_id"]
        names = self._name_map(db, match_id)
        answer_rows = self._submissions(db, match_id, task_index, "answer", 0)
        answer = self._submission_value(answer_rows[0]) if answer_rows else None
        answer_option = None
        if answer is not None:
            answer_option = next((item for item in task["public"]["question"]["options"] if item["id"] == answer), None)
        changes = []
        for row in self._submissions(db, match_id, task_index, "guess", 0):
            correct = answer is not None and self._submission_value(row) == answer
            self._record_pair(state, row["player_id"], target, correct)
            if correct:
                self._award(db, match_id, row["player_id"], 2)
                changes.append({"player_id": row["player_id"], "player_name": names[row["player_id"]], "points": 2})
        solution = {
            "type": "player_question",
            "asker_name": task["public"]["asker_name"],
            "target_name": names[target],
            "question": task["public"]["question"]["text_ar"],
            "answer": answer_option["text_ar"] if answer_option else None,
            "skipped": answer is None,
            "score_changes": changes,
        }
        self._complete_task(db, match_id, state, task, solution, now)

    def _resolve_live_choice(self, db, match_id: str, state: Dict[str, Any],
                             task: Dict[str, Any], task_index: int, now: int) -> None:
        names = self._name_map(db, match_id)
        answer_rows = self._submissions(db, match_id, task_index, "answer", 0)
        answers = {}
        for row in answer_rows:
            answers[row["player_id"]] = self._submission_value(row)
        option_map = {item["id"]: item["text_ar"] for item in task["public"]["question"]["options"]}
        result_rows = []
        for target in task["private"]["target_order"]:
            answer_id = answers.get(target)
            if answer_id not in option_map:
                result_rows.append({"answer": None, "owner_name": names[target], "score_changes": []})
                continue
            changes = []
            for row in self._submissions(db, match_id, task_index, "guess", task["private"]["target_order"].index(target) + 1):
                guess = self._submission_value(row)
                correct = guess == target
                self._record_pair(state, row["player_id"], target, correct)
                if correct:
                    self._award(db, match_id, row["player_id"], 2)
                    changes.append({"player_name": names[row["player_id"]], "points": 2})
            result_rows.append({
                "answer": option_map[answer_id],
                "owner_name": names[target],
                "score_changes": changes,
            })
        solution = {
            "type": "live_choice",
            "question": task["public"]["question"]["text_ar"],
            "answers": result_rows,
        }
        self._complete_task(db, match_id, state, task, solution, now)

    def _resolve_finale(self, db, match_id: str, state: Dict[str, Any],
                        task: Dict[str, Any], task_index: int, now: int) -> None:
        target = task["private"]["target_player_id"]
        names = self._name_map(db, match_id)
        changes = []
        for row in self._submissions(db, match_id, task_index, "guess", 0):
            correct = self._submission_value(row) == target
            self._record_pair(state, row["player_id"], target, correct)
            if correct:
                self._award(db, match_id, row["player_id"], 3)
                changes.append({"player_id": row["player_id"], "player_name": names[row["player_id"]], "points": 3})
        solution = {
            "type": "finale",
            "target_name": names[target],
            "facts": task["private"]["facts"],
            "score_changes": changes,
        }
        self._complete_task(db, match_id, state, task, solution, now)

    def _advance_mystery_clue(self, db, match_id: str, state: Dict[str, Any],
                              task: Dict[str, Any], task_index: int, now: int) -> None:
        level = task["clue_level"]
        rows = self._submissions(db, match_id, task_index, "guess", level)
        target = task["private"]["mystery"]["target_player_id"]
        success = any(self._submission_value(row) == target for row in rows)
        all_done = self._eligible_submissions_complete(
            db, match_id, task_index, task["eligible_guesser_ids"], "guess", level
        )
        if all_done or now >= task["deadline_at"]:
            if success or level >= 3:
                self._resolve_mystery(db, match_id, state, task, task_index, now)
            else:
                task["clue_level"] = level + 1
                task["deadline_at"] = now + self.timings["mystery"]

    def _advance_live_target(self, db, match_id: str, state: Dict[str, Any],
                             task: Dict[str, Any], task_index: int, now: int) -> None:
        order = task["private"]["target_order"]
        while task["live_target_index"] < len(order):
            target_index = task["live_target_index"]
            target = order[target_index]
            answer = self._find_submission_value(
                self._submissions(db, match_id, task_index, "answer", 0),
                target, "answer", 0
            )
            if answer is None:
                task["live_target_index"] = target_index + 1
                continue
            eligible = [player_id for player_id in order if player_id != target]
            attempt = target_index + 1
            done = self._eligible_submissions_complete(
                db, match_id, task_index, eligible, "guess", attempt
            )
            if not done and now < task["deadline_at"]:
                return
            task["live_target_index"] = target_index + 1
            if task["live_target_index"] < len(order):
                task["deadline_at"] = now + self.timings["live_guess"]
        self._resolve_live_choice(db, match_id, state, task, task_index, now)

    def _switch_player_question_to_guess(self, task: Dict[str, Any], now: int) -> None:
        task["subphase"] = "guess"
        task["deadline_at"] = now + self.timings["player_guess"]

    def _switch_live_to_guess(self, task: Dict[str, Any], now: int) -> None:
        task["subphase"] = "guess"
        task["live_target_index"] = 0
        task["deadline_at"] = now + self.timings["live_guess"]

    def _finish_phase_or_advance(self, db, match_id: str, match_row,
                                 state: Dict[str, Any], now: int) -> None:
        task_index = state["current_task_index"]
        task = self._current_task(state)
        task["status"] = "completed"
        next_index = task_index + 1
        if next_index < len(state["tasks"]):
            state["current_task_index"] = next_index
            self._activate_task(state, now)
        else:
            next_phase_index = state["phase_index"] + 1
            if next_phase_index >= len(PHASES):
                state["status"] = "completed"
                state["phase"] = "completed"
                state["finished_at"] = now
                state["final_standings"] = self._standings(db, match_id)
                db.execute(
                    "UPDATE matches SET status = 'completed', phase = 'completed', state_json = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                    (self._json(state), now, now, match_id),
                )
                return
            players = [
                {"id": row["player_id"], "seat": row["seat"], "display_name": row["display_name"]}
                for row in db.execute(
                    "SELECT player_id, seat, display_name FROM match_players WHERE match_id = ? ORDER BY seat",
                    (match_id,),
                ).fetchall()
            ]
            profiles = {}
            for player in players:
                row = self._profile_row(db, player["id"])
                if row is None:
                    raise StoreError("ملف لاعب المباراة غير موجود.")
                profiles[player["id"]] = json.loads(row["answers_json"])
            phase = PHASES[next_phase_index]
            try:
                tasks = build_phase_tasks(
                    phase, players, profiles, state, match_row["seed"], self.catalog
                )
            except (MatchPlanningError, ContentSelectionError) as exc:
                raise StoreError("تعذر تجهيز المرحلة التالية: %s" % str(exc))
            state["phase_index"] = next_phase_index
            state["phase"] = phase
            state["current_task_index"] = 0
            state["tasks"] = tasks
            self._assign_task_sequences(state)
            self._activate_task(state, now)
        db.execute(
            "UPDATE matches SET phase = ?, state_json = ?, updated_at = ? WHERE id = ?",
            (state["phase"], self._json(state), now, match_id),
        )

    def _tick(self, db, match_row, state: Dict[str, Any], now: int) -> None:
        if match_row["status"] != "active":
            return
        task = self._current_task(state)
        if task["status"] == "reveal":
            if now >= task["deadline_at"]:
                self._finish_phase_or_advance(db, match_row["id"], match_row, state, now)
            return
        if task["status"] != "active" or now < task["deadline_at"]:
            return
        sequence_no = task["sequence_no"]
        kind = task["kind"]
        if kind == "knowledge":
            self._resolve_knowledge(db, match_row["id"], state, task, sequence_no, now)
        elif kind == "mystery":
            self._advance_mystery_clue(db, match_row["id"], state, task, sequence_no, now)
        elif kind == "player_question":
            self._resolve_player_question(db, match_row["id"], state, task, sequence_no, now)
        elif kind == "live_choice":
            if task["subphase"] == "answer":
                self._switch_live_to_guess(task, now)
                self._advance_live_target(db, match_row["id"], state, task, sequence_no, now)
            else:
                self._advance_live_target(db, match_row["id"], state, task, sequence_no, now)
        elif kind == "finale":
            self._resolve_finale(db, match_row["id"], state, task, sequence_no, now)
        db.execute(
            "UPDATE matches SET state_json = ?, updated_at = ? WHERE id = ?",
            (self._json(state), now, match_row["id"]),
        )

    def get_match(self, invite_code: str, token: Optional[str]) -> Dict[str, Any]:
        now = self._now()
        with closing(self.store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            viewer_id, room_id = self._actor(db, invite_code, token)
            match_row = db.execute(
                "SELECT * FROM matches WHERE room_id = ? ORDER BY created_at DESC LIMIT 1",
                (room_id,),
            ).fetchone()
            if match_row is None:
                db.commit()
                return {"ok": True, "match": None}
            state = self._load(match_row["state_json"])
            if match_row["status"] == "active":
                self._tick(db, match_row, state, now)
                refreshed = db.execute("SELECT * FROM matches WHERE id = ?", (match_row["id"],)).fetchone()
                match_row = refreshed
                state = self._load(match_row["state_json"])
            players = db.execute(
                "SELECT player_id, seat, display_name, score, correct_guesses "
                "FROM match_players WHERE match_id = ? ORDER BY seat",
                (match_row["id"],),
            ).fetchall()
            current_task = self._current_task(state) if match_row["status"] == "active" else None
            submissions = self._submissions(
                db, match_row["id"], current_task["sequence_no"]
            ) if current_task else []
            db.commit()
        return {
            "ok": True,
            "match": self._public_state(match_row, state, players, submissions, viewer_id, now),
        }

    def _public_state(self, match_row, state, players, submissions,
                      viewer_id: str, now: int) -> Dict[str, Any]:
        scoreboard = [{
            "player_id": row["player_id"],
            "name": row["display_name"],
            "seat": row["seat"],
            "score": row["score"],
            "correct_guesses": row["correct_guesses"],
        } for row in players]
        scoreboard.sort(key=lambda item: (-item["score"], -item["correct_guesses"], item["seat"]))
        base = {
            "id": match_row["id"],
            "status": match_row["status"],
            "phase": state.get("phase"),
            "phase_label": PHASE_LABELS.get(state.get("phase"), "النتيجة النهائية"),
            "phase_number": int(state.get("phase_index", 0)) + 1,
            "phase_count": len(PHASES),
            "scoreboard": scoreboard,
            "created_at": match_row["created_at"],
            "finished_at": state.get("finished_at"),
        }
        if match_row["status"] == "completed":
            base["final_standings"] = state.get("final_standings", scoreboard)
            return base
        task = self._current_task(state)
        index = state["current_task_index"]
        kind = task["kind"]
        task_public = {
            "kind": kind,
            "status": task["status"],
            "subphase": task.get("subphase", "guess"),
            "round_in_phase": index + 1,
            "rounds_in_phase": len(state["tasks"]),
            "task_sequence": task["sequence_no"],
        }
        if task["status"] == "reveal":
            task_public["result"] = task.get("result")
            task_public["remaining_seconds"] = max(0, task["deadline_at"] - now)
        else:
            task_public["deadline_at"] = task["deadline_at"]
            task_public["remaining_seconds"] = max(0, task["deadline_at"] - now)
            if kind == "mystery":
                mystery = task["private"]["mystery"]
                clue_view = ContentEngine.public_mystery(mystery, task["clue_level"])
                task_public["content"] = {
                    "title_ar": clue_view["title_ar"],
                    "story_ar": clue_view["story_ar"],
                    "difficulty": clue_view["difficulty"],
                    "revealed_clues": clue_view["revealed_clues"],
                }
            elif kind in ("knowledge", "player_question", "finale"):
                task_public["content"] = task["public"]
            elif kind == "live_choice":
                task_public["content"] = {"question": task["public"]["question"]}
                if task["subphase"] == "guess":
                    target_index = task["live_target_index"]
                    target_id = task["private"]["target_order"][target_index]
                    answer = self._find_submission_value(
                        submissions, target_id, "answer", 0
                    )
                    option = next(
                        (item for item in task["public"]["question"]["options"] if item["id"] == answer),
                        None,
                    )
                    task_public["anonymous_answer"] = option["text_ar"] if option else None
                    task_public["live_guess_number"] = target_index + 1
                    task_public["live_guess_total"] = len(task["private"]["target_order"])
        task_public["my_state"] = self._viewer_action_state(
            task, viewer_id, task["sequence_no"], submissions
        )
        base["current_task"] = task_public
        base["last_result"] = state.get("last_result")
        return base

    @staticmethod
    def _find_submission_value(rows, player_id, kind, attempt_no):
        for row in rows:
            if (row["player_id"] == player_id and row["kind"] == kind
                    and row["attempt_no"] == attempt_no):
                return json.loads(row["payload_json"]).get("value")
        return None

    def _viewer_action_state(self, task, viewer_id, task_index, rows) -> Dict[str, Any]:
        kind = task["kind"]
        subphase = task.get("subphase", "guess")
        result = {"can_answer": False, "can_guess": False, "has_submitted": False}
        if task["status"] != "active":
            return result
        if kind == "knowledge":
            result["can_guess"] = viewer_id in task["eligible_guesser_ids"]
            result["has_submitted"] = self._find_submission_value(rows, viewer_id, "guess", 0) is not None
        elif kind == "mystery":
            result["can_guess"] = viewer_id in task["eligible_guesser_ids"]
            level = task["clue_level"]
            result["has_submitted"] = any(
                row["player_id"] == viewer_id and row["kind"] == "guess" and row["attempt_no"] == level
                for row in rows
            )
        elif kind == "player_question":
            target = task["private"]["target_player_id"]
            if subphase == "answer":
                result["is_target"] = viewer_id == target
                result["can_answer"] = viewer_id == target
                result["has_submitted"] = self._find_submission_value(rows, viewer_id, "answer", 0) is not None
            else:
                result["can_guess"] = viewer_id in task["eligible_guesser_ids"]
                result["has_submitted"] = self._find_submission_value(rows, viewer_id, "guess", 0) is not None
        elif kind == "live_choice":
            if subphase == "answer":
                result["can_answer"] = True
                result["has_submitted"] = self._find_submission_value(rows, viewer_id, "answer", 0) is not None
            else:
                target = task["private"]["target_order"][task["live_target_index"]]
                attempt = task["live_target_index"] + 1
                result["is_answer_owner"] = viewer_id == target
                result["can_guess"] = viewer_id != target
                result["has_submitted"] = self._find_submission_value(rows, viewer_id, "guess", attempt) is not None
        elif kind == "finale":
            result["can_guess"] = viewer_id in task["eligible_guesser_ids"]
            result["has_submitted"] = self._find_submission_value(rows, viewer_id, "guess", 0) is not None
        return result

    def _advance_after_submission(self, db, match_row, state, task, task_index, now):
        match_id = match_row["id"]
        kind = task["kind"]
        if kind == "knowledge":
            if self._eligible_submissions_complete(db, match_id, task_index, task["eligible_guesser_ids"], "guess", 0):
                self._resolve_knowledge(db, match_id, state, task, task_index, now)
        elif kind == "mystery":
            self._advance_mystery_clue(db, match_id, state, task, task_index, now)
        elif kind == "player_question":
            if task["subphase"] == "answer":
                self._switch_player_question_to_guess(task, now)
            elif self._eligible_submissions_complete(db, match_id, task_index, task["eligible_guesser_ids"], "guess", 0):
                self._resolve_player_question(db, match_id, state, task, task_index, now)
        elif kind == "live_choice":
            if task["subphase"] == "answer":
                if self._eligible_submissions_complete(db, match_id, task_index, task["private"]["target_order"], "answer", 0):
                    self._switch_live_to_guess(task, now)
            else:
                target_index = task["live_target_index"]
                target = task["private"]["target_order"][target_index]
                eligible = [player_id for player_id in task["private"]["target_order"] if player_id != target]
                attempt = target_index + 1
                if self._eligible_submissions_complete(db, match_id, task_index, eligible, "guess", attempt):
                    self._advance_live_target(db, match_id, state, task, task_index, now)
        elif kind == "finale":
            if self._eligible_submissions_complete(db, match_id, task_index, task["eligible_guesser_ids"], "guess", 0):
                self._resolve_finale(db, match_id, state, task, task_index, now)

    def submit_action(self, invite_code: str, token: Optional[str],
                      action: Any, value: Any,
                      expected_task_sequence: Optional[int] = None) -> Dict[str, Any]:
        if action not in ("answer", "guess"):
            raise InvalidInput("نوع المشاركة غير معروف.")
        if not isinstance(value, str) or not value:
            raise InvalidInput("اختر إجابة صالحة.")
        now = self._now()
        with closing(self.store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            viewer_id, room_id = self._actor(db, invite_code, token)
            match_row = db.execute(
                "SELECT * FROM matches WHERE room_id = ? AND status = 'active'",
                (room_id,),
            ).fetchone()
            if match_row is None:
                raise MatchConflict("لا توجد مباراة جارية في هذه الغرفة.")
            state = self._load(match_row["state_json"])
            self._tick(db, match_row, state, now)
            refreshed = db.execute("SELECT * FROM matches WHERE id = ?", (match_row["id"],)).fetchone()
            if refreshed["status"] != "active":
                raise MatchConflict("انتهت المباراة أو الجولة الحالية.")
            state = self._load(refreshed["state_json"])
            task = self._current_task(state)
            if expected_task_sequence is not None:
                if isinstance(expected_task_sequence, bool) or not isinstance(expected_task_sequence, int):
                    raise InvalidInput("رقم الجولة المرسل غير صالح.")
                if expected_task_sequence != task["sequence_no"]:
                    raise MatchConflict("انتهت هذه الجولة. حدّث شاشة المباراة قبل إرسال اختيارك.")
            if task["status"] != "active":
                raise MatchConflict("الجولة في مرحلة كشف النتيجة.")
            index = state["current_task_index"]
            sequence_no = task["sequence_no"]
            kind = task["kind"]
            attempt_no = 0

            if kind == "knowledge":
                if action != "guess" or viewer_id not in task["eligible_guesser_ids"]:
                    raise MatchConflict("هذا الدور مخصص للاعبين الذين يخمّنون.")
                allowed = {item["id"] for item in task["public"]["question"]["options"]}
                if value not in allowed:
                    raise InvalidInput("اختر أحد خيارات السؤال.")
            elif kind == "mystery":
                if action != "guess" or viewer_id not in task["eligible_guesser_ids"]:
                    raise MatchConflict("هذا الدور مخصص لتخمين صاحب الرسالة.")
                if value not in self._name_map(db, match_row["id"]):
                    raise InvalidInput("اختر لاعبًا من الغرفة.")
                if value == viewer_id:
                    raise InvalidInput("لا يمكنك تخمين نفسك.")
                attempt_no = task["clue_level"]
            elif kind == "player_question":
                target = task["private"]["target_player_id"]
                if task["subphase"] == "answer":
                    if action != "answer" or viewer_id != target:
                        raise MatchConflict("بانتظار إجابة اللاعب الذي اختاره النظام.")
                    allowed = {item["id"] for item in task["public"]["question"]["options"]}
                    if value not in allowed:
                        raise InvalidInput("اختر أحد خيارات السؤال.")
                else:
                    if action != "guess" or viewer_id not in task["eligible_guesser_ids"]:
                        raise MatchConflict("هذا الدور مخصص لتخمين إجابة اللاعب.")
                    allowed = {item["id"] for item in task["public"]["question"]["options"]}
                    if value not in allowed:
                        raise InvalidInput("اختر أحد خيارات السؤال.")
            elif kind == "live_choice":
                if task["subphase"] == "answer":
                    if action != "answer":
                        raise MatchConflict("هذه المرحلة تتطلب اختيار إجابتك أولًا.")
                    allowed = {item["id"] for item in task["public"]["question"]["options"]}
                    if value not in allowed:
                        raise InvalidInput("اختر أحد خيارات السؤال.")
                else:
                    target = task["private"]["target_order"][task["live_target_index"]]
                    if action != "guess" or viewer_id == target:
                        raise MatchConflict("صاحب الإجابة لا يخمّن نفسه.")
                    if value not in task["private"]["target_order"]:
                        raise InvalidInput("اختر لاعبًا من الغرفة.")
                    if value == viewer_id:
                        raise InvalidInput("لا يمكنك تخمين نفسك.")
                    attempt_no = task["live_target_index"] + 1
            elif kind == "finale":
                if action != "guess" or viewer_id not in task["eligible_guesser_ids"]:
                    raise MatchConflict("هذا الدور مخصص لتخمين صاحب الاختيارات.")
                if value not in self._name_map(db, match_row["id"]):
                    raise InvalidInput("اختر لاعبًا من الغرفة.")
                if value == viewer_id:
                    raise InvalidInput("لا يمكنك تخمين نفسك.")

            self._save_submission(db, match_row["id"], sequence_no, viewer_id,
                                  action, attempt_no, value, now)
            if action == "answer":
                db.execute(
                    "UPDATE match_players SET answered_count = answered_count + 1 "
                    "WHERE match_id = ? AND player_id = ?",
                    (match_row["id"], viewer_id),
                )
            self._advance_after_submission(db, match_row, state, task, sequence_no, now)
            db.execute(
                "UPDATE matches SET state_json = ?, phase = ?, updated_at = ? WHERE id = ?",
                (self._json(state), state["phase"], now, match_row["id"]),
            )
            db.commit()
        return self.get_match(invite_code, token)

    def _standings(self, db, match_id: str) -> List[Dict[str, Any]]:
        rows = db.execute(
            "SELECT player_id, seat, display_name, score, correct_guesses, answered_count "
            "FROM match_players WHERE match_id = ? ORDER BY score DESC, correct_guesses DESC, seat ASC",
            (match_id,),
        ).fetchall()
        return [{
            "player_id": row["player_id"],
            "name": row["display_name"],
            "seat": row["seat"],
            "score": row["score"],
            "correct_guesses": row["correct_guesses"],
            "answered_count": row["answered_count"],
        } for row in rows]
