# 🌍 Roche Green SDK 2.0 - Integration Guide (Onboarding Playbook)

> Current version: **v2.0.2**

Welcome to the **Roche Green SDK 2.0**. This tool allows you to measure the carbon footprint of your software and calculate the Software Carbon Intensity (SCI) of your code, whether it is traditional processing (CPU/RAM) or Artificial Intelligence inference (SCI for AI).

The SDK is built on a **Dual-Path Architecture** that calculates environmental impact directly in your local environment. The only optional network calls are CodeCarbon's IP geolocation (skipped when you pass `country_iso_code`) and the Boavizta hardware LCA lookup (only for standard code on AWS/GCP/Azure with an `instance_type`).

The calculation is based on the official standard from the Green Software Foundation:  
**SCI = ((E * I) + M) / R**

---

## 🛠️ 1. Installation and Setup

You do not need to set up complex infrastructure. The SDK runs locally and stores data in a local SQLite database.

1. Clone or copy the `green-sdk` repository into the root directory of your project.
2. Open your terminal in that directory and install the package locally by running:

```bash
pip install -e .

```

*(This will automatically install the required dependencies, such as `codecarbon` and `requests`, defined in `requirements.txt`).*

---

## 🚀 2. How to use the SDK in your code (Dual-Path)

Import `GreenLogger` in your main script and wrap the function or process you want to measure using a `with` block. Depending on the nature of your project, use one of the following templates:

### Option A: Standard Code Projects (Data Science, ETLs, APIs)

This activates **Path A**, using CodeCarbon to measure the actual physical CPU and RAM energy consumption on your local machine or container.

```python
from roche_green.logger import GreenLogger

# Define how many units you are processing (e.g., CSV rows, transactions)
records_processed = 5000

with GreenLogger(
    project_id="Your_Project_Name",    # E.g., "EcoFocus"
    step_name="Data_Processing",       # E.g., "Pandas_Cleaning"
    functional_units=records_processed,
    functional_unit_name="CSV rows",
    provider="AWS",                    # E.g., "AWS", "GCP", "Azure", "On-Premise"
    instance_type="m5.large"           # NEW v2.0: Triggers Boavizta for exact Hardware LCA
):
    # Place your actual code here
    process_massive_data()

```

### Option B: AI and LLM Projects (SCI for AI via CLEER)

