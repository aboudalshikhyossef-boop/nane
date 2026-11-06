import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from server.content import load_catalogs
from server.game.service import MatchConflict, MatchService
from server.store import InvalidInput, NaneStore, RoomNotOpen


class FakeClock:
    def __init__(self):
        self.value = int(time.time())

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class MatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = NaneStore(str(Path(self.temp.name) / "nane-game.sqlite3"))
        self.catalog = load_catalogs()
        self.clock = FakeClock()
        self.service = MatchService(
            self.store,
            catalog=self.catalog,
            clock=self.clock,
            timings={
                "knowledge": 4, "mystery": 4, "player_answer": 4,
                "player_guess": 4, "live_answer": 4, "live_guess": 4,
                "finale": 4, "reveal": 1,
            },
        )
        self.players = []
        self.code = None

    def tearDown(self):
        self.temp.cleanup()

    def prepare_room(self, count=3, complete_profiles=True):
        created = self.store.create_room("لاعب 1")
        self.code = created["invite_code"]
        self.players = [{
            "id": created["player_id"],
            "token": created["session_token"],
            "name": "لاعب 1",
        }]
        for number in range(2, count + 1):
            joined = self.store.join_room(self.code, "لاعب %d" % number)
            self.players.append({
                "id": joined["player_id"],
                "token": joined["session_token"],
                "name": "لاعب %d" % number,
            })
        for player in self.players:
            self.store.set_ready(self.code, player["token"], True)
            if complete_profiles:
                answers = {
                    item["field_id"]: item["options"][0]["id"]
                    for item in self.catalog.profile_questions
                }
                self.service.save_profile(self.code, player["token"], answers)

    def raw_match(self):
        with closing(self.store._connect()) as db:
            row = db.execute(
                "SELECT * FROM matches WHERE room_id = (SELECT id FROM rooms WHERE invite_code = ?) "
                "ORDER BY created_at DESC LIMIT 1", (self.code,)
            ).fetchone()
            return dict(row) if row else None

    def raw_state(self):
        row = self.raw_match()
        return json.loads(row["state_json"]) if row else None

    def call_as(self, player_index, action, value):
        return self.service.submit_action(
            self.code, self.players[player_index]["token"], action, value
        )

    def test_match_state_is_restored_from_database_by_a_new_service_instance(self):
        self.prepare_room(3)
        started = self.service.start_match(self.code, self.players[0]["token"])["match"]
        restored_service = MatchService(
            self.store, catalog=self.catalog, clock=self.clock,
            timings={"knowledge": 4, "mystery": 4, "player_answer": 4,
                     "player_guess": 4, "live_answer": 4, "live_guess": 4,
                     "finale": 4, "reveal": 1},
        )
        restored = restored_service.get_match(self.code, self.players[1]["token"])["match"]
        self.assertEqual(restored["id"], started["id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["phase"], "knowledge")

    def test_match_task_sequence_is_unique_across_phase_boundaries(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        self._finish_current_phase("knowledge")
        self._finish_current_phase("mystery")
        self._finish_current_phase("player_question")
        state = self.raw_state()
        self.assertEqual(state["phase"], "live_choice")
        current = state["tasks"][state["current_task_index"]]
        self.assertGreater(current["sequence_no"], 20)
        self.assertEqual(len({task["sequence_no"] for task in state["tasks"]}), len(state["tasks"]))

    def test_profile_requires_catalogued_options_and_is_private_to_owner(self):
        self.prepare_room(3, complete_profiles=False)
        first = self.players[0]
        answers = {"favorite_color": self.catalog.profile_questions[0]["options"][0]["id"]}
        saved = self.service.save_profile(self.code, first["token"], answers)
        self.assertEqual(saved["profile"]["completed_count"], 1)
        self.assertFalse(saved["profile"]["is_complete"])
        self.assertEqual(saved["profile"]["answers"], answers)
        with self.assertRaises(InvalidInput):
            self.service.save_profile(self.code, first["token"], {"favorite_color": "not-an-option"})
        with self.assertRaises(InvalidInput):
            self.service.save_profile(self.code, first["token"], {"not-a-field": "o1"})

    def test_match_cannot_start_until_each_player_has_21_answers(self):
        self.prepare_room(3, complete_profiles=False)
        with self.assertRaises(MatchConflict):
            self.service.start_match(self.code, self.players[0]["token"])

    def test_three_player_match_is_prepared_with_five_phases_and_expected_task_counts(self):
        self.prepare_room(3)
        result = self.service.start_match(self.code, self.players[0]["token"])
        match = result["match"]
        self.assertEqual(match["status"], "active")
        self.assertEqual(match["phase"], "knowledge")
        state = self.raw_state()
        self.assertEqual(len(state["tasks"]), 9)
        self.assertEqual({task["kind"] for task in state["tasks"]}, {"knowledge"})
        self.assertEqual(len(self.catalog.profile_fields), 21)

    def test_four_player_match_has_balanced_phase_task_counts(self):
        self.prepare_room(4)
        self.service.start_match(self.code, self.players[0]["token"])
        state = self.raw_state()
        self.assertEqual(len(state["tasks"]), 12)
        targets = [task["private"]["target_player_id"] for task in state["tasks"]]
        self.assertEqual({targets.count(player["id"]) for player in self.players}, {3})

    def test_knowledge_question_public_state_does_not_contain_correct_answer_keys(self):
        self.prepare_room(3)
        state = self.service.start_match(self.code, self.players[0]["token"])["match"]
        raw = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("correct_option_id", raw)
        self.assertNotIn("field_id", raw)
        self.assertNotIn('"private"', raw)
        self.assertIn("content", state["current_task"])

    def test_knowledge_submissions_score_correct_guesses_and_reveal_answer(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        state = self.raw_state()
        task = state["tasks"][0]
        target = task["private"]["target_player_id"]
        correct = task["private"]["correct_option_id"]
        guessers = [p for p in self.players if p["id"] != target]
        for player in guessers:
            self.service.submit_action(self.code, player["token"], "guess", correct)
        state = self.raw_state()
        self.assertEqual(state["tasks"][0]["status"], "reveal")
        result = self.service.get_match(self.code, self.players[0]["token"])["match"]["current_task"]["result"]
        self.assertEqual(result["answer"], task["private"]["correct_answer_text"])
        standings = self.service.get_match(self.code, self.players[0]["token"])["match"]["scoreboard"]
        self.assertEqual(sum(item["score"] for item in standings), 4)

    def test_duplicate_action_is_idempotent_but_different_retry_is_rejected(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        task = self.raw_state()["tasks"][0]
        correct = task["private"]["correct_option_id"]
        guesser = next(p for p in self.players if p["id"] != task["private"]["target_player_id"])
        self.service.submit_action(self.code, guesser["token"], "guess", correct)
        self.service.submit_action(self.code, guesser["token"], "guess", correct)
        wrong = next(item["id"] for item in task["public"]["question"]["options"] if item["id"] != correct)
        with self.assertRaises(MatchConflict):
            self.service.submit_action(self.code, guesser["token"], "guess", wrong)

    def test_target_cannot_submit_a_guess_about_own_profile(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        task = self.raw_state()["tasks"][0]
        target_index = next(i for i,p in enumerate(self.players) if p["id"] == task["private"]["target_player_id"])
        with self.assertRaises(MatchConflict):
            self.call_as(target_index, "guess", task["private"]["correct_option_id"])

    def test_profile_is_locked_during_active_match_and_room_cannot_be_left(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        with self.assertRaises(RoomNotOpen):
            self.service.save_profile(self.code, self.players[0]["token"], {})
        with self.assertRaises(RoomNotOpen):
            self.store.leave_room(self.code, self.players[0]["token"])

    def test_mystery_public_projection_hides_target_and_unreleased_clues(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        # Finish the knowledge phase using correct choices, advancing reveal timers.
        self._finish_current_phase("knowledge")
        view = self.service.get_match(self.code, self.players[0]["token"])["match"]
        self.assertEqual(view["phase"], "mystery")
        raw = json.dumps(view["current_task"], ensure_ascii=False)
        self.assertNotIn("target_player_id", raw)
        self.assertNotIn("private_answers", raw)
        self.assertEqual(len(view["current_task"]["content"]["revealed_clues"]), 1)

    def _finish_current_phase(self, phase):
        guard = 0
        while guard < 100:
            guard += 1
            row = self.raw_match()
            if row is None or row["status"] != "active":
                return
            state = json.loads(row["state_json"])
            if state["phase"] != phase:
                return
            task_index = state["current_task_index"]
            task = state["tasks"][task_index]
            if task["status"] == "reveal":
                self.clock.advance(2)
                self.service.get_match(self.code, self.players[0]["token"])
                continue
            kind = task["kind"]
            if kind == "knowledge":
                target = task["private"]["target_player_id"]
                correct = task["private"]["correct_option_id"]
                for player in self.players:
                    if player["id"] != target:
                        self.service.submit_action(self.code, player["token"], "guess", correct)
            elif kind == "mystery":
                target = task["private"]["mystery"]["target_player_id"]
                wrong_id = next(p["id"] for p in self.players if p["id"] != target)
                eligible = [p for p in self.players if p["id"] != target]
                for index, player in enumerate(eligible):
                    self.service.submit_action(
                        self.code, player["token"], "guess",
                        target if index == 0 else wrong_id
                    )
            elif kind == "player_question":
                target_id = task["private"]["target_player_id"]
                target = next(p for p in self.players if p["id"] == target_id)
                if task["subphase"] == "answer":
                    option = task["public"]["question"]["options"][0]["id"]
                    self.service.submit_action(self.code, target["token"], "answer", option)
                else:
                    answer = task["private"]["answer_option_id"]
                    if answer is None:
                        with closing(self.store._connect()) as db:
                            row = db.execute(
                                "SELECT payload_json FROM match_submissions WHERE match_id = ? AND task_index = ? AND player_id = ? AND kind = 'answer'",
                                (row["id"], task_index, target_id)
                            ).fetchone()
                            answer = json.loads(row["payload_json"])["value"]
                    for player in self.players:
                        if player["id"] != target_id:
                            self.service.submit_action(self.code, player["token"], "guess", answer)
            elif kind == "live_choice":
                if task["subphase"] == "answer":
                    option = task["public"]["question"]["options"][0]["id"]
                    for player in self.players:
                        self.service.submit_action(self.code, player["token"], "answer", option)
                else:
                    target_id = task["private"]["target_order"][task["live_target_index"]]
                    for player in self.players:
                        if player["id"] != target_id:
                            self.service.submit_action(self.code, player["token"], "guess", target_id)
            elif kind == "finale":
                target_id = task["private"]["target_player_id"]
                for player in self.players:
                    if player["id"] != target_id:
                        self.service.submit_action(self.code, player["token"], "guess", target_id)
        self.fail("Phase did not finish within the task guard.")

    def test_player_question_target_is_hidden_until_reveal_and_answers_are_private(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        self._finish_current_phase("knowledge")
        self._finish_current_phase("mystery")
        self.assertEqual(self.raw_state()["phase"], "player_question")
        state = self.raw_state()
        task = state["tasks"][0]
        target_id = task["private"]["target_player_id"]
        view = self.service.get_match(self.code, self.players[0]["token"])["match"]
        public = json.dumps(view["current_task"], ensure_ascii=False)
        self.assertNotIn(target_id, public)
        target = next(p for p in self.players if p["id"] == target_id)
        option = task["public"]["question"]["options"][0]["id"]
        self.service.submit_action(self.code, target["token"], "answer", option)
        state = self.raw_state()
        task = state["tasks"][state["current_task_index"]]
        self.assertEqual(task["subphase"], "guess")
        view = self.service.get_match(self.code, self.players[0]["token"])["match"]
        raw = json.dumps(view["current_task"], ensure_ascii=False)
        self.assertNotIn("answer_option_id", raw)
        self.assertNotIn(target_id, raw)

    def test_full_three_player_match_finishes_all_five_phases_and_scores(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        phase_order = []
        sequence_numbers = []
        guard = 0
        while guard < 200:
            guard += 1
            row = self.raw_match()
            if row["status"] == "completed":
                break
            state = json.loads(row["state_json"])
            if not phase_order or phase_order[-1] != state["phase"]:
                phase_order.append(state["phase"])
            task = state["tasks"][state["current_task_index"]]
            if task["sequence_no"] not in sequence_numbers:
                sequence_numbers.append(task["sequence_no"])
            if task["status"] == "reveal":
                self.clock.advance(2)
                self.service.get_match(self.code, self.players[0]["token"])
                continue
            kind = task["kind"]
            if kind == "knowledge":
                target = task["private"]["target_player_id"]
                value = task["private"]["correct_option_id"]
                for player in self.players:
                    if player["id"] != target:
                        self.service.submit_action(self.code, player["token"], "guess", value)
            elif kind == "mystery":
                target = task["private"]["mystery"]["target_player_id"]
                eligible = [p for p in self.players if p["id"] != target]
                for index, player in enumerate(eligible):
                    value = target if index == 0 else next(p["id"] for p in eligible if p["id"] != player["id"])
                    self.service.submit_action(self.code, player["token"], "guess", value)
            elif kind == "player_question":
                target = next(p for p in self.players if p["id"] == task["private"]["target_player_id"])
                if task["subphase"] == "answer":
                    value = task["public"]["question"]["options"][0]["id"]
                    self.service.submit_action(self.code, target["token"], "answer", value)
                else:
                    with closing(self.store._connect()) as db:
                        answer = db.execute(
                            "SELECT payload_json FROM match_submissions WHERE match_id = ? AND task_index = ? AND player_id = ? AND kind = 'answer'",
                            (row["id"], task["sequence_no"], target["id"])
                        ).fetchone()
                    value = json.loads(answer["payload_json"])["value"]
                    for player in self.players:
                        latest = self.raw_state()
                        active_task = latest["tasks"][latest["current_task_index"]]
                        if active_task["status"] != "active" or active_task.get("subphase") != "guess":
                            break
                        if player["id"] != target["id"]:
                            self.service.submit_action(self.code, player["token"], "guess", value)
            elif kind == "live_choice":
                if task["subphase"] == "answer":
                    value = task["public"]["question"]["options"][0]["id"]
                    for player in self.players:
                        self.service.submit_action(self.code, player["token"], "answer", value)
                else:
                    target = task["private"]["target_order"][task["live_target_index"]]
                    for player in self.players:
                        latest = self.raw_state()
                        active_task = latest["tasks"][latest["current_task_index"]]
                        if active_task["status"] != "active":
                            break
                        if player["id"] != target:
                            self.service.submit_action(self.code, player["token"], "guess", target)
            elif kind == "finale":
                target = task["private"]["target_player_id"]
                for player in self.players:
                    latest = self.raw_state()
                    active_task = latest["tasks"][latest["current_task_index"]]
                    if active_task["status"] != "active":
                        break
                    if player["id"] != target:
                        self.service.submit_action(self.code, player["token"], "guess", target)
        self.assertLess(guard, 200)
        final = self.service.get_match(self.code, self.players[0]["token"])["match"]
        self.assertEqual(final["status"], "completed")
        self.assertEqual(phase_order, ["knowledge", "mystery", "player_question", "live_choice", "finale"])
        self.assertEqual(len(final["final_standings"]), 3)
        # Sequence numbers are unique across all phase-local task lists.
        self.assertEqual(len(set(sequence_numbers)), len(sequence_numbers))
        self.assertEqual(len(sequence_numbers), 30)
        self.assertGreater(sum(item["score"] for item in final["final_standings"]), 0)

    def test_completed_match_allows_next_match_but_requires_profile_readiness(self):
        self.prepare_room(3)
        self.service.start_match(self.code, self.players[0]["token"])
        # Complete current active match by allowing all timers to expire.
        guard = 0
        while guard < 150:
            guard += 1
            row = self.raw_match()
            if row["status"] == "completed":
                break
            state = json.loads(row["state_json"])
            task = state["tasks"][state["current_task_index"]]
            self.clock.advance(task.get("deadline_at", self.clock()) - self.clock() + 2)
            self.service.get_match(self.code, self.players[0]["token"])
        self.assertEqual(self.raw_match()["status"], "completed")
        # Reset the accelerated test clock, then refresh all player sessions as
        # the real clients would do through their normal room polling.
        self.clock.value = int(time.time())
        for player in self.players:
            self.store.get_session(player["token"])
        next_match = self.service.start_match(self.code, self.players[0]["token"])["match"]
        self.assertEqual(next_match["status"], "active")


if __name__ == "__main__":
    unittest.main()
