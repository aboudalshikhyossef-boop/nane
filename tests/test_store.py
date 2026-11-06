import concurrent.futures
from contextlib import closing
import tempfile
import time
import unittest
from pathlib import Path

from server.store import NaneStore, InvalidInput, NameTaken, RoomFull, Unauthorized


class NaneStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.sqlite3")
        self.store = NaneStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_room_assigns_first_seat_and_private_session(self):
        created = self.store.create_room("يوسف")
        self.assertEqual(created["room"]["occupied"], 1)
        self.assertEqual(created["room"]["you"]["seat"], 1)
        self.assertEqual(created["room"]["players"][0]["name"], "يوسف")
        self.assertNotIn("session_token", created["room"])
        session = self.store.get_session(created["session_token"])
        self.assertEqual(session["invite_code"], created["invite_code"])

    def test_four_players_fill_four_distinct_seats_and_fifth_is_rejected(self):
        created = self.store.create_room("لاعب 1")
        for name in ("لاعب 2", "لاعب 3", "لاعب 4"):
            self.store.join_room(created["invite_code"], name)
        room = self.store.get_room(created["invite_code"])
        self.assertEqual(room["occupied"], 4)
        self.assertEqual([player["seat"] for player in room["players"]], [1, 2, 3, 4])
        with self.assertRaises(RoomFull):
            self.store.join_room(created["invite_code"], "لاعب خامس")

    def test_duplicate_names_are_rejected_case_insensitively(self):
        created = self.store.create_room("Player")
        with self.assertRaises(NameTaken):
            self.store.join_room(created["invite_code"], "player")

    def test_invalid_name_is_rejected(self):
        with self.assertRaises(InvalidInput):
            self.store.create_room("   ")
        with self.assertRaises(InvalidInput):
            self.store.create_room("ا" * 19)

    def test_ready_state_is_per_player_and_all_ready_requires_four(self):
        created = self.store.create_room("A")
        room = self.store.set_ready(created["invite_code"], created["session_token"], True)
        self.assertFalse(room["all_ready"])
        self.assertTrue(room["you"]["ready"])
        tokens = [created["session_token"]]
        for name in ("B", "C", "D"):
            tokens.append(self.store.join_room(created["invite_code"], name)["session_token"])
        for token in tokens[1:]:
            room = self.store.set_ready(created["invite_code"], token, True)
        self.assertTrue(room["all_ready"])
        self.assertEqual(room["occupied"], 4)

    def test_leave_releases_seat_and_revokes_session(self):
        created = self.store.create_room("A")
        self.store.leave_room(created["invite_code"], created["session_token"])
        with self.assertRaises(Unauthorized):
            self.store.get_session(created["session_token"])
        joined = self.store.join_room(created["invite_code"], "New player")
        self.assertEqual(joined["room"]["you"]["seat"], 1)

    def test_invalid_join_code_does_not_revoke_existing_session(self):
        created = self.store.create_room("A")
        with self.assertRaises(InvalidInput):
            self.store.join_room("bad", "A", current_token=created["session_token"])
        self.assertEqual(self.store.get_session(created["session_token"])["player_id"], created["player_id"])

    def test_joining_full_room_does_not_revoke_session_in_another_room(self):
        full_room = self.store.create_room("A")
        for name in ("B", "C", "D"):
            self.store.join_room(full_room["invite_code"], name)
        other = self.store.create_room("Other")
        with self.assertRaises(RoomFull):
            self.store.join_room(full_room["invite_code"], "Other", current_token=other["session_token"])
        self.assertEqual(self.store.get_session(other["session_token"])["player_id"], other["player_id"])

    def test_concurrent_joins_never_allocate_more_than_four_seats(self):
        created = self.store.create_room("Host")
        names = ["P2", "P3", "P4", "P5", "P6", "P7"]

        def join(name):
            try:
                result = self.store.join_room(created["invite_code"], name)
                return ("ok", result["room"]["you"]["seat"])
            except RoomFull:
                return ("full", None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(join, names))
        self.assertEqual(sum(1 for item in results if item[0] == "ok"), 3)
        room = self.store.get_room(created["invite_code"])
        self.assertEqual(room["occupied"], 4)
        self.assertEqual(sorted(player["seat"] for player in room["players"]), [1, 2, 3, 4])


    def test_custom_database_path_does_not_change_parent_permissions(self):
        custom_dir = Path(self.temp.name) / "shared-dir"
        custom_dir.mkdir(mode=0o755)
        custom_dir.chmod(0o755)
        custom_db = custom_dir / "custom.sqlite3"
        NaneStore(str(custom_db))
        self.assertEqual(custom_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(custom_db.stat().st_mode & 0o777, 0o600)


    def test_all_ready_requires_every_player_to_be_connected(self):
        created = self.store.create_room("A")
        tokens = [created["session_token"]]
        for name in ("B", "C", "D"):
            tokens.append(self.store.join_room(created["invite_code"], name)["session_token"])
        for token in tokens:
            room = self.store.set_ready(created["invite_code"], token, True)
        self.assertTrue(room["all_ready"])
        with closing(self.store._connect()) as db:
            db.execute(
                "UPDATE players SET last_seen = ? WHERE seat = 4",
                (int(time.time()) - 60,),
            )
        room = self.store.get_room(created["invite_code"])
        self.assertFalse(room["all_ready"])
        self.assertFalse(room["players"][3]["connected"])


    def test_three_ready_players_are_enough_for_lobby_start(self):
        created = self.store.create_room("A")
        second = self.store.join_room(created["invite_code"], "B")
        third = self.store.join_room(created["invite_code"], "C")
        self.store.set_ready(created["invite_code"], created["session_token"], True)
        self.store.set_ready(created["invite_code"], second["session_token"], True)
        self.store.set_ready(created["invite_code"], third["session_token"], True)
        room = self.store.get_room(created["invite_code"])
        self.assertTrue(room["all_ready"])
        self.assertEqual(room["occupied"], 3)

    def test_two_player_lobby_never_reports_all_ready(self):
        created = self.store.create_room("A")
        second = self.store.join_room(created["invite_code"], "B")
        self.store.set_ready(created["invite_code"], created["session_token"], True)
        self.store.set_ready(created["invite_code"], second["session_token"], True)
        room = self.store.get_room(created["invite_code"])
        self.assertEqual(room["occupied"], 2)
        self.assertFalse(room["all_ready"])


if __name__ == "__main__":
    unittest.main()
