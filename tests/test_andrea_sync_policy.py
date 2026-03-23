"""Policy helpers for verify-before-deny (no HTTP)."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from services.andrea_sync.policy import (
    META_DIGEST_KEY,
    META_DIGEST_TS_KEY,
    evaluate_skill_absence_claim,
    resolve_skill_truth,
)
from services.andrea_sync.store import connect, migrate, set_meta


class TestAndreaSyncPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self._prev = os.environ.get("ANDREA_SYNC_DB")
        os.environ["ANDREA_SYNC_DB"] = str(self.db_path)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        if self._prev is None:
            os.environ.pop("ANDREA_SYNC_DB", None)
        else:
            os.environ["ANDREA_SYNC_DB"] = self._prev
        self.db_path.unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(str(self.db_path) + suf).unlink(missing_ok=True)

    def test_missing_digest_blocks_absence_claim(self) -> None:
        ev = evaluate_skill_absence_claim(self.conn, "telegram")
        self.assertFalse(ev["may_claim_absent"])
        self.assertTrue(ev.get("must_refresh"))

    def test_stale_digest_blocks_absence_claim(self) -> None:
        old = time.time() - 99999
        blob = {"rows": [{"id": "skill:telegram", "status": "ready"}], "published_ts": old}
        set_meta(self.conn, META_DIGEST_KEY, json.dumps(blob))
        set_meta(self.conn, META_DIGEST_TS_KEY, str(old))
        ev = evaluate_skill_absence_claim(self.conn, "telegram", max_age_seconds=60.0)
        self.assertFalse(ev["may_claim_absent"])
        self.assertEqual(ev["reason"], "capability_digest_stale")

    def test_alias_resolution_matches_runtime_skill(self) -> None:
        now = time.time()
        blob = {
            "rows": [
                {
                    "id": "skill:bluebubbles",
                    "detail": "bluebubbles",
                    "status": "ready",
                    "aliases": ["blue bubbles", "imessage", "text messages"],
                    "availability": "verified_available",
                }
            ],
            "published_ts": now,
        }
        set_meta(self.conn, META_DIGEST_KEY, json.dumps(blob))
        set_meta(self.conn, META_DIGEST_TS_KEY, str(now))
        truth = resolve_skill_truth(self.conn, "blue bubbles")
        self.assertTrue(truth["verified"])
        self.assertEqual(truth["status"], "verified_available")
        ev = evaluate_skill_absence_claim(self.conn, "text messages")
        self.assertFalse(ev["may_claim_absent"])
        self.assertEqual(ev["reason"], "verify_before_deny:skill_ready")

    def test_unknown_skill_requires_probe_not_absence_claim(self) -> None:
        now = time.time()
        blob = {"rows": [{"id": "skill:telegram", "status": "ready"}], "published_ts": now}
        set_meta(self.conn, META_DIGEST_KEY, json.dumps(blob))
        set_meta(self.conn, META_DIGEST_TS_KEY, str(now))
        truth = resolve_skill_truth(self.conn, "blue bubbles")
        self.assertFalse(truth["verified"])
        self.assertEqual(truth["status"], "unknown_needs_probe")
        ev = evaluate_skill_absence_claim(self.conn, "blue bubbles")
        self.assertFalse(ev["may_claim_absent"])
        self.assertTrue(ev.get("must_probe"))


if __name__ == "__main__":
    unittest.main()
