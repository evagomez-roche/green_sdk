import os
import time
import uuid
import sqlite3

# Use the OS certificate store so HTTPS calls (Boavizta, CodeCarbon geo lookup) work
# behind corporate TLS-inspection proxies whose root CA is not in certifi's bundle.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from datetime import datetime
from codecarbon import OfflineEmissionsTracker, EmissionsTracker

DB_FILE = "green_telemetry.db"
SDK_VERSION = "v2.0.2"

# --- CLEER default constants (Path B) -------------------------------------
CLEER_E_IN_KWH_PER_1K = 0.00032      # 0.32 Wh per 1k input tokens
CLEER_E_OUT_KWH_PER_1K = 0.00096     # 0.96 Wh per 1k output tokens
CLEER_CACHE_MULTIPLIER = 0.15        # cached input tokens cost 15% of a fresh token
CLEER_DEV_AMORTIZATION = 1.5         # training / development amortization
CLEER_HOST_OVERHEAD = 1.5            # host (CPU/RAM/network) overhead on accelerator energy
CLEER_IDLE_FACTOR = 1.1              # idle capacity factor
CLEER_PUE = 1.45                     # datacenter Power Usage Effectiveness
CLEER_GRID_INTENSITY_G_PER_KWH = 350.0
CLEER_EMBODIED_ACC_G_PER_KWH = 90.0  # embodied fallback (accelerator + dev share)
CLEER_EMBODIED_FAC_G_PER_KWH = 7.0   # embodied fallback (facility share) -> 97 gCO2e/kWh total

# --- Boavizta -------------------------------------------------------------
BOAVIZTA_URL = "https://api.boavizta.org/v1/cloud/instance"
BOAVIZTA_PROVIDERS = {"AWS": "aws", "GCP": "gcp", "AZURE": "azure"}
# Boavizta's `embedded` value for a cloud instance is already the instance's share
# of the host's manufacturing impact over its default 6-year lifespan.
BOAVIZTA_LIFESPAN_S = 6 * 365 * 24 * 3600

# CodeCarbon CPU tracking modes -> MEASUREMENT_METHOD labels
_CPU_MODE_LABELS = {
    "intel_rapl": "rapl",
    "rapl": "rapl",
    "tdp": "tdp_estimate",
    "constant": "constant",
    "intel_power_gadget": "power_gadget",
    "apple_powermetrics": "powermetrics",
    "windows_emi": "windows_emi",
}


