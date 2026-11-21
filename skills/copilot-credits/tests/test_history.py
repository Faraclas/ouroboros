"""Offline tests for copilot-credits daily snapshot persistence and /history route."""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_plugin as TP  # noqa: E402
from test_plugin import Base, FakeFetch, P, SECRETS, _DELETE, body, granted  # noqa: E402


class DailySnapshotHistoryTests(Base):
    def test_persists_by_ny_day_overwrites_same_day_and_serves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Nov 21 04:59:59Z is Nov 20 23:59:59 EST; Nov 21 05:00:00Z is Nov 21 00:00:00 EST.
            t1 = _dt.datetime(2026, 11, 21, 4, 59, 59, tzinfo=_dt.timezone.utc).timestamp()
            t2 = _dt.datetime(2026, 11, 21, 5, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
            t3 = _dt.datetime(2026, 11, 21, 18, 30, 0, tzinfo=_dt.timezone.utc).timestamp()
            self.wall.t = t1
            fetch = FakeFetch(
                (200, None, body(credits_used=11700.5, entitlement=12600)),
                (200, None, body(credits_used=11750.25, entitlement=12600)),
                (200, None, body(credits_used=11796.365, entitlement=12600)),
            )
            svc = P.CreditsService(
                lambda: P.read_credential(granted()), fetch,
                wall=self.wall, monotonic=self.mono, log=self.logged.append, state_dir=tmp,
            )
            self.get(svc)
            self.mono.t += 61
            self.wall.t = t2
            self.get(svc)
            self.mono.t += 61
            self.wall.t = t3
            self.get(svc)

            snap_path = os.path.join(tmp, P.SNAPSHOT_FILENAME)
            with open(snap_path, encoding="utf-8") as handle:
                raw_text = handle.read()
            for secret in SECRETS:
                self.assertNotIn(secret, raw_text)
            hist = svc.get_history()
            self.assertEqual(hist["status"], "ok")
            self.assertEqual(hist["timezone"], "America/New_York")
            self.assertEqual(hist["month"], "2026-11")
            self.assertEqual(hist["today"], "2026-11-21")
            self.assertEqual(hist["days"], [
                {"date": "2026-11-20", "credits_used": 11700.5, "entitlement": 12600,
                 "observed_at": "2026-11-21T04:59:59Z", "login": "sample-user"},
                {"date": "2026-11-21", "credits_used": 11796.365, "entitlement": 12600,
                 "observed_at": "2026-11-21T18:30:00Z", "login": "sample-user"},
            ])
            # Other months return empty; no missing past days are invented.
            self.assertEqual(svc.get_history("2026-10")["days"], [])

    def test_edt_midnight_boundary_and_future_day_omission(self):
        with tempfile.TemporaryDirectory() as tmp:
            # EDT (UTC-4): July 15 03:59:59Z -> July 14; July 15 04:00:00Z -> July 15.
            t1 = _dt.datetime(2026, 7, 15, 3, 59, 59, tzinfo=_dt.timezone.utc).timestamp()
            t2 = _dt.datetime(2026, 7, 15, 4, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
            self.wall.t = t1
            fetch = FakeFetch(
                (200, None, body(credits_used=500, entitlement=10000)),
                (200, None, body(credits_used=620, entitlement=10000)),
            )
            svc = P.CreditsService(
                lambda: P.read_credential(granted()), fetch,
                wall=self.wall, monotonic=self.mono, log=self.logged.append, state_dir=tmp,
            )
            self.get(svc)
            self.mono.t += 61
            self.wall.t = t2
            self.get(svc)
            self.assertEqual([d["date"] for d in svc.get_history()["days"]], ["2026-07-14", "2026-07-15"])
            # Rewinding wall clock omits days after `today`.
            self.wall.t = t1
            self.assertEqual([d["date"] for d in svc.get_history()["days"]], ["2026-07-14"])

    def test_does_not_persist_failures_credential_changes_missing_credits_or_corrupt_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.wall.t = _dt.datetime(2026, 11, 21, 15, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
            settings = granted()
            fetch = FakeFetch((200, None, body(credits_used=_DELETE)), P.UpstreamError("timeout"))
            svc = P.CreditsService(
                lambda: P.read_credential(settings), fetch,
                wall=self.wall, monotonic=self.mono, log=self.logged.append, state_dir=tmp,
            )
            self.get(svc)
            self.assertFalse(os.path.exists(os.path.join(tmp, P.SNAPSHOT_FILENAME)))
            self.mono.t += 61
            self.get(svc)
            self.assertEqual(svc.get_history()["days"], [])

            # Credential changed mid-flight discards result and writes no snapshot.
            def rotate():
                settings.values["OPENAI_COMPATIBLE_API_KEY"] = TP.TOKEN_B

            svc2 = P.CreditsService(
                lambda: P.read_credential(settings),
                FakeFetch((200, None, body(credits_used=999)), hook=rotate),
                wall=self.wall, monotonic=self.mono, state_dir=tmp,
            )
            self.assertUnavailable(self.get(svc2), "credential_changed")
            self.assertFalse(os.path.exists(os.path.join(tmp, P.SNAPSHOT_FILENAME)))

            with open(os.path.join(tmp, P.SNAPSHOT_FILENAME), "w", encoding="utf-8") as handle:
                handle.write("{corrupt")
            bad = svc.get_history()
            self.assertEqual(bad["status"], "unavailable")
            self.assertEqual(bad["error"]["code"], "history_unreadable")


if __name__ == "__main__":
    unittest.main(verbosity=1)
