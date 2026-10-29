"""Regression tests for saved CPU-topology fingerprints."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP_PATH = Path(__file__).resolve().parents[1] / "primegrid-affinity-gui.py"
SPEC = importlib.util.spec_from_file_location("primegrid_affinity_topology_test", APP_PATH)
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def make_domains():
    cores_a = [(i, i + 12) for i in range(12)]
    cores_b = [(24 + i, 36 + i) for i in range(12)]
    return [(set(range(24)), cores_a), (set(range(24, 48)), cores_b)]


class TopologyValidationTests(unittest.TestCase):
    def setUp(self):
        self.domains = make_domains()

    def test_topology_fingerprint_is_stable(self):
        self.assertEqual(APP.topology_fingerprint(self.domains), APP.topology_fingerprint(make_domains()))

    def test_domain_fingerprint_is_stable(self):
        self.assertEqual(APP.domain_fingerprint(self.domains, 0), APP.domain_fingerprint(make_domains(), 0))

    def test_fingerprint_changes_when_online_cpu_set_changes(self):
        changed = make_domains()
        changed[0][0].remove(23)
        changed[0][1][:] = changed[0][1][:-1]
        self.assertNotEqual(APP.topology_fingerprint(self.domains), APP.topology_fingerprint(changed))

    def test_fingerprint_changes_when_smt_sibling_mapping_changes(self):
        changed = make_domains()
        changed[0][1][0] = (0, 47)
        self.assertNotEqual(APP.topology_fingerprint(self.domains), APP.topology_fingerprint(changed))

    def test_profile_with_matching_fingerprints_is_current(self):
        settings = {"ccd_index": 1, **APP.topology_profile_metadata(self.domains, 1)}
        self.assertEqual(APP.profile_topology_status(settings, self.domains), "current")

    def test_legacy_profile_without_fingerprints_requires_review(self):
        settings = {"ccd_index": 0, "core_indices": [0, 2, 4]}
        self.assertEqual(APP.profile_topology_status(settings, self.domains), "unverified")

    def test_profile_requires_review_when_any_part_of_topology_changes(self):
        settings = {"ccd_index": 0, **APP.topology_profile_metadata(self.domains, 0)}
        changed = make_domains()
        changed[1][0].remove(47)
        changed[1][1][:] = changed[1][1][:-1]
        self.assertEqual(APP.profile_topology_status(settings, changed), "changed")

    def test_profile_requires_review_if_selected_domain_disappears(self):
        settings = {"ccd_index": 1, **APP.topology_profile_metadata(self.domains, 1)}
        self.assertEqual(APP.profile_topology_status(settings, self.domains[:1]), "changed")

    def test_save_profile_only_stores_topology_fingerprints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            config_file = state_dir / "settings.json"
            lock_file = state_dir / "settings.lock"
            with patch.object(APP, "STATE_DIR", state_dir), \
                 patch.object(APP, "CONFIG_FILE", config_file), \
                 patch.object(APP, "LOCK_FILE", lock_file):
                APP.save_profile_only("test-task", "1", 0, [0, 2, 4], domains=self.domains)
                data = json.loads(config_file.read_text())
                settings = data["apps"]["test-task"]["slots"]["1"]
                self.assertEqual(settings["topology_signature"], APP.topology_fingerprint(self.domains))
                self.assertEqual(settings["domain_signature"], APP.domain_fingerprint(self.domains, 0))
                self.assertEqual(settings["domain_cpu_list"], sorted(self.domains[0][0]))
                self.assertEqual(APP.profile_topology_status(settings, self.domains), "current")

    def test_monitor_has_guard_before_affinity_application(self):
        source = APP_PATH.read_text()
        guard = source.index('topology_state = profile_topology_status(settings, domains)')
        skip = source.index('continue', guard)
        apply = source.index('apply_task_affinity(identity, ccd_index, core_indices, domains=domains)', guard)
        self.assertLess(guard, skip)
        self.assertLess(skip, apply)


if __name__ == "__main__":
    unittest.main()
