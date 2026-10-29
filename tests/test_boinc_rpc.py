"""Tests for BOINC task-name discovery and safe PID/slot matching."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

APP_PATH = Path(__file__).resolve().parents[1] / "primegrid-affinity-gui.py"
SPEC = importlib.util.spec_from_file_location("primegrid_affinity_gui_boinc_test", APP_PATH)
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class BoincRpcTests(unittest.TestCase):
    def test_parses_active_primegrid_results(self):
        xml = b'''<boinc_gui_rpc_reply><results>
          <result><name>PG_result_alpha</name><project_url>https://www.primegrid.com/</project_url>
            <plan_class>avx2</plan_class>
            <active_task><slot>3</slot><pid>1234</pid></active_task></result>
          <result><name>PG_result_gpu</name><project_url>https://www.primegrid.com/</project_url>
            <plan_class>opencl_nvidia_101</plan_class>
            <active_task><slot>4</slot><pid>5678</pid></active_task></result>
          <result><name>inactive</name><project_url>https://www.primegrid.com/</project_url></result>
          <result><name>other_project</name><project_url>https://example.org/</project_url>
            <active_task><slot>5</slot><pid>9876</pid></active_task></result>
        </results></boinc_gui_rpc_reply>'''
        results = APP.parse_boinc_results_xml(xml)
        self.assertEqual(len(results), 2)
        self.assertEqual((results[0].name, results[0].pid, results[0].slot), ("PG_result_alpha", 1234, 3))
        self.assertFalse(results[0].uses_gpu)
        self.assertTrue(results[1].uses_gpu)

    def make_task(self, pid, name="sllr-worker", slot=None, ppid=1):
        return APP.Task(pid=pid, name=name, command=name, tids=[pid], ppid=ppid,
                        start_ticks=pid * 10, work_slot=slot)

    def test_rpc_requests_active_results_from_local_client(self):
        payload = (b'<boinc_gui_rpc_reply><results><result>'
                   b'<name>PG_result_alpha</name><project_url>https://www.primegrid.com/</project_url>'
                   b'<plan_class>avx2</plan_class>'
                   b'<active_task><slot>3</slot><pid>1234</pid></active_task>'
                   b'</result></results></boinc_gui_rpc_reply>\x03')

        class FakeSocket:
            def __init__(self):
                self.request = b""
                self.sent_reply = False
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def settimeout(self, _timeout):
                pass
            def sendall(self, request):
                self.request = request
            def recv(self, _size):
                if self.sent_reply:
                    return b""
                self.sent_reply = True
                return payload

        fake = FakeSocket()
        with patch.object(APP.socket, "create_connection", return_value=fake):
            results = APP.get_boinc_active_results()
        self.assertIn(b"127.0.0.1", f"{APP.BOINC_RPC_HOST}:{APP.BOINC_RPC_PORT}".encode())
        self.assertIn(b"<get_results>", fake.request)
        self.assertIn(b"<active_only>1</active_only>", fake.request)
        self.assertEqual([(item.name, item.pid, item.slot) for item in results],
                         [("PG_result_alpha", 1234, 3)])
        self.assertEqual(results[0].plan_class, "avx2")
        self.assertFalse(results[0].uses_gpu)

    def test_exact_pid_match_sets_boinc_name_and_pid(self):
        task = self.make_task(1234)
        result = APP.BoincResult("PG_result_alpha", 1234, 3, "https://www.primegrid.com/")
        APP.match_boinc_results([task], [result], related=lambda _a, _b: False)
        self.assertEqual(task.boinc_task_name, "PG_result_alpha")
        self.assertEqual(task.boinc_pid, 1234)
        self.assertEqual(task.display_name, "PG_result_alpha")
        self.assertFalse(task.boinc_uses_gpu)

    def test_unique_slot_match_when_boinc_and_worker_pids_differ(self):
        task = self.make_task(1234, slot=3)
        result = APP.BoincResult("PG_result_alpha", 9999, 3, "https://www.primegrid.com/")
        APP.match_boinc_results([task], [result], related=lambda _a, _b: False)
        self.assertEqual(task.boinc_task_name, "PG_result_alpha")
        self.assertEqual(task.pid, 1234)
        self.assertEqual(task.boinc_pid, 9999)
        self.assertFalse(task.boinc_uses_gpu)

    def test_unique_ancestry_match_when_slot_is_unavailable(self):
        task = self.make_task(1234, slot=None)
        result = APP.BoincResult("PG_result_alpha", 9999, None, "https://www.primegrid.com/")
        APP.match_boinc_results([task], [result], related=lambda a, b: (a, b) == (1234, 9999))
        self.assertEqual(task.boinc_task_name, "PG_result_alpha")

    def test_ambiguous_slot_matches_are_left_unmatched(self):
        tasks = [self.make_task(1234, slot=3), self.make_task(1235, slot=3)]
        results = [APP.BoincResult("result-a", 9001, 3, "https://www.primegrid.com/"),
                   APP.BoincResult("result-b", 9002, 3, "https://www.primegrid.com/")]
        APP.match_boinc_results(tasks, results, related=lambda _a, _b: False)
        self.assertTrue(all(task.boinc_task_name is None for task in tasks))
        self.assertTrue(all("unmatched" in task.display_name for task in tasks))

    def test_rpc_unavailable_is_nonfatal(self):
        with patch.object(APP.socket, "create_connection", side_effect=OSError("not running")):
            self.assertIsNone(APP.get_boinc_active_results())


    def test_gpu_plan_classes_are_marked_as_gpu_tasks(self):
        gpu_classes = ["cudaGFN", "opencl_nvidia_101", "opencl_ati_101", "hip", "rocm", "amdgpu", "GPU"]
        for plan_class in gpu_classes:
            with self.subTest(plan_class=plan_class):
                result = APP.BoincResult("result", 111, 1, "https://www.primegrid.com/", plan_class)
                self.assertTrue(result.uses_gpu)

    def test_matching_active_cpu_and_gpu_results_excludes_gpu_worker(self):
        cpu = self.make_task(100, name="cpu-worker", slot=1)
        gpu = self.make_task(200, name="gpu-worker", slot=2)
        results = [
            APP.BoincResult("PG_cpu_result", 100, 1, "https://www.primegrid.com/", "avx2"),
            APP.BoincResult("PG_gpu_result", 200, 2, "https://www.primegrid.com/", "opencl_nvidia_101"),
        ]
        APP.match_boinc_results([cpu, gpu], results, related=lambda _a, _b: False)
        self.assertEqual(APP.cpu_tasks_only([cpu, gpu]), [cpu])
        self.assertTrue(gpu.boinc_uses_gpu)
        self.assertFalse(cpu.boinc_uses_gpu)

    def test_gpu_resource_metadata_is_detected_even_without_gpu_plan_class(self):
        result = APP.BoincResult(
            "result", 111, 1, "https://www.primegrid.com/", "", "resources coproc_nvidia 1"
        )
        self.assertTrue(result.uses_gpu)

    def test_cpu_task_filter_hides_gpu_and_unclassified_workers(self):
        cpu = self.make_task(100, name="cpu-worker")
        gpu = self.make_task(200, name="gpu-worker")
        unknown = self.make_task(300, name="unknown-worker")
        cpu.boinc_uses_gpu = False
        gpu.boinc_uses_gpu = True
        unknown.boinc_uses_gpu = None
        self.assertEqual(APP.cpu_tasks_only([cpu, gpu, unknown]), [cpu])

    def test_opencl_resource_subtags_are_detected(self):
        xml = b'''<boinc_gui_rpc_reply><results><result>
          <name>PG_gpu</name><project_url>https://www.primegrid.com/</project_url>
          <plan_class></plan_class><resources><coproc_opencl>1</coproc_opencl></resources>
          <active_task><slot>1</slot><pid>123</pid></active_task>
        </result></results></boinc_gui_rpc_reply>'''
        results = APP.parse_boinc_results_xml(xml)
        self.assertEqual(len(results), 1)
        self.assertIn("coproc_opencl", results[0].resources)
        self.assertTrue(results[0].uses_gpu)

    def test_find_tasks_fails_closed_when_boinc_rpc_unavailable(self):
        with patch.object(APP, "get_boinc_active_results", return_value=None):
            tasks = APP.find_tasks()
        self.assertEqual(tasks, [])
        self.assertIn("CPU/GPU task type cannot be confirmed", APP.TASK_DISCOVERY_NOTE)

    def test_boinc_row_uses_result_name_but_keeps_worker_pid_distinct(self):
        task = self.make_task(1234, slot=3)
        task.boinc_task_name = "PG_result_alpha"
        task.boinc_pid = 9999
        self.assertEqual(task.display_name, "PG_result_alpha")
        self.assertEqual(task.pid, 1234)
        self.assertEqual(task.boinc_pid, 9999)


if __name__ == "__main__":
    unittest.main()
