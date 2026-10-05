import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, Repository, SatelliteSchedulingService, iso, utcnow


class ReceiptReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = SatelliteSchedulingService(self.db)
        self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.window = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 7200})

    def tearDown(self):
        self.tmp.cleanup()

    def received_schedule(self, mb=27000, hours=1, rate=60):
        req = self.svc.create_request("requester-t1", "requester", "T1", {"satellite_id": "SAT1", "data_mb": mb, "priority": 7, "deadline": iso(self.now + timedelta(days=1))})
        schedule = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=hours)), "rate_mbps": rate})
        self.svc.transition(schedule["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(schedule["id"], "op", "operator", "", "received", {})
        return schedule

    def receipt(self, revision=1, mb=27000, start=None, end=None, schedule_id=None, external_id="R-1"):
        schedule_id = schedule_id or self.schedule_id
        return self.svc.submit_receipt(schedule_id, "op1", "operator", {
            "external_receipt_id": external_id,
            "window_revision": revision,
            "actual_starts_at": iso(start or self.now),
            "actual_ends_at": iso(end or self.now + timedelta(hours=1)),
            "actual_received_mb": mb,
        })

    @property
    def schedule_id(self):
        if not hasattr(self, "_schedule_id"):
            self._schedule_id = self.received_schedule()["id"]
        return self._schedule_id

    def test_duplicate_receipt_settles_once_and_refunds_shortfall(self):
        first = self.receipt(mb=18000)
        second = self.receipt(mb=18000, external_id="R-1-DUP")
        self.assertEqual(first["status"], "settled")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        settlements = self.svc.list_settlements("operator")
        self.assertEqual(len(settlements), 1)
        self.assertAlmostEqual(settlements[0]["refund_seconds"], 1200.0)
        self.assertAlmostEqual(settlements[0]["billed_seconds"], 2400.0)
        used = self.svc._used_quota(self.svc.repo.conn, "T1", "GS1", self.now.date().isoformat())
        self.assertAlmostEqual(used, 2400.0)

    def test_concurrent_duplicate_submissions_have_one_entry_and_settlement(self):
        schedule_id = self.received_schedule()["id"]
        results, errors = [], []

        def submit(operator):
            try:
                results.append(self.svc.submit_receipt(schedule_id, operator, "operator", {
                    "external_receipt_id": "R-CONCURRENT",
                    "window_revision": 1,
                    "actual_starts_at": iso(self.now),
                    "actual_ends_at": iso(self.now + timedelta(hours=1)),
                    "actual_received_mb": 27000,
                }))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(f"op{i}",)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({x["status"] for x in results}, {"settled"})
        self.assertEqual(len({x["id"] for x in results}), 1)
        self.assertEqual(len(self.svc.list_settlements("operator")), 1)

    def test_revision_mismatch_is_held_original_plan_retained_then_rechecks(self):
        before = self.svc.get_schedule(self.schedule_id)
        held = self.receipt(revision=2, mb=18000)
        self.assertEqual(held["status"], "held")
        self.assertIn("窗口版次不一致", held["reason"])
        self.assertEqual(self.svc.list_settlements("operator"), [])
        after_held = self.svc.get_schedule(self.schedule_id)
        self.assertEqual(before["starts_at"], after_held["starts_at"])
        self.assertEqual(before["ends_at"], after_held["ends_at"])
        self.assertEqual(after_held["status"], "received")
        self.assertEqual(self.svc.retry_receipt(held["id"], "op", "operator")["status"], "held")
        self.svc.change_window(self.window["id"], "op", "operator", {"starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2))})
        settled = self.svc.retry_receipt(held["id"], "op", "operator")
        self.assertEqual(settled["status"], "settled")
        self.assertAlmostEqual(settled["settlement"]["refund_seconds"], 1200.0)

    def test_receipt_outside_visible_window_is_entirely_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.receipt(end=self.now + timedelta(hours=2, minutes=1))
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "receipt_outside_window")
        self.assertEqual(ctx.exception.details["status"], "rejected")
        self.assertEqual(self.svc.list_settlements("operator"), [])
        with self.assertRaises(ApiError) as retry_ctx:
            self.svc.retry_receipt(ctx.exception.details["id"], "op", "operator")
        self.assertEqual(retry_ctx.exception.code, "receipt_not_retryable")

    def test_failed_write_remains_pending_and_retry_does_not_double_settle(self):
        schedule_id = self.received_schedule()["id"]
        original_audit = Repository.audit

        def failing_audit(conn, request_id, sid, actor, role, action, detail):
            if action == "receipt_settled":
                Repository.audit = staticmethod(original_audit)
                raise sqlite3.OperationalError("simulated write failure")
            return original_audit(conn, request_id, sid, actor, role, action, detail)

        Repository.audit = staticmethod(failing_audit)
        pending = self.svc.submit_receipt(schedule_id, "op", "operator", {
            "external_receipt_id": "R-FAIL", "window_revision": 1,
            "actual_starts_at": iso(self.now), "actual_ends_at": iso(self.now + timedelta(hours=1)),
            "actual_received_mb": 27000,
        })
        self.assertEqual(pending["status"], "pending_retry")
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(self.svc.list_settlements("operator"), [])
        settled = self.svc.retry_receipt(pending["id"], "op", "operator")
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(len(self.svc.list_settlements("operator")), 1)
        self.svc.retry_receipt(pending["id"], "op2", "operator")
        self.assertEqual(len(self.svc.list_settlements("operator")), 1)

    def test_legacy_received_schedule_backfills_from_plan_on_startup(self):
        schedule_id = self.received_schedule()["id"]
        self.svc.repo.conn.execute("PRAGMA user_version=0")
        upgraded = SatelliteSchedulingService(self.db)
        settlements = upgraded.list_settlements("operator")
        self.assertEqual(len(settlements), 1)
        settlement = settlements[0]
        self.assertEqual(settlement["schedule_id"], schedule_id)
        self.assertEqual(settlement["source"], "legacy_plan")
        self.assertEqual(settlement["planned_mb"], settlement["actual_mb"])
        self.assertEqual(settlement["refund_seconds"], 0.0)
        reconciliation = upgraded.reconciliation("operator")
        self.assertEqual(reconciliation["summary"]["backfilled_plan"], 1)
        self.assertEqual(reconciliation["items"][0]["reconciliation_status"], "backfilled_plan")

    def test_separate_receipt_settlement_and_reconciliation_views(self):
        self.receipt(mb=18000)
        receipt = self.svc.list_receipts("operator")[0]
        settlement = self.svc.list_settlements("operator")[0]
        reconciliation = self.svc.reconciliation("auditor")["items"][0]
        self.assertEqual(receipt["actual_received_mb"], 18000)
        self.assertNotIn("billed_seconds", receipt)
        self.assertEqual(settlement["receipt_id"], receipt["id"])
        self.assertEqual(reconciliation["receipt_id"], receipt["id"])
        self.assertEqual(reconciliation["settlement_id"], settlement["id"])
        self.assertEqual(reconciliation["reconciliation_status"], "settled")


if __name__ == "__main__":
    unittest.main()
