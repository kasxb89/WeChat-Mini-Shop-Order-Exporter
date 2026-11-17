from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from order_exporter.activation import ActivationStore
from order_exporter.models import OrderRecord
from order_exporter.store import Store


class ActivationTests(unittest.TestCase):
    def setUp(self):
        configured=patch('order_exporter.activation.ACTIVATION_CODE_SHA256',hashlib.sha256(b'unit-test-activation').hexdigest())
        configured.start()
        self.addCleanup(configured.stop)
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/"activation.json"
        self.now=datetime(2026,11,16,0,0,tzinfo=timezone.utc)
        self.store=ActivationStore(self.path,lambda:self.now)

    def test_first_run_starts_seven_days_and_reopening_does_not_restart_them(self):
        self.assertEqual(self.store.status().remaining_days,7)
        first=json.loads(self.path.read_text())["started_at"]
        self.now+=timedelta(days=6,hours=23)
        reopened=ActivationStore(self.path,lambda:self.now)
        self.assertEqual(reopened.status().remaining_days,1)
        self.assertEqual(json.loads(self.path.read_text())["started_at"],first)

    def test_exact_seven_day_boundary_locks_only_collection_and_preserves_files(self):
        self.store.status()
        unrelated=Path(self.directory.name)/"客户订单.xlsx"
        unrelated.write_bytes(b"customer-data")
        self.now+=timedelta(days=7)
        self.assertEqual(self.store.status().state,"expired")
        self.assertFalse(self.store.status().can_collect)
        self.assertEqual(unrelated.read_bytes(),b"customer-data")
        self.assertTrue(self.path.is_file())

    def test_wrong_or_non_ascii_code_does_not_activate(self):
        self.store.status()
        for value in ("", "wrong-code", "unit-test-activation-extra", "激活码错误"):
            self.assertFalse(self.store.activate(value))
        self.assertEqual(self.store.status().state,"trial")

    def test_correct_code_is_saved_once_and_remains_active_after_restarts(self):
        self.assertTrue(self.store.activate("unit-test-activation"))
        activated=self.path.read_bytes()
        self.now+=timedelta(days=365)
        reopened=ActivationStore(self.path,lambda:self.now)
        self.assertEqual(reopened.status().state,"activated")
        self.assertTrue(reopened.activate("unit-test-activation"))
        self.assertEqual(self.path.read_bytes(),activated)

    def test_expired_trial_can_be_activated_without_losing_its_start_time(self):
        self.store.status()
        first=json.loads(self.path.read_text())["started_at"]
        self.now+=timedelta(days=10)
        self.assertFalse(self.store.status().can_collect)
        self.assertTrue(self.store.activate("unit-test-activation"))
        self.assertTrue(self.store.status().can_collect)
        self.assertEqual(json.loads(self.path.read_text())["started_at"],first)

    def test_clock_rollback_does_not_move_the_deadline_or_last_seen_back(self):
        deadline=self.store.status().deadline
        self.now+=timedelta(days=6)
        self.store.status()
        seen=json.loads(self.path.read_text())["last_seen_at"]
        self.now-=timedelta(days=5)
        self.assertEqual(self.store.status().deadline,deadline)
        self.assertEqual(self.store.status().remaining_days,1)
        self.assertEqual(json.loads(self.path.read_text())["last_seen_at"],seen)

    def test_invalid_record_blocks_collection_and_can_be_repaired_by_the_code(self):
        self.path.write_text("invalid",encoding="utf-8")
        self.assertFalse(self.store.status().can_collect)
        self.assertTrue(self.store.activate("unit-test-activation"))
        self.assertEqual(self.store.status().state,"activated")

    def test_unconfigured_build_cannot_be_permanently_activated(self):
        with patch('order_exporter.activation.ACTIVATION_CODE_SHA256',''):
            self.assertFalse(self.store.activate('unit-test-activation'))
        self.assertEqual(self.store.status().state,'trial')


class AutomaticSaveTests(unittest.TestCase):
    def run_worker(self, root, fail=False):
        from order_exporter.ui import Worker
        database=Path(root)/"orders.sqlite3"
        output=Path(root)/"orders.xlsx"
        store=Store(database)
        job=store.create(output)
        store.close()
        worker=Worker(database,job,output)
        outcomes=[]
        worker.outcome.connect(lambda status,message:outcomes.append((status,message)))
        with patch("order_exporter.native.Desktop"), patch("order_exporter.vision.get_vision"), patch("order_exporter.direct.Collector") as cls:
            collector=cls.return_value
            collector.bound_window=None
            collector.end_reason="已到列表末尾。"
            collector.api.requests=1
            collector.api.request_seconds=.1
            def collect(save,initial_count):
                save(OrderRecord("001234567890123",("验证商品",),(),recipient_checked=True))
                return 1
            collector.collect.side_effect=collect
            if fail:
                with patch("order_exporter.ui.export_xlsx",side_effect=PermissionError):
                    worker.run()
            else:
                worker.run()
        return outcomes,output

    def test_automatic_save_preserves_the_completed_file(self):
        with tempfile.TemporaryDirectory() as root:
            outcomes,output=self.run_worker(root)
            self.assertTrue(output.is_file())
            self.assertEqual(outcomes[0][0],"complete")

    def test_excel_lock_does_not_claim_the_file_was_saved(self):
        with tempfile.TemporaryDirectory() as root:
            outcomes,output=self.run_worker(root,fail=True)
            self.assertFalse(output.exists())
            self.assertIn("正被占用",outcomes[0][1])


if __name__=="__main__":
    unittest.main()
