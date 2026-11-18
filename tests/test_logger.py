"""Unit tests for roche_green.logger (no network, no real CodeCarbon required).

Run with:  python -m unittest discover -s tests -v
"""
import os
import sys
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# --- Fake CodeCarbon ------------------------------------------------------
class _FakeEmissionsData:
    def __init__(self):
        self.cpu_energy = 0.001
        self.ram_energy = 0.0005
        self.gpu_energy = 0.002
        self.cpu_power = 42.0
        self.country_iso_code = "ESP"


class _FakeCPU:
    def __init__(self, mode):
        self._mode = mode


_FakeCPU.__name__ = "CPU"


class _FakeTracker:
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.final_emissions_data = None
        self._hardware = [_FakeCPU("intel_rapl")]
        _FakeTracker.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.final_emissions_data = _FakeEmissionsData()
        # Mimic real CodeCarbon: write emissions.csv unless save_to_file=False
        if self.kwargs.get("save_to_file", True):
            with open("emissions.csv", "w") as f:
                f.write("fake\n")
        return 0.0001  # kg -> 0.1 g


try:
    import codecarbon  # noqa: F401
    _REAL_CODECARBON = True
except ImportError:
    _REAL_CODECARBON = False
    fake = types.ModuleType("codecarbon")
    fake.EmissionsTracker = _FakeTracker
    fake.OfflineEmissionsTracker = _FakeTracker
    sys.modules["codecarbon"] = fake

from roche_green import logger as gl  # noqa: E402


class GreenLoggerTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)
        self.db = os.path.join(self._tmp.name, "t.db")
        # Always use the fake tracker so results are deterministic
        self._patches = [
            mock.patch.object(gl, "EmissionsTracker", _FakeTracker),
            mock.patch.object(gl, "OfflineEmissionsTracker", _FakeTracker),
            mock.patch.object(gl.requests, "post", side_effect=AssertionError("no network in tests")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _row(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM CO_SOFTWARE_CARBON_INTENSITY").fetchone())

    def _ai_logger(self, **kw):
        args = dict(project_id="P", step_name="LLM", functional_unit_name="API request",
                    functional_units=1, is_ai=True, model_name="m", prompt_tokens=1000,
                    completion_tokens=500, db_path=self.db)
        args.update(kw)
        return gl.GreenLogger(**args)

    # 1. cached_tokens=0 -> 0% savings, M not double-counted
    def test_no_cache_zero_yield_and_no_double_count(self):
        lg = self._ai_logger(cached_tokens=0, duration_seconds=2.5)
        with lg:
            pass
        row = self._row()
        self.assertEqual(row["OPTIMIZATION_YIELD_PCT"], 0.0)
        self.assertAlmostEqual(row["BASELINE_SCI_GCO2E"], row["CLEER_SCI_GCO2E"])

        cleer = lg._calculate_cleer_impact()
        m = cleer["m_cleer_used"]
        local_g = 0.1
        expected_sci = (local_g + cleer["carbon_g"] + m) / 1
        self.assertAlmostEqual(row["SCI_SCORE_GCO2E_TX"], expected_sci)
        self.assertAlmostEqual(row["EMBODIED_EMISSIONS_GCO2E"], m)
        self.assertAlmostEqual(row["TOTAL_CARBON_FOOTPRINT_GCO2E"], local_g + cleer["carbon_g"])
        self.assertAlmostEqual(row["CLEER_SCI_GCO2E"], cleer["carbon_g"] + m)
        self.assertEqual(row["MEASUREMENT_PERIOD"], "2.50 Seconds")
        self.assertEqual(row["MEASUREMENT_METHOD"], "rapl + CLEER_v2")
        self.assertEqual(row["REGION"], "ESP")
        self.assertEqual(row["SDK_VERSION"], "v2.0.2")
        self.assertTrue(row["PROCESS_DESC"].startswith("LLM"))
        # gpu_energy included
        self.assertAlmostEqual(row["ENERGY_CONSUMED_KWH"], 0.0035 + cleer["energy_kwh"])

    def test_cache_produces_positive_yield(self):
        with self._ai_logger(cached_tokens=4000):
            pass
        self.assertGreater(self._row()["OPTIMIZATION_YIELD_PCT"], 0.0)

    def test_tokens_set_inside_block_are_recorded(self):
        with self._ai_logger(prompt_tokens=0, completion_tokens=0) as g:
            g.prompt_tokens, g.cached_tokens, g.completion_tokens = 800, 200, 100
        row = self._row()
        self.assertEqual((row["PROMPT_TOKENS"], row["CACHED_TOKENS"], row["COMPLETION_TOKENS"]), (800, 200, 100))
        self.assertGreater(row["OPTIMIZATION_YIELD_PCT"], 0.0)

    # 2. Exception -> [FAILED]
    def test_exception_marks_row_failed(self):
        with self.assertRaises(RuntimeError):
            with gl.GreenLogger(project_id="P", step_name="ETL", functional_unit_name="rows",
                                functional_units=10, db_path=self.db):
                raise RuntimeError("boom")
        row = self._row()
        self.assertTrue(row["PROCESS_DESC"].startswith("[FAILED]"), row["PROCESS_DESC"])
        self.assertNotIn("[INCOMPLETE]", row["PROCESS_DESC"])
        self.assertEqual(row["MEASUREMENT_METHOD"], "failed_run")

    # 3. No emissions.csv in cwd
    def test_no_emissions_csv_created(self):
        for iso in (None, "ESP"):
            with gl.GreenLogger(project_id="P", step_name="ETL", functional_unit_name="rows",
                                functional_units=10, db_path=self.db, country_iso_code=iso):
                pass
            self.assertIs(_FakeTracker.instances[-1].kwargs.get("save_to_file"), False)
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "emissions.csv")))

    def test_tracker_id_unique_per_run(self):
        for _ in range(2):
            with gl.GreenLogger(project_id="P", step_name="ETL", functional_unit_name="rows",
                                functional_units=1, db_path=self.db):
                pass
        with sqlite3.connect(self.db) as conn:
            ids = [r[0] for r in conn.execute("SELECT SCI_TRACKER_ID FROM CO_SOFTWARE_CARBON_INTENSITY")]
        self.assertEqual(len(set(ids)), 2)

    def test_boavizta_skipped_for_ai_and_unknown_provider(self):
        self.assertIsNone(self._ai_logger(provider="AWS", instance_type="m5.large")._fetch_boavizta_m(10))
        lg = gl.GreenLogger(project_id="P", step_name="s", functional_unit_name="rows",
                            functional_units=1, provider="On-Premise", instance_type="x", db_path=self.db)
        self.assertIsNone(lg._fetch_boavizta_m(10))

    def test_boavizta_reads_embedded_and_prorates_6y(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"impacts": {"gwp": {"manufacture": {"value": 0},
                                                      "embedded": {"value": 1000.0}}}}
        lg = gl.GreenLogger(project_id="P", step_name="s", functional_unit_name="rows",
                            functional_units=1, provider="aws", instance_type="m5.large", db_path=self.db)
        with mock.patch.object(gl.requests, "post", return_value=resp) as post:
            m = lg._fetch_boavizta_m(3600)
        post.assert_called_once()
        self.assertAlmostEqual(m, 1000.0 * 1000.0 / (6 * 365 * 24 * 3600) * 3600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
