import base64
import gzip
import json
from queue import Queue
import re
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from order_exporter.bridge import BridgeReader


def snapshot(nodes=None):
    return {"rect": [-700, 20, -200, 820], "nodes": nodes if nodes is not None else [
        {"text": "测试商城", "role": "text", "rect": [-680, 40, -380, 65]},
        {"text": "全部订单", "role": "listitem", "rect": [-680, 80, -580, 110], "selected": True},
    ]}


def respond(request, result=None):
    return (json.dumps({"request_id": request["request_id"], "result": result}, ensure_ascii=False)
            + "\n").encode("utf-8")


class FakeOutput:
    def __init__(self):
        self.lines = Queue()
        self.closed = False

    def readline(self, limit):
        return self.lines.get()[:limit]

    def close(self):
        self.closed = True
        self.lines.put(b"")


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, raw):
        request = json.loads(raw)
        self.process.requests.append(request)
        output = self.process.reply(request)
        if output is not None:
            self.process.stdout.lines.put(output)
        return len(raw)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, reply=None, stubborn=False):
        self.reply = reply or (lambda request: respond(request, snapshot()))
        self.stubborn = stubborn
        self.requests = []
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.returncode = -1
            self.stdout.lines.put(b"")

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.stdout.lines.put(b"")

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        return self.returncode


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.platform = patch("order_exporter.bridge.sys.platform", "win32")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def reader(self, process=None, **kwargs):
        process = process or FakeProcess()
        spawn = patch("order_exporter.bridge.subprocess.Popen", return_value=process)
        popen = spawn.start()
        self.addCleanup(spawn.stop)
        reader = BridgeReader(**kwargs)
        self.addCleanup(reader.close)
        return reader, process, popen

    def test_reuses_worker_and_returns_unicode_absolute_bounds_and_selection(self):
        reader, process, popen = self.reader()
        first = reader.read(123, 456)
        second = reader.read(789, 654)
        self.assertEqual(first["rect"], [-700, 20, -200, 820])
        self.assertEqual(first["nodes"], snapshot()["nodes"])
        self.assertIsInstance(first["elapsed_ms"], float)
        self.assertGreaterEqual(first["elapsed_ms"], 0)
        self.assertEqual(second["nodes"], first["nodes"])
        self.assertEqual(process.requests, [
            {"request_id": 1, "hwnd": 123, "pid": 456},
            {"request_id": 2, "hwnd": 789, "pid": 654},
        ])
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("-NoProfile", command)
        self.assertIn("-NonInteractive", command)
        bootstrap = base64.b64decode(command[-1]).decode("utf-16le")
        packed = re.search(r"FromBase64String\('([^']+)'\)", bootstrap).group(1)
        self.assertEqual(gzip.decompress(base64.b64decode(packed)).decode("utf-8"), reader.host)
        self.assertNotIn("测试商城", " ".join(command))
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)


    def test_cold_and_warm_requests_have_separate_time_budgets(self):
        queues = []

        def response_queue(*args, **kwargs):
            value = Queue(*args, **kwargs)
            value.get = Mock(wraps=value.get)
            queues.append(value)
            return value

        reader, _, _ = self.reader()
        with patch("order_exporter.bridge.queue.Queue", side_effect=response_queue):
            self.assertIsNotNone(reader.read(123, 456))
            self.assertIsNotNone(reader.read(123, 456))
        waits = [call.kwargs["timeout"] for call in queues[0].get.call_args_list]
        self.assertTrue(5 < waits[0] <= 6)
        self.assertTrue(1 < waits[1] <= 2)

    def test_unavailable_provider_and_empty_snapshot_leave_worker_reusable(self):
        process = FakeProcess(lambda r: respond(r, None if r["request_id"] == 1 else snapshot([])))
        reader, process, popen = self.reader(process)
        self.assertIsNone(reader.read(123, 456))
        self.assertFalse(process.terminated)
        self.assertEqual(reader.read(123, 456)["nodes"], [])
        popen.assert_called_once()

    def test_timeout_discards_worker_and_cannot_reuse_a_late_response(self):
        first = FakeProcess(lambda _: None, stubborn=True)
        second = FakeProcess()
        reader, _, popen = self.reader(first, timeout=.02, startup_timeout=.02)
        popen.side_effect = [first, second]
        self.assertIsNone(reader.read(123, 456))
        self.assertTrue(first.terminated)
        self.assertTrue(first.killed)
        self.assertTrue(first.stdin.closed)
        self.assertTrue(first.stdout.closed)
        first.stdout.lines.put(respond({"request_id": 1}, snapshot()))
        result = reader.read(789, 654)
        self.assertIsNotNone(result)
        self.assertEqual(second.requests, [{"request_id": 2, "hwnd": 789, "pid": 654}])
        self.assertEqual(popen.call_count, 2)

    def test_malformed_mismatched_truncated_and_eof_responses_discard_worker(self):
        cases = [
            lambda _: b"not-json\n",
            lambda _: b'{"request_id": true, "result": null}\n',
            lambda r: respond({"request_id": r["request_id"] + 1}, snapshot()),
            lambda _: b'{"request_id": 1}\n',
            lambda _: b'{"request_id": 1, "result": null}',
            lambda _: b"\xff\n",
            lambda _: b"",
            lambda r: respond(r, []),
        ]
        for reply in cases:
            with self.subTest(reply=reply):
                reader, process, _ = self.reader(FakeProcess(reply))
                self.assertIsNone(reader.read(123, 456))
                self.assertTrue(process.terminated)

    def test_oversized_response_discards_worker(self):
        reader, process, _ = self.reader(FakeProcess(lambda _: b"x" * 65 + b"\n"))
        with patch("order_exporter.bridge._MAX_RESPONSE_BYTES", 64):
            self.assertIsNone(reader.read(123, 456))
        self.assertTrue(process.terminated)

    def test_rejects_invalid_geometry_roles_and_selected_flags(self):
        malformed = [
            {"rect": [0, 0, 0, 10], "nodes": []},
            {"rect": [0, 0, float("nan"), 10], "nodes": []},
            {"rect": [0, 0, float("inf"), 10], "nodes": []},
            {"rect": [False, 0, 10, 10], "nodes": []},
            {"rect": [0, 0, 10, 10], "nodes": None},
            snapshot([{"text": "outside", "role": "text", "rect": [0, 0, 10, 10]}]),
            snapshot([{"text": "empty bounds", "role": "text", "rect": [-680, 40, -680, 65]}]),
            snapshot([{"text": " ", "role": "text", "rect": [-680, 40, -380, 65]}]),
            snapshot([{"text": "wrong role", "role": "pane", "rect": [-680, 40, -380, 65]}]),
            snapshot([{"text": "wrong selected", "role": "text", "rect": [-680, 40, -380, 65], "selected": 1}]),
        ]
        for value in malformed:
            with self.subTest(value=value):
                reader, process, _ = self.reader(FakeProcess(lambda r: respond(r, value)))
                self.assertIsNone(reader.read(123, 456))
                self.assertTrue(process.terminated)

    def test_partly_clipped_visible_controls_keep_their_absolute_rectangles(self):
        nodes = [{"text": "partly visible", "role": "button", "rect": [-720, 40, -680, 65]}]
        reader, _, _ = self.reader(FakeProcess(lambda r: respond(r, snapshot(nodes))))
        self.assertEqual(reader.read(123, 456)["nodes"], nodes)

    def test_invalid_targets_and_non_windows_do_not_launch_worker(self):
        reader, _, popen = self.reader()
        for hwnd, pid in ((0, 456), (123, 0), (-1, 456), (True, 456), (123, False),
                          ("123", 456), (None, 456), (2 ** 64, 456), (123, 2 ** 32)):
            self.assertIsNone(reader.read(hwnd, pid))
        with patch("order_exporter.bridge.sys.platform", "linux"):
            self.assertIsNone(reader.read(123, 456))
        popen.assert_not_called()

    def test_launch_failure_and_broken_pipe_return_none(self):
        reader, process, popen = self.reader()
        popen.side_effect = OSError("missing worker")
        self.assertIsNone(reader.read(123, 456))
        popen.side_effect = None
        process.stdin.write = Mock(side_effect=BrokenPipeError)
        self.assertIsNone(reader.read(123, 456))
        self.assertTrue(process.terminated)

    def test_exited_process_is_replaced_and_close_is_idempotent(self):
        first, second = FakeProcess(), FakeProcess()
        reader, _, popen = self.reader(first)
        popen.side_effect = [first, second]
        self.assertIsNotNone(reader.read(123, 456))
        first.returncode = 1
        self.assertIsNotNone(reader.read(123, 456))
        self.assertTrue(first.stdin.closed)
        reader.close()
        reader.close()
        self.assertTrue(second.terminated)
        self.assertTrue(second.stdin.closed)
        self.assertTrue(second.stdout.closed)

    def test_concurrent_calls_are_serialized_with_distinct_request_ids(self):
        reader, process, _ = self.reader()
        values = []
        workers = [threading.Thread(target=lambda h=h: values.append(reader.read(h, 456)))
                   for h in (123, 789)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(values), 2)
        self.assertTrue(all(value is not None for value in values))
        self.assertEqual([r["request_id"] for r in process.requests], [1, 2])
        self.assertEqual({r["hwnd"] for r in process.requests}, {123, 789})

    def test_invoke_uses_same_worker_as_read_and_sends_exact_name_and_absolute_hint(self):
        process = FakeProcess(lambda r: respond(r, True if r.get("action") == "invoke" else snapshot()))
        reader, process, popen = self.reader(process)
        self.assertIsNotNone(reader.read(123, 456))
        self.assertIs(reader.invoke(123, 456, "测试商城", [-680, 40, -380, 65]), True)
        self.assertIsNotNone(reader.read(123, 456))
        self.assertEqual(process.requests[1], {
            "request_id": 2, "action": "invoke", "hwnd": 123, "pid": 456,
            "text": "测试商城", "rect": [-680, 40, -380, 65],
        })
        self.assertNotIn("action", process.requests[0])
        self.assertNotIn("action", process.requests[2])
        popen.assert_called_once()
        self.assertNotIn("测试商城", " ".join(popen.call_args.args[0]))

    def test_non_invoke_target_returns_false_and_allows_a_fresh_read_for_caller_fallback(self):
        process = FakeProcess(lambda r: respond(r, False if r.get("action") == "invoke" else snapshot()))
        reader, process, popen = self.reader(process)
        self.assertIs(reader.invoke(123, 456, "测试商城", [-680, 40, -380, 65]), False)
        self.assertFalse(process.terminated)
        self.assertEqual(reader.read(123, 456)["nodes"], snapshot()["nodes"])
        self.assertEqual([r.get("action", "read") for r in process.requests], ["invoke", "read"])
        popen.assert_called_once()

    def test_invoke_result_unknown_is_distinct_from_not_supported_and_is_never_retried(self):
        reader, process, popen = self.reader(FakeProcess(lambda r: respond(r, None)))
        self.assertIsNone(reader.invoke(123, 456, "测试商城", [-680, 40, -380, 65]))
        self.assertEqual(len(process.requests), 1)
        self.assertFalse(process.terminated)
        popen.assert_called_once()

    def test_invoke_timeout_discards_worker_without_resending_action(self):
        first, second = FakeProcess(lambda _: None), FakeProcess()
        reader, _, popen = self.reader(first, timeout=.02, startup_timeout=.02)
        popen.side_effect = [first, second]
        self.assertIsNone(reader.invoke(123, 456, "测试商城", [-680, 40, -380, 65]))
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(first.requests[0]["action"], "invoke")
        self.assertTrue(first.terminated)
        self.assertIsNotNone(reader.read(123, 456))
        self.assertNotIn("action", second.requests[0])
        self.assertEqual(second.requests[0]["request_id"], 2)

    def test_malformed_invoke_results_are_unknown_and_discard_worker(self):
        replies = [
            lambda r: respond(r, 1),
            lambda r: respond(r, "true"),
            lambda r: respond(r, {}),
            lambda r: respond(r, []),
            lambda r: respond({"request_id": r["request_id"] + 1}, True),
            lambda _: b"broken\n",
            lambda _: b"",
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                reader, process, _ = self.reader(FakeProcess(reply))
                self.assertIsNone(reader.invoke(123, 456, "测试商城", [-680, 40, -380, 65]))
                self.assertTrue(process.terminated)
                self.assertEqual(len(process.requests), 1)

    def test_invalid_invoke_arguments_are_rejected_without_sending_input(self):
        reader, _, popen = self.reader()
        valid = [-680, 40, -380, 65]
        for hwnd, pid in ((0, 456), (123, 0), (True, 456), (123, "456")):
            self.assertIs(reader.invoke(hwnd, pid, "target", valid), False)
        for name in (None, "", " ", 123, "x" * 16385):
            self.assertIs(reader.invoke(123, 456, name, valid), False)
        for rect in (None, (), tuple(valid), [], [1, 2, 3], [0, 0, 0, 10],
                     [False, 0, 10, 10], [0, 0, float("inf"), 10], [0, 0, float("nan"), 10]):
            self.assertIs(reader.invoke(123, 456, "target", rect), False)
        with patch("order_exporter.bridge.sys.platform", "linux"):
            self.assertIs(reader.invoke(123, 456, "target", valid), False)
        popen.assert_not_called()

    def test_invoke_launch_and_pipe_errors_are_unknown(self):
        reader, process, popen = self.reader()
        popen.side_effect = OSError("missing worker")
        self.assertIsNone(reader.invoke(123, 456, "target", [-680, 40, -380, 65]))
        popen.side_effect = None
        process.stdin.flush = Mock(side_effect=BrokenPipeError)
        self.assertIsNone(reader.invoke(123, 456, "target", [-680, 40, -380, 65]))
        self.assertEqual(len(process.requests), 1)
        self.assertTrue(process.terminated)

    def test_read_and_invoke_from_different_threads_share_the_serial_protocol(self):
        process = FakeProcess(lambda r: respond(r, True if r.get("action") == "invoke" else snapshot()))
        reader, process, _ = self.reader(process)
        values = {}
        workers = [
            threading.Thread(target=lambda: values.update(read=reader.read(123, 456))),
            threading.Thread(target=lambda: values.update(invoke=reader.invoke(123, 456, "测试商城", [-680, 40, -380, 65]))),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertIsInstance(values["read"], dict)
        self.assertIs(values["invoke"], True)
        self.assertEqual([r["request_id"] for r in process.requests], [1, 2])


@unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell.exe"), "Windows PowerShell required")
class HostCompileTests(unittest.TestCase):
    def test_host_compiles_with_empty_stdin_without_reading_any_window(self):
        command = BridgeReader()._command()
        self.assertLess(len(subprocess.list2cmdline(command)), 32767)
        result = subprocess.run(command, input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=8, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
