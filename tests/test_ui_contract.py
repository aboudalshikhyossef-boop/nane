from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NaneUiContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "web" / "css" / "main.css").read_text(encoding="utf-8")

    def test_home_is_rtl_and_has_room_controls(self):
        self.assertIn('lang="ar"', self.html)
        self.assertIn('dir="rtl"', self.html)
        for key in ("createName", "createButton", "joinName", "roomCode",
                    "joinButton", "room", "readyButton", "players"):
            self.assertIn('id="%s"' % key, self.html)

    def test_profile_and_match_screens_have_stable_mount_points(self):
        for key in ("profile", "profileQuestions", "profileProgressBar",
                    "profileProgressText", "profileSaveState", "profileButton",
                    "profileBack", "arena", "arenaContent", "arenaPhase",
                    "taskRound", "timerValue", "timerBar", "scoreboardList",
                    "startButton", "startText"):
            self.assertIn('id="%s"' % key, self.html)
        self.assertIn('aria-live="polite"', self.html)

    def test_client_uses_xmlhttprequest_for_legacy_safari(self):
        self.assertIn("new XMLHttpRequest()", self.js)
        self.assertNotIn("fetch(", self.js)
        self.assertNotIn("new WebSocket", self.js)

    def test_client_covers_profile_match_and_stale_action_contract(self):
        for contract in (
            "/profile", "/match/start", "/match/action", "task_sequence",
            "remaining_seconds", "revealed_clues", "live_guess_number",
            "player_question", "live_choice", "finale", "mystery", "knowledge",
        ):
            self.assertIn(contract, self.js)

    def test_browser_script_uses_es5_syntax_for_ios_9_safari(self):
        self.assertNotIn("=>", self.js)
        self.assertNotIn("`", self.js)
        self.assertIsNone(re.search(r"\b(?:const|let)\s+[A-Za-z_$]", self.js))
        self.assertNotIn("fetch(", self.js)
        self.assertNotIn("Promise", self.js)

    def test_styles_support_small_touch_screens_without_grid_or_flex_gap(self):
        self.assertIn("@media(max-width:420px)", self.css)
        self.assertNotIn("display:grid", self.css)
        self.assertNotIn("gap:", self.css)
        self.assertIn("min-height:48px", self.css)
        self.assertIn("touch-action:manipulation", self.css)


if __name__ == "__main__":
    unittest.main()
