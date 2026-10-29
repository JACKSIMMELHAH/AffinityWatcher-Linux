"""Regression tests for PrimeGrid Affinity explicit physical-core selection."""
import importlib.util
from pathlib import Path
import sys
import unittest

APP_PATH = Path(__file__).resolve().parents[1] / "primegrid-affinity-gui.py"
SPEC = importlib.util.spec_from_file_location("primegrid_affinity_gui_test", APP_PATH)
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def make_domains():
    # Two simulated 12-core CCDs. Tuples model SMT siblings; the app uses one
    # logical CPU (the first entry) from each physical-core tuple.
    cores_a = [(i, i + 12) for i in range(12)]
    cores_b = [(24 + i, 36 + i) for i in range(12)]
    return [(set(range(24)), cores_a), (set(range(24, 48)), cores_b)]


class CpuPoolTests(unittest.TestCase):
    def setUp(self):
        self.domains = make_domains()

    def test_checkbox_selection_can_be_noncontiguous(self):
        selected = [0, 2, 5, 7]
        self.assertEqual(APP.targets_for(self.domains, 0, selected), {0, 2, 5, 7})
        self.assertEqual(APP.format_core_indices(selected), "1,3,6,8")

    def test_checkbox_selection_is_normalized_to_sorted_indices(self):
        selected = APP.normalize_core_indices([7, 2, 5], 12)
        self.assertEqual(selected, [2, 5, 7])
        self.assertEqual(APP.targets_for(self.domains, 0, selected), {2, 5, 7})

    def test_can_split_one_ccd_into_disjoint_noncontiguous_pools(self):
        first = APP.targets_for(self.domains, 0, [0, 2, 4, 6, 8, 10])
        second = APP.targets_for(self.domains, 0, [1, 3, 5, 7, 9, 11])
        self.assertEqual(first, {0, 2, 4, 6, 8, 10})
        self.assertEqual(second, {1, 3, 5, 7, 9, 11})
        self.assertFalse(first & second)

    def test_profiles_on_different_ccds_do_not_overlap(self):
        first = APP.targets_for(self.domains, 0, [0, 2, 4, 6])
        second = APP.targets_for(self.domains, 1, [0, 2, 4, 6])
        self.assertFalse(first & second)

    def test_new_explicit_profiles_round_trip(self):
        settings = {"ccd_index": 0, "core_indices": [1, 4, 7], "core_count": 3}
        indices = APP.core_indices_from_settings(settings, 12)
        self.assertEqual(indices, [1, 4, 7])
        self.assertEqual(APP.targets_for(self.domains, 0, indices), {1, 4, 7})

    def test_legacy_profiles_migrate_to_contiguous_selection(self):
        self.assertEqual(APP.core_indices_from_settings({"core_count": 4}, 12), [0, 1, 2, 3])
        self.assertEqual(APP.core_indices_from_settings({"core_start": 5, "core_count": 3}, 12), [5, 6, 7])

    def test_saved_noncontiguous_profiles_do_not_report_false_overlap(self):
        config = {"apps": {
            "task-a": {"slots": {"1": {"ccd_index": 0, "core_indices": [0, 2, 4]}}},
            "task-b": {"slots": {"1": {"ccd_index": 0, "core_indices": [1, 3, 5]}}},
        }}
        self.assertEqual(APP.saved_profile_overlaps(config, self.domains), [])

    def test_saved_noncontiguous_profiles_report_actual_overlap(self):
        config = {"apps": {
            "task-a": {"slots": {"1": {"ccd_index": 0, "core_indices": [0, 2, 4]}}},
            "task-b": {"slots": {"1": {"ccd_index": 0, "core_indices": [2, 3, 5]}}},
        }}
        overlaps = APP.saved_profile_overlaps(config, self.domains)
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["cpus"], {2})

    def test_legacy_profiles_still_detect_overlaps(self):
        config = {"apps": {
            "task-a": {"slots": {"1": {"ccd_index": 0, "core_count": 6}}},
            "task-b": {"slots": {"1": {"ccd_index": 0, "core_count": 6, "core_start": 0}}},
        }}
        overlaps = APP.saved_profile_overlaps(config, self.domains)
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["cpus"], set(range(6)))

    def test_proposed_disjoint_explicit_selection_has_no_conflict(self):
        config = {"apps": {"task-a": {"slots": {"1": {"ccd_index": 0, "core_indices": [0, 2, 4]}}}}}
        conflicts = APP.profile_overlaps(config, self.domains, "task-b", "1", 0, [1, 3, 5])
        self.assertEqual(conflicts, [])

    def test_proposed_overlapping_explicit_selection_reports_shared_cpus(self):
        config = {"apps": {"task-a": {"slots": {"1": {"ccd_index": 0, "core_indices": [0, 2, 4]}}}}}
        conflicts = APP.profile_overlaps(config, self.domains, "task-b", "1", 0, [2, 5, 7])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["cpus"], {2})

    def test_editing_profile_does_not_conflict_with_itself(self):
        config = {"apps": {"task-a": {"slots": {"1": {"ccd_index": 0, "core_indices": [0, 2, 4]}}}}}
        conflicts = APP.profile_overlaps(config, self.domains, "task-a", "1", 0, [0, 2, 4])
        self.assertEqual(conflicts, [])

    def test_rejects_empty_selection(self):
        with self.assertRaises(RuntimeError):
            APP.targets_for(self.domains, 0, [])

    def test_rejects_duplicate_core_selection(self):
        with self.assertRaises(RuntimeError):
            APP.targets_for(self.domains, 0, [0, 2, 2])

    def test_rejects_core_outside_selected_ccd(self):
        with self.assertRaises(RuntimeError):
            APP.targets_for(self.domains, 0, [0, 12])

    def test_legacy_contiguous_target_api_still_works(self):
        self.assertEqual(APP.targets_for(self.domains, 0, 6, 6), set(range(6, 12)))


if __name__ == "__main__":
    unittest.main()
