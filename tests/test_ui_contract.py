from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NaneUiContractTests(unittest.TestCase):
    def test_home_is_rtl_and_has_room_controls(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('lang="ar"', html)
        self.assertIn('dir="rtl"', html)
        for key in ("createName", "createButton", "joinName", "roomCode",
                    "joinButton", "room", "readyButton", "players"):
            self.assertIn('id="%s"' % key, html)

    def test_client_uses_xmlhttprequest_for_legacy_safari(self):
        js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("new XMLHttpRequest()", js)
        self.assertNotIn("fetch(", js)
        self.assertNotIn("new WebSocket", js)

    def test_styles_include_small_phone_breakpoint(self):
        css = (ROOT / "web" / "css" / "main.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:420px)", css)
        self.assertNotIn("display:grid", css)


if __name__ == "__main__":
    unittest.main()