def init_local_db(db_path: str = DB_FILE):
    """Initializes SQLite with v1.0.0 + v2.0.0 (CLEER + Boavizta) schema."""
    with sqlite3.connect(db_path, timeout=15.0) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS CO_SOFTWARE_CARBON_INTENSITY (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                EFFECTIVE_DATE TEXT, EMBODIED_EMISSIONS_GCO2E REAL, ENERGY_CONSUMED_KWH REAL,
                EXECUTION_DATE TEXT, FUNCTIONAL_UNIT_TX INTEGER, FUNCTIONAL_UNIT_NAME TEXT,
                PROCESS_DESC TEXT, PROJECT_NAME TEXT, REGION TEXT, SCI_SCORE_GCO2E_TX REAL,
                SCI_TRACKER_ID TEXT, TOTAL_CARBON_FOOTPRINT_GCO2E REAL, IS_AI INTEGER DEFAULT 0,
                AI_MODEL_NAME TEXT, PROMPT_TOKENS INTEGER DEFAULT 0, COMPLETION_TOKENS INTEGER DEFAULT 0,
                IS_SYNCED INTEGER DEFAULT 0, MEASUREMENT_PERIOD TEXT, PROVIDER TEXT DEFAULT 'Unknown',
                MEASUREMENT_METHOD TEXT DEFAULT 'unknown', SDK_VERSION TEXT DEFAULT 'v1.0.0'
            )
        """)

        alter_queries = [
            "ALTER TABLE CO_SOFTWARE_CARBON_INTENSITY ADD COLUMN CACHED_TOKENS INTEGER DEFAULT 0",
            "ALTER TABLE CO_SOFTWARE_CARBON_INTENSITY ADD COLUMN BASELINE_SCI_GCO2E REAL DEFAULT 0.0",
            "ALTER TABLE CO_SOFTWARE_CARBON_INTENSITY ADD COLUMN CLEER_SCI_GCO2E REAL DEFAULT 0.0",
            "ALTER TABLE CO_SOFTWARE_CARBON_INTENSITY ADD COLUMN OPTIMIZATION_YIELD_PCT REAL DEFAULT 0.0",
            "ALTER TABLE CO_SOFTWARE_CARBON_INTENSITY ADD COLUMN INSTANCE_TYPE TEXT DEFAULT 'Unknown'"
        ]
        for query in alter_queries:
            try:
                cursor.execute(query)
            except sqlite3.OperationalError:
                pass
        conn.commit()

class GreenLogger:
    def __init__(
        self, project_id: str, step_name: str, functional_unit_name: str, functional_units: int,
        is_ai: bool = False, model_name: str = None, prompt_tokens: int = 0, cached_tokens: int = 0,
        completion_tokens: int = 0, measurement_period: str = None, provider: str = "Unknown",
        country_iso_code: str = None, embodied_carbon_rate: float = 0.0, instance_type: str = None,
        db_path: str = DB_FILE, duration_seconds: float = None
    ):
        if not functional_unit_name or functional_unit_name.strip().lower() in ["", "transaction", "test"]:
            raise ValueError("🚨 GREEN OPS ERROR: You must specify a real 'functional_unit_name'.")
        if not isinstance(functional_units, int) or functional_units <= 0:
            raise ValueError("🚨 GREEN OPS ERROR: 'functional_units' must be > 0.")
        if is_ai and not model_name:
            raise ValueError("🚨 GREEN OPS ERROR: When 'is_ai=True', provide a 'model_name'.")
        if duration_seconds is not None and duration_seconds < 0:
            raise ValueError("🚨 GREEN OPS ERROR: 'duration_seconds' must be >= 0.")

        self.project_id = project_id
        self.step_name = step_name
        self.functional_units = functional_units
        self.functional_unit_name = functional_unit_name
        self.is_ai = is_ai
        self.model_name = model_name
        self.prompt_tokens = prompt_tokens or 0
        self.cached_tokens = cached_tokens or 0
        self.completion_tokens = completion_tokens or 0
        self.measurement_period = measurement_period
        self.provider = provider
        self.country_iso_code = country_iso_code
        self.embodied_carbon_rate = embodied_carbon_rate
        self.instance_type = instance_type
        self.db_path = db_path
        self.duration_seconds = duration_seconds
        self.row_id = None

        # save_to_file=False: do not drop an emissions.csv into the user's working directory
        if self.country_iso_code:
            self.tracker = OfflineEmissionsTracker(
                country_iso_code=self.country_iso_code, log_level="error", save_to_file=False
            )
        else:
            self.tracker = EmissionsTracker(log_level="error", save_to_file=False)

    def _fetch_boavizta_m(self, duration_s):
        """Fetches hardware embodied carbon (M, in gCO2e) for `duration_s` from the Boavizta API.

        Only used for Path A on a known public cloud: in AI mode the local runner's
        instance_type says nothing about the remote GPU servers doing the inference.
        """
        if not self.instance_type or self.is_ai:
            return None
        prov = BOAVIZTA_PROVIDERS.get(str(self.provider).strip().upper())
        if prov is None:
            return None

        try:
            payload = {"provider": prov, "instance_type": self.instance_type}
            res = requests.post(BOAVIZTA_URL, json=payload, timeout=3)

            if res.status_code == 200:
                data = res.json()
                # Embodied (manufacturing) impact in kgCO2eq, already apportioned to this
                # instance over Boavizta's 6-year default lifespan.
                m_kg = data.get('impacts', {}).get('gwp', {}).get('embedded', {}).get('value', 0.0)
                if isinstance(m_kg, (int, float)) and m_kg > 0:
                    m_g = m_kg * 1000.0
                    return (m_g / BOAVIZTA_LIFESPAN_S) * duration_s
        except Exception as e:
            print(f"⚠️ [BOAVIZTA] Fallo al obtener LCA para '{self.instance_type}': {e}. Usando fallback.")
        return None

    def _calculate_cleer_impact(self, exact_m_value=None):
        """Path B: Generative AI CLEER Mathematical Model (With Boavizta Injection Capability)

        `carbon_g` is operational carbon only (E * I); embodied carbon is returned
        separately in `m_cleer_used` so callers never double-count M.
        """
        e_in_kwh, e_out_kwh, discount_r = CLEER_E_IN_KWH_PER_1K, CLEER_E_OUT_KWH_PER_1K, CLEER_CACHE_MULTIPLIER
        gross_in_tokens = self.prompt_tokens + self.cached_tokens

        e_acc_baseline = ((gross_in_tokens / 1000.0) * e_in_kwh) + ((self.completion_tokens / 1000.0) * e_out_kwh)
        e_acc_cleer = ((self.prompt_tokens / 1000.0) * e_in_kwh) + \
                      ((self.cached_tokens / 1000.0) * (e_in_kwh * discount_r)) + \
                      ((self.completion_tokens / 1000.0) * e_out_kwh)

        e_dev_base = e_acc_baseline * CLEER_DEV_AMORTIZATION
        e_dev_cleer = e_acc_cleer * CLEER_DEV_AMORTIZATION

        def facility(e_acc, e_dev):
            return (e_acc * CLEER_HOST_OVERHEAD * CLEER_IDLE_FACTOR * CLEER_PUE) + \
                   (e_dev * CLEER_HOST_OVERHEAD * CLEER_PUE)

        e_fac_base = facility(e_acc_baseline, e_dev_base)
        e_fac_cleer = facility(e_acc_cleer, e_dev_cleer)

        # HYBRID LOGIC: Real Data > Fallback
        if exact_m_value is not None:
            m_base = exact_m_value
            m_cleer = exact_m_value
        else:
            m_base = (e_acc_baseline + e_dev_base) * CLEER_EMBODIED_ACC_G_PER_KWH + (e_fac_base * CLEER_EMBODIED_FAC_G_PER_KWH)
            m_cleer = (e_acc_cleer + e_dev_cleer) * CLEER_EMBODIED_ACC_G_PER_KWH + (e_fac_cleer * CLEER_EMBODIED_FAC_G_PER_KWH)

        grid_intensity = CLEER_GRID_INTENSITY_G_PER_KWH
        return {
            "energy_kwh": e_fac_cleer,
            "carbon_g": (e_fac_cleer * grid_intensity),
            "baseline_carbon_g": (e_fac_base * grid_intensity) + m_base,
            "m_cleer_used": m_cleer
        }

    def _final_emissions_data(self):
        return getattr(self.tracker, 'final_emissions_data', None)

    def _detect_measurement_method(self):
        """Reports how CodeCarbon actually measured CPU power (rapl / tdp_estimate / constant / ...)."""
        # 1. CodeCarbon's hardware objects carry the real CPU tracking mode
        for hw in getattr(self.tracker, '_hardware', None) or []:
            mode = getattr(hw, '_mode', None)
            if type(hw).__name__ == 'CPU' and mode:
                return _CPU_MODE_LABELS.get(str(mode).lower(), str(mode).lower())
        # 2. Some CodeCarbon versions store it in the tracker config
        conf = getattr(self.tracker, '_conf', None)
        if isinstance(conf, dict):
            mode = conf.get('cpu_tracking_mode') or conf.get('cpu_mode')
            if mode:
                return _CPU_MODE_LABELS.get(str(mode).lower(), str(mode).lower())
        # 3. Last resort: no CPU power reading at all means nothing was measured
        data = self._final_emissions_data()
        if data is not None and not (getattr(data, 'cpu_power', 0.0) or 0.0):
            return "unknown"
        return "rapl" if os.path.exists('/sys/class/powercap/intel-rapl') else "tdp_estimate"

    def __enter__(self):
        self.start_time = time.time()
        self.tracker.start()
        self.row_id = self._insert_initial_row()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        wall_time_seconds = time.time() - self.start_time
        execution_time_seconds = self.duration_seconds if self.duration_seconds is not None else wall_time_seconds

        try:
            emissions_kg = self.tracker.stop()
        except Exception as e:
            print(f"⚠️ [GREEN LOG] CodeCarbon failed to stop cleanly: {e}")
            emissions_kg = None
        local_carbon_g = emissions_kg * 1000.0 if emissions_kg else 0.0

        data = self._final_emissions_data()
        if data is not None:
            local_energy_kwh = sum(
                (getattr(data, attr, 0.0) or 0.0) for attr in ('cpu_energy', 'gpu_energy', 'ram_energy')
            )
        else:
            local_energy_kwh = 0.0

        local_method = self._detect_measurement_method()
        method_str = local_method

        # Buscamos el LCA del hardware real primero (solo Path A en AWS/GCP/Azure)
        m_exact = self._fetch_boavizta_m(execution_time_seconds)

        remote_energy_kwh, remote_carbon_g = 0.0, 0.0
        baseline_sci, cleer_sci, optimization_yield = 0.0, 0.0, 0.0

        if not self.is_ai:
            # Path A: Usar Boavizta M si existe, sino fallback a rate manual
            M_allocated = m_exact if m_exact is not None else (self.embodied_carbon_rate * execution_time_seconds)
            if m_exact is not None: method_str += " + boavizta_m"
        else:
            # Path B: CLEER (embodied carbon from CLEER fallback constants)
            cleer_metrics = self._calculate_cleer_impact(exact_m_value=m_exact)
            method_str = f"{local_method} + CLEER_v2"
            if m_exact is not None: method_str += " (Boavizta Hardware)"

            M_allocated = cleer_metrics["m_cleer_used"]
            remote_energy_kwh = cleer_metrics["energy_kwh"]
            remote_carbon_g = cleer_metrics["carbon_g"]  # operational only, excludes M
            baseline_sci = cleer_metrics["baseline_carbon_g"] / self.functional_units
            # Include M so CLEER is compared apples-to-apples with the baseline
            cleer_sci = (remote_carbon_g + M_allocated) / self.functional_units
            optimization_yield = ((baseline_sci - cleer_sci) / baseline_sci) * 100 if baseline_sci > 0 else 0.0

        total_energy = local_energy_kwh + remote_energy_kwh
        total_carbon = local_carbon_g + remote_carbon_g
        sci_score = (total_carbon + M_allocated) / self.functional_units

        if self.country_iso_code:
            detected_region = self.country_iso_code
        else:
            detected_region = getattr(data, 'country_iso_code', None) or 'Unknown'
        final_period = self.measurement_period or (f"{execution_time_seconds/60:.2f} Minutes" if execution_time_seconds >= 60 else f"{execution_time_seconds:.2f} Seconds")

        process_desc = f"{self.step_name} {'[AI]' if self.is_ai else '[Standard]'}"
        if exc_type is not None:
            process_desc = f"[FAILED] {process_desc}"
            method_str = "failed_run"

        payload = {
            "EFFECTIVE_DATE": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "EMBODIED_EMISSIONS_GCO2E": float(M_allocated),
            "ENERGY_CONSUMED_KWH": float(total_energy),
            "EXECUTION_DATE": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "PROCESS_DESC": process_desc,
            "REGION": detected_region, "SCI_SCORE_GCO2E_TX": float(sci_score),
            "TOTAL_CARBON_FOOTPRINT_GCO2E": float(total_carbon),
            "MEASUREMENT_PERIOD": final_period, "MEASUREMENT_METHOD": method_str,
            "SDK_VERSION": SDK_VERSION, "PROMPT_TOKENS": self.prompt_tokens,
            "COMPLETION_TOKENS": self.completion_tokens, "CACHED_TOKENS": self.cached_tokens,
            "PROVIDER": self.provider, "AI_MODEL_NAME": self.model_name,
            "BASELINE_SCI_GCO2E": float(baseline_sci), "CLEER_SCI_GCO2E": float(cleer_sci),
            "OPTIMIZATION_YIELD_PCT": float(optimization_yield),
            "INSTANCE_TYPE": self.instance_type or 'Unknown'
        }

        if self.row_id:
            self._update_final_row(payload)
        return False  # never swallow the user's exception

    def _insert_initial_row(self):
        try:
            init_local_db(self.db_path)
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                cursor = conn.cursor()
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # Unique per run so individual executions can be told apart
                tracker_id = f"{self.project_id}-{uuid.uuid4().hex[:8]}"

                cursor.execute("""
                    INSERT INTO CO_SOFTWARE_CARBON_INTENSITY (
                        EFFECTIVE_DATE, EXECUTION_DATE, FUNCTIONAL_UNIT_TX, FUNCTIONAL_UNIT_NAME, PROCESS_DESC, PROJECT_NAME,
                        SCI_TRACKER_ID, IS_AI, AI_MODEL_NAME, PROMPT_TOKENS, COMPLETION_TOKENS, CACHED_TOKENS, PROVIDER, MEASUREMENT_METHOD, SDK_VERSION, INSTANCE_TYPE
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'incomplete_run', ?, ?)
                """, (
                    current_time, current_time, int(self.functional_units), self.functional_unit_name,
                    f"[INCOMPLETE] {self.step_name}", self.project_id, tracker_id, 1 if self.is_ai else 0,
                    self.model_name if self.is_ai else None, int(self.prompt_tokens), int(self.completion_tokens),
                    int(self.cached_tokens), self.provider, SDK_VERSION, self.instance_type or 'Unknown'
                ))
                return cursor.lastrowid
        except Exception as e:
            print(f"❌ [GREEN LOG] Failed inserting initial row: {e}")
            return None

    def _update_final_row(self, p: dict):
        try:
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE CO_SOFTWARE_CARBON_INTENSITY
                    SET EFFECTIVE_DATE=?, EMBODIED_EMISSIONS_GCO2E=?, ENERGY_CONSUMED_KWH=?, EXECUTION_DATE=?, PROCESS_DESC=?,
                        REGION=?, SCI_SCORE_GCO2E_TX=?, TOTAL_CARBON_FOOTPRINT_GCO2E=?, MEASUREMENT_PERIOD=?, MEASUREMENT_METHOD=?,
                        SDK_VERSION=?, PROMPT_TOKENS=?, COMPLETION_TOKENS=?, CACHED_TOKENS=?, PROVIDER=?, AI_MODEL_NAME=?,
                        BASELINE_SCI_GCO2E=?, CLEER_SCI_GCO2E=?, OPTIMIZATION_YIELD_PCT=?, INSTANCE_TYPE=?
                    WHERE ID=?
                """, (
                    p["EFFECTIVE_DATE"], p["EMBODIED_EMISSIONS_GCO2E"], p["ENERGY_CONSUMED_KWH"], p["EXECUTION_DATE"],
                    p["PROCESS_DESC"], p["REGION"], p["SCI_SCORE_GCO2E_TX"], p["TOTAL_CARBON_FOOTPRINT_GCO2E"],
                    p["MEASUREMENT_PERIOD"], p["MEASUREMENT_METHOD"], p["SDK_VERSION"], p["PROMPT_TOKENS"],
                    p["COMPLETION_TOKENS"], p["CACHED_TOKENS"], p["PROVIDER"], p["AI_MODEL_NAME"],
                    p["BASELINE_SCI_GCO2E"], p["CLEER_SCI_GCO2E"], p["OPTIMIZATION_YIELD_PCT"], p["INSTANCE_TYPE"], self.row_id
                ))
        except Exception as e:
            print(f"❌ [GREEN LOG] Failed updating final SQLite row: {e}")
