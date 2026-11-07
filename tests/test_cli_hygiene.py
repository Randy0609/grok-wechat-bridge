import contextlib
import io
import json
import re
import subprocess
import unittest
from pathlib import Path

from grok_wechat_bridge import adapters, cli
from grok_wechat_bridge.modality import Direction
from grok_wechat_bridge.report import DIRECTION_STATUS

ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def test_selftest_exercises_all_directions_without_claiming_live(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["selftest"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(out["directions_exercised"]), sorted(d.value for d in Direction))
        self.assertFalse(any(r["perception_live_verified"] for r in out["results"]))

    def test_directions_table_marks_live_not_implemented(self):
        self.assertEqual(set(DIRECTION_STATUS), set(Direction))
        for status in DIRECTION_STATUS.values():
            self.assertIn("NOT implemented", status["live"])

    def test_live_adapters_refuse_to_construct(self):
        for cls in (adapters.LiveWeChatAdapter, adapters.LivePrimaryGrokBotAdapter):
            with self.assertRaises(adapters.NotImplementedLiveIntegration):
                cls()


class RepositoryHygieneTests(unittest.TestCase):
    """Tracked files must not carry credentials, personal paths or binary media."""

    def _tracked(self):
        try:
            names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                   text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git not available")
        return [ROOT / n for n in names]

    def test_no_media_or_db_files_tracked(self):
        bad = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".silk", ".amr", ".wav",
               ".mp3", ".ogg", ".sqlite", ".db", ".pem", ".key"}
        self.assertEqual([p.name for p in self._tracked() if p.suffix.lower() in bad], [])

    def test_no_personal_paths_or_secret_markers(self):
        patterns = [
            re.compile("/" + "Users/[A-Za-z]"),
            re.compile("/" + "home/[a-z][a-z0-9_-]*/"),
            re.compile(r"[A-Z]:\\" + r"Users\\"),
            re.compile("-----BEGIN " + "[A-Z ]*PRIVATE KEY"),
            re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}"),
            re.compile("wx" + "id_[a-z0-9]{6,}"),
            re.compile("x" + "ai-[A-Za-z0-9]{20,}"),
            re.compile("s" + "k-[A-Za-z0-9]{20,}"),
        ]
        hits = []
        for path in self._tracked():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pat in patterns:
                if pat.search(text):
                    hits.append(f"{path.relative_to(ROOT)}: {pat.pattern}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