AI inference energy consumption happens in the cloud. Because cloud providers do not expose physical GPU sensors, setting `is_ai=True` activates **Path B (The CLEER Engine)**. The SDK deterministically estimates the remote impact from token telemetry (see [CLEER default constants](#-cleer-default-constants-path-b)), while CodeCarbon still measures the local client process.

The `with` block should **wrap the AI API call**, so the measured local energy and the recorded `MEASUREMENT_PERIOD` correspond to the actual request. Token counts are only known after the response arrives, so set them on the logger object inside the block (they are read when the block exits):

```python
from roche_green.logger import GreenLogger

with GreenLogger(
    project_id="Your_AI_Project_Name",
    step_name="LLM_Inference",
    is_ai=True,                           # MANDATORY FOR AI (Activates CLEER)
    model_name="claude-3-5-sonnet",       # The foundational model you are using
    provider="AWS",                       # Not inferred from the model name: set it explicitly
    functional_units=1,                   # 1 transaction/call
    functional_unit_name="API request"
) as green:
    response = ai_client.generate_text(prompt="Summarize this document")
    green.prompt_tokens = response.usage.input_tokens
    green.cached_tokens = response.usage.cache_read_input_tokens   # GSF Prompt Caching
    green.completion_tokens = response.usage.output_tokens
```

If the API call necessarily happens **outside** the `with` block (e.g. you log after the fact), time it yourself and pass `duration_seconds` so `MEASUREMENT_PERIOD` reflects the real call instead of the near-zero duration of an empty block:

```python
import time
t0 = time.time()
response = ai_client.generate_text(prompt="Summarize this document")
elapsed = time.time() - t0

with GreenLogger(
    project_id="Your_AI_Project_Name", step_name="LLM_Inference",
    is_ai=True, model_name="claude-3-5-sonnet", provider="AWS",
    prompt_tokens=response.usage.input_tokens,
    cached_tokens=response.usage.cache_read_input_tokens,
    completion_tokens=response.usage.output_tokens,
    functional_units=1, functional_unit_name="API request",
    duration_seconds=elapsed,
):
    pass
```

### What gets recorded

* `SCI_SCORE_GCO2E_TX = (local carbon + CLEER operational carbon + M) / R`. `TOTAL_CARBON_FOOTPRINT_GCO2E` is operational carbon only; embodied carbon `M` is stored separately in `EMBODIED_EMISSIONS_GCO2E` and counted exactly once.
* `BASELINE_SCI_GCO2E` is the SCI as if every cached token had been processed fresh, and `CLEER_SCI_GCO2E` is the SCI with caching applied. Both include `M`. `OPTIMIZATION_YIELD_PCT` is the relative saving between them, so it is `0.0` when `cached_tokens == 0`.
* `ENERGY_CONSUMED_KWH` = CodeCarbon CPU + GPU + RAM energy, plus the CLEER remote energy for AI runs.
* `MEASUREMENT_METHOD` reports CodeCarbon's actual CPU tracking mode (`rapl`, `tdp_estimate`, `constant`, ...), plus `+ boavizta_m` or `+ CLEER_v2` where applicable.
* `SCI_TRACKER_ID` is unique per run (`<project_id>-<random 8 hex>`).
* **Failed runs:** a row is inserted as `[INCOMPLETE] <step>` when the block starts. If the block raises, the row is saved as `[FAILED] <step> ...` with `MEASUREMENT_METHOD = "failed_run"`, and the exception still propagates. Exclude these rows from comparisons.
* CodeCarbon is started with `save_to_file=False`, so no `emissions.csv` is written to your working directory. All data goes to the SQLite DB.

### 🧮 CLEER default constants (Path B)

| Constant | Default | Meaning |
|---|---|---|
| Input energy | `0.32` Wh / 1k tokens | Accelerator energy per 1,000 fresh input tokens |
| Output energy | `0.96` Wh / 1k tokens | Accelerator energy per 1,000 completion tokens |
| Cache multiplier | `0.15` | A cached input token costs 15% of a fresh input token |
| Dev amortization | `1.5` | Training / development energy amortized onto inference |
| Host overhead | `1.5` | Host CPU/RAM/network on top of accelerator energy |
| Idle factor | `1.1` | Idle / over-provisioned capacity |
| PUE | `1.45` | Datacenter Power Usage Effectiveness |
| Grid intensity (`I`) | `350` gCO2e/kWh | Carbon intensity applied to remote energy |
| Embodied fallback (`M`) | `97` gCO2e/kWh | `90` × (accelerator + dev energy) + `7` × facility energy |

These live as module-level constants at the top of `roche_green/logger.py`.

---

## ⏱️ 3. Time Normalization (`measurement_period`)

In GreenOps, it is crucial to differentiate between **Script Execution Time** (how long the Python code takes to run) and the **Measurement Period** (the actual timeframe the telemetry represents).

The `GreenLogger` handles both scenarios to ensure the SCI Dashboard can mathematically normalize metrics (Carbon and Energy) accurately:

* **Real-Time Tracking (Default):** If you are measuring a live process (e.g., an API call, a model inference, or a live data transformation), ignore this parameter. The SDK will automatically time your code execution (or use `duration_seconds` if you passed it).
* **Batch Processing & Historical Data:** If your script runs in 5 seconds, but it is parsing logs or processing a batch that represents a whole month of activity, you **must** specify the timeframe.
*Accepted formats:* `"X Minutes"`, `"X Hours"`, `"X Days"`, `"X Months"`, `"X Years"`.

**Example for Historical Data:**

```python
from roche_green.logger import GreenLogger

# Initialize logger for historical/batch data
with GreenLogger(
    project_id="myCO2", 
    step_name="DWH_Compute", 
    functional_unit_name="queries", 
    functional_units=5000, 
    measurement_period="1 Month"  # <--- CRITICAL FOR ACCURATE DASHBOARD SCALING
):
    # Your log parsing / batch processing code here
    parse_historical_logs()

```

---

## 📏 4. How to define your Functional Unit (`functional_units`)

To calculate the Software Carbon Intensity (SCI) accurately, the system divides your total carbon emissions by a Functional Unit (R). **It is critical that you choose the correct Functional Unit for your project and keep it consistent across all your measurements.**

Please use the following standard depending on the nature of your software:

1. **CI/CD, Builds, and Static Pipelines:**
* *Standard:* `functional_units = 1` | `functional_unit_name = "pipeline execution"` or `"full build"`
* *Why:* The goal is to measure the carbon cost of running the *entire* pipeline or deployment process once. Do not use the number of individual tests or compiled files as your unit.


2. **AI Models, Chatbots, and Transactional APIs:**
* *Standard:* `functional_units = [Number of requests]` | `functional_unit_name = "API request"` or `"user prompt"`
* *Why:* The impact of these systems scales with user traffic. We need to measure the carbon cost per individual interaction (e.g., if your test script sends 500 prompts, set this to 500).


3. **Massive Data Processing (ETLs, Big Data scripts):**
* *Standard:* `functional_units = [Volume]` | `functional_unit_name = "CSV rows"`, `"DB records"`, or `"MBs processed"`
* *Why:* Processing 10 rows is not the same as 10 million. Use the number of database records, rows, or Megabytes analyzed during the execution to measure the efficiency of your algorithm.



---

## 📖 5. `GreenLogger` Parameter Dictionary

To ensure your data is accurately reflected in the global dashboard, please fill in these parameters correctly:

* **`project_id` (String, Mandatory):** The official name of your project. Always use the exact same name across all your scripts so the metrics are grouped together.
* **`step_name` (String, Mandatory):** A brief description of the task being measured.
* **`functional_units` (Integer, Mandatory):** The $R$ value in the SCI equation. It represents the unit of work (e.g., 1 user, 1000 records).
* **`functional_unit_name` (String, Mandatory):** The name/description of your $R$ value (e.g., "API request"). This defines what the SCI score represents.
* **`is_ai` (Boolean, Optional):** Set to `True` ONLY if you are measuring the impact of a remote AI model (Triggers Path B / CLEER).
* **`measurement_period` (String, Optional):** The explicit timeframe your data represents (e.g., "1 Month"). Use this ONLY if you are processing historical data or batches. If left blank, the SDK will automatically record the real-time script execution duration.
* **`provider` (String, Optional):** The infrastructure where your code executes (e.g., `"AWS"`, `"GCP"`, `"Azure"`, `"On-Premise"`). Defaults to `"Unknown"`; it is **not** inferred from the model name. It is stored with the row and, for standard code, decides whether Boavizta can be queried (case-insensitive `AWS`, `GCP` or `Azure` only).
* **`instance_type` (String, Optional):** (v2.0) The cloud instance your **standard (non-AI)** code runs on (e.g., `"m5.large"`). With `provider` set to AWS/GCP/Azure, this queries the Boavizta API for the instance's embodied (manufacturing) carbon (`impacts.gwp.embedded`). Boavizta already gives the instance's share over a 6-year lifespan, and the SDK prorates it to the run's duration. If the lookup fails or doesn't apply, the SDK uses `embodied_carbon_rate`. It is ignored for `is_ai=True`: the local runner's instance says nothing about the remote GPU servers, so CLEER's embodied fallback is used.
* **`country_iso_code` (String, Optional)**: The 3-letter ISO country code where the code is running (e.g., `"ESP"`, `"IND"`). If left blank, the SDK attempts to geolocate via IP. Security Note: If your environment has strict firewalls, you must pass this parameter to force the SDK into fully offline mode.
* **`embodied_carbon_rate` (Float, Optional):** The allocated embodied carbon rate of your hardware, in gCO2e **per second**. Defaults to `0.0`. Only modify this manually if you have precise hardware LCA data and are not using `instance_type` (Path A only).
* **`duration_seconds` (Float, Optional):** (v2.0.2) Overrides the measured wall-clock time used for `MEASUREMENT_PERIOD` and embodied-carbon proration. Use it when the work being logged (typically an AI API call) happened outside the `with` block.
* **`db_path` (String, Optional):** Path and filename for the output database (Default: `"green_telemetry.db"`).

**Exclusive parameters if `is_ai=True`:**

* **`model_name` (String, Mandatory):** The name of the AI model being used (e.g., `"gpt-4o-mini"`).
* **`prompt_tokens` (Integer):** The net (non-cached) input tokens processed by the model.
* **`cached_tokens` (Integer):** (v2.0) Input tokens served directly from the provider's memory. *Critical for demonstrating FinOps and Carbon savings through Semantic/Prompt Caching.*
* **`completion_tokens` (Integer):** The number of tokens generated by the model.

---

## 🧭 6. The Optimization Assistant (CLI)

Before optimizing your code, you need to know *what* to optimize. The SDK includes a built-in CLI tool powered by the **Green Software Foundation (GSF)** pattern catalog. It provides tailored architectural recommendations based on your role (e.g., Data Engineer, AI/ML Engineer) and whether your project involves AI.

Once you have installed the SDK (`pip install -e .`), you can run the assistant from any terminal within your project directory:

```bash
roche-green suggest
```

---
## 📊 7. Evaluating Optimizations (Baseline vs. Optimized)

The ultimate goal of Green Software is not to compete against other projects, but to establish a baseline and prove internal efficiency gains. To do this, you must provide a **Baseline (PRE)** and an **Optimized (POST)** database.

When optimizing code between the PRE and POST phases, developers must evaluate and implement the official patterns from the Green Software Foundation (GSF). The complete catalog categorizes green software patterns based on user personas (e.g., Architect, Software Engineer) and project domains. You can explore the full, searchable catalog to find the most relevant optimizations for your specific workload here: https://patterns.greensoftware.foundation/

**🚨 THE GOLDEN RULE: Apples to Apples**
To allow the Dashboard to calculate exact savings, the telemetry structure must be completely symmetrical:

1. **Exact same number of steps:** If your baseline measurement used 3 `GreenLogger` wrappers (resulting in 3 rows in the DB), your optimized measurement MUST also use 3 wrappers wrapping the exact same logic blocks.
2. **Exact same names:** The `step_name` parameter in your Python code must remain **100% identical** between the baseline and the optimized code (e.g., if it was `"Client_Vite_Build"`, keep it exactly like that). Do not manually add "-post" or "-optimized" in your code.
3. **File naming convention:**
* Send your baseline file named normally (e.g., `yourproject_telemetry_pre.db`).
* Send your optimized file with the word **"post"** or **"opt"** in the filename (e.g., `yourproject_telemetry_post.db`). Our central parser will automatically detect this and tag your steps as `(POST-OPTIMIZATION)`.



*(Note: Failure to maintain the exact same `step_name` and row count will result in mismatched data, and the dashboard will reject the granular comparison).*

---

## 🏢 8. My Green IT Lab (Roche sHPC Infrastructure)

For internal developers running measurements on the local infrastructure (Roche sHPC), follow this workflow for provisioning and access. This ensures `Path A` metrics of the highest precision (no sensor blocking by virtualization).

**Identity and Access Management (CIDM)**
To use sHPC services, a valid Unix ID is mandatory. The detailed official process can be found on the Roche Wiki.

* In the CIDM portal, select "Request Access" and request the "sHPC Onboarding Foundation Role". This will create the Red Hat IdM account with your UNIX ID and add you to the `RSI-A-EC-HubSSOAccess` access control group.
* To enable SSH access over VPN (required for remote IDEs like VSCode), additionally request membership in the global group `GLOZeroTrust-AppControl_SSH`. (Requires manager approval).

**Development Environment Deployment (Jupyter)**

* Access the web portal at `https://scoop.science.roche.com/` (you should be connected to the VPN).
* Navigate to "Interactive Apps" and select "Jupyter Notebook".
* Configure your Slurm node:
* **App type:** Choose whether to provide a container or load a Linux module.
* **Right-sizing:** Strictly define the required "Number of cores" and "Amount of Memory (in GB)" to avoid provisioning idle capacity.
* **GPU:** Check the "request GPU" box only if your AI workload requires local execution on the nodes.


* Select "Use JupyterLab instead of Jupyter Notebook" to access the modern interface. From there, you can clone this SDK and run your measurements.

*(Once your executions are complete, share the generated `.db` files with the Digital Sustainability Team for integration into the global Dashboard).*

### ⚠️ Shared HPC (sHPC) Nodes

Path A uses CodeCarbon, which measures the **whole node**, not your process. On shared sHPC nodes this has two consequences:

* **Other users' jobs are included.** RAPL counters and machine-level CPU/RAM figures cover every job scheduled on the node, so your row also contains your neighbours' energy and isn't reproducible between runs.
* **Newer Xeon nodes don't expose RAPL** to users, so CodeCarbon falls back to a TDP/constant estimate for the full CPU (`MEASUREMENT_METHOD` shows `tdp_estimate` or `constant` instead of `rapl`). That also overstates a job using only a few cores.

For shared clusters, use **job-level estimation** from Slurm accounting rather than full-node measurement:

```
E_cpu (kWh) = job_cpu_time (h) × (cpu_tdp (W) / total_cores) / 1000
E_job (kWh) = E_cpu + ram_energy
```

where `job_cpu_time` is the core-hours consumed by your job (e.g. `sacct -j <jobid> --format=TotalCPU`), `cpu_tdp / total_cores` is the per-core power of the node's CPU, and `ram_energy` is the requested memory × time × a per-GB power factor (CodeCarbon uses 0.375 W/GB). Full-node CodeCarbon numbers are only representative when you have the **entire node exclusively** (e.g. `--exclusive`).
