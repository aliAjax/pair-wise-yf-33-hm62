import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, SatelliteSchedulingService, iso, utcnow


class ReceiptReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db"); self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.window = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 7200})

    def tearDown(self): self.tmp.cleanup()

    def _received_schedule(self, mb=35000, start=None, end=None, rate=60, window_id=None):
        start = start or self.now; end = end or (self.now + timedelta(hours=1, minutes=30))
        req = self.svc.create_request("requester-t1", "requester", "T1", {"satellite_id": "SAT1", "data_mb": mb, "priority": 7, "deadline": iso(self.now + timedelta(days=1))})
        schedule = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": window_id or self.window["id"], "antenna_id": "ANT1", "starts_at": iso(start), "ends_at": iso(end), "rate_mbps": rate})
        self.svc.transition(schedule["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(schedule["id"], "op", "operator", "", "received", {})
        return req, schedule

    def _receipt_body(self, schedule_id, actual_mb=27000, start=None, end=None, revision=1):
        start = start or self.now; end = end or (self.now + timedelta(hours=1))
        return {"schedule_id": schedule_id, "actual_starts_at": iso(start), "actual_ends_at": iso(end), "actual_mb": actual_mb, "window_revision": revision}

    def test_receipt_settles_once_and_refunds_shortfall_quota(self):
        req, schedule = self._received_schedule()
        day = self.now.date().isoformat()
        self.assertEqual(self.svc.get_quota_usage("T1", "GS1", day)["used_seconds"], 5400)
        receipt = self.svc.submit_receipt("op", "operator", self._receipt_body(schedule["id"]))
        self.assertEqual(receipt["status"], "settled")
        settlement = self.svc.list_settlements("operator", "")[0]
        self.assertEqual(settlement["schedule_id"], schedule["id"])
        self.assertEqual(settlement["planned_seconds"], 5400)
        self.assertEqual(settlement["actual_seconds"], 3600)
        self.assertEqual(settlement["refund_seconds"], 1800)
        self.assertEqual(settlement["refund_mb"], 8000)
        # 差量退回租户当天配额
        usage = self.svc.get_quota_usage("T1", "GS1", day)
        self.assertEqual(usage["used_seconds"], 3600)
        self.assertEqual(usage["remaining_seconds"], 3600)
        # 重复回执只结算一次
        duplicate = self.svc.submit_receipt("op", "operator", self._receipt_body(schedule["id"]))
        self.assertEqual(duplicate["id"], receipt["id"])
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 1)
        self.assertEqual(len(self.svc.list_receipts("operator", "")), 1)

    def test_window_revision_mismatch_suspends_and_keeps_plan(self):
        req, schedule = self._received_schedule()
        receipt = self.svc.submit_receipt("op", "operator", self._receipt_body(schedule["id"], revision=999))
        self.assertEqual(receipt["status"], "suspended")
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 0)
        # 原计划照旧保留
        self.assertEqual(self.svc.get_schedule(schedule["id"])["status"], "received")
        # 挂起不退回配额
        self.assertEqual(self.svc.get_quota_usage("T1", "GS1", self.now.date().isoformat())["used_seconds"], 5400)
        # 对账台能看到挂起项
        desk = self.svc.reconciliation("operator", "")
        self.assertEqual(len(desk["suspended"]), 1)
        self.assertEqual(desk["totals"]["settled_count"], 0)

    def test_exceeds_visible_window_rejected_entirely(self):
        req, schedule = self._received_schedule()
        # 实际结束超出窗口结束（窗口 2h，实际 2.5h）
        receipt = self.svc.submit_receipt("op", "operator", self._receipt_body(schedule["id"], end=self.now + timedelta(hours=2, minutes=30)))
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 0)
        self.assertEqual(self.svc.get_schedule(schedule["id"])["status"], "received")
        desk = self.svc.reconciliation("operator", "")
        self.assertEqual(len(desk["rejected"]), 1)

    def test_concurrent_duplicate_submission_settles_once(self):
        import threading
        req, schedule = self._received_schedule()
        body = self._receipt_body(schedule["id"])
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            results.append(self.svc.submit_receipt("op", "operator", body))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(self.svc.list_receipts("operator", "")), 1)
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 1)

    def test_write_failure_keeps_pending_and_retry_no_accumulation(self):
        req, schedule = self._received_schedule()
        self.svc.settlement_failures_left = 1
        receipt = self.svc.submit_receipt("op", "operator", self._receipt_body(schedule["id"]))
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(receipt["settle_attempts"], 1)
        self.assertTrue(receipt["last_error"])
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 0)
        # 重试后结算成功
        retried = self.svc.retry_receipt(receipt["id"], "op", "operator")
        self.assertEqual(retried["status"], "settled")
        self.assertIsNone(retried["last_error"])
        self.assertEqual(retried["settle_attempts"], 2)
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 1)
        # 重复重试不再累加
        again = self.svc.retry_receipt(receipt["id"], "op", "operator")
        self.assertEqual(again["status"], "settled")
        self.assertEqual(again["settle_attempts"], 2)
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 1)

    def test_backfill_old_data_at_planned_value(self):
        req, schedule = self._received_schedule()
        # 旧数据无回执
        self.assertEqual(len(self.svc.reconciliation("operator", "")["backfill_candidates"]), 1)
        result = self.svc.backfill_receipts("cmd", "commander")
        self.assertEqual(result["count"], 1)
        receipt = self.svc.get_receipt(result["backfilled"][0]["receipt_id"])
        self.assertEqual(receipt["status"], "settled")
        self.assertEqual(receipt["backfilled"], 1)
        settlement = self.svc.list_settlements("operator", "")[0]
        self.assertEqual(settlement["actual_mb"], settlement["planned_mb"])
        self.assertEqual(settlement["refund_seconds"], 0)
        # 重复回填不重复入账
        again = self.svc.backfill_receipts("cmd", "commander")
        self.assertEqual(again["count"], 0)
        self.assertEqual(len(self.svc.list_settlements("operator", "")), 1)

    def test_backfill_requires_commander(self):
        req, schedule = self._received_schedule()
        with self.assertRaises(ApiError) as ctx:
            self.svc.backfill_receipts("op", "operator")
        self.assertEqual(ctx.exception.code, "backfill_forbidden")

    def test_reconciliation_desk_totals(self):
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 20000})
        wide = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=4)), "max_rate_mbps": 70})
        req1, s1 = self._received_schedule(mb=35000, start=self.now, end=self.now + timedelta(hours=1, minutes=30), window_id=wide["id"])
        req2, s2 = self._received_schedule(mb=20000, start=self.now + timedelta(hours=2), end=self.now + timedelta(hours=3, minutes=30), window_id=wide["id"])
        # s1 正常结算（少收 8000MB）
        self.svc.submit_receipt("op", "operator", self._receipt_body(s1["id"], actual_mb=27000))
        # s2 挂起
        self.svc.submit_receipt("op", "operator", self._receipt_body(s2["id"], actual_mb=15000, revision=2))
        desk = self.svc.reconciliation("operator", "")
        self.assertEqual(len(desk["pending"]), 0)
        self.assertEqual(len(desk["suspended"]), 1)
        self.assertEqual(desk["totals"]["settled_count"], 1)
        self.assertEqual(desk["totals"]["planned_mb"], 35000)
        self.assertEqual(desk["totals"]["actual_mb"], 27000)
        self.assertEqual(desk["totals"]["refund_mb"], 8000)
        # 结算账、回执入口、对账台数据一致但各自独立
        self.assertEqual(len(desk["receipts"]), 2)
        self.assertEqual(len(desk["settlements"]), 1)


if __name__ == "__main__": unittest.main()
