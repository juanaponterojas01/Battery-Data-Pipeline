# Revised Implementation Plan: EV Battery Data Pipeline (Panasonic 18650PF)

## Executive Summary
This plan builds an AWS SAM serverless pipeline that processes Panasonic 18650PF drive-cycle CSVs one test at a time. The pipeline is triggered by uploading a CSV and a sidecar `.meta.json` file to a raw S3 bucket. An Orchestrator Lambda parses the S3 event, reads the metadata, and starts a Step Functions execution. The execution chains 4 Lambdas: Validation → Processing → SOC Estimation → Metadata/Report. The final outputs are a processed CSV and a metadata JSON saved to a processed S3 bucket.
A local runnable simulation is also provided to test the entire logic on a local machine before deployment.

## Data Schema & Assumptions
The actual CSVs use these column names. The Lambdas will map them internally:
- `Voltage` → `Voltage` (Range ~2.5V–4.2V)
- `Current` → `Current` (Negative for discharge. Sign will be inverted in Validation Lambda.)
- `Battery_Temp_degC` → `Temperature` (Needs to be renamed on read)
- `Time` → `Time` (Elapsed time in seconds)
- `Ah` → `Ah` (Cumulative Ampere-hours. Used to infer the initial SOC)
- `TimeStamp` → `TimeStamp` (Human-readable timestamp, not used for math)
- `Chamber_Temp_degC` → `Chamber_Temp_degC` (Not used for validation)

Key Assumptions:
- Only drive cycle tests will be uploaded to the pipeline.
- Timestep tolerance: Data shows ~0.1s steps but with slight jitter. The validation will use `np.isclose(time_diffs, expected_dt, atol=0.01)` instead of exact equality.
- Lambda Scaling: Drive cycle files are 100k–200k rows. Lambdas must be configured with 2048MB+ memory and 10-minute timeout.

## Project Structure
```
ev-battery-pipeline/
├── .github/
│   └── workflows/
│       └── deploy.yaml              # CI/CD
├── src/
│   ├── orchestrator/
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── validation/
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── processing/
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── soc_estimation/
│   │   ├── app.py
│   │   └── requirements.txt
│   └── metadata/
│       ├── app.py
│       └── requirements.txt
├── scripts/
│   └── local_simulator.py
├── statemachine/
│   └── pipeline.asl.json
├── template.yaml
├── samconfig.toml
└── README.md
```

## Step 1: Metadata Sidecar Format (`*.meta.json`)
Before uploading a CSV, the user must also upload a JSON file with the same base name (e.g., `test.csv` + `test.meta.json`).
Example `test.meta.json`:
```json
{
  "cell_id": "PF_Cell_01",
  "test_date": "2023-10-27",
  "drive_cycle": "UDDS_10degC",
  "temperature": 10
}
```
The Orchestrator Lambda will read this sidecar file to inject the required metadata into the Step Functions payload.

## Step 2: Orchestrator Lambda (`src/orchestrator/app.py`)
Trigger: S3 PUT event (via EventBridge) on `BatteryRawBucket`.
Logic:
1. Receives the S3 event (bucket, key).
2. Checks if the key is a `.csv` file inside the `raw/` prefix.
3. Constructs the corresponding `.meta.json` key.
4. Reads the JSON metadata from S3.
5. Starts the Step Functions execution using boto3.
6. Passes the payload: `bucket`, `key`, `cell_id`, `test_date`, `drive_cycle`.

## Step 3: Validation Lambda (`src/validation/app.py`)
Input: Event from Orchestrator (bucket, key, metadata).
Logic:
1. Read CSV from S3 into Pandas.
2. Rename `Battery_Temp_degC` → `Temperature`.
3. Invert Current sign: `df['Current'] = df['Current'] * -1` (discharge becomes positive).
4. Validation Checks:
   - Voltage bounds: `2.5V ≤ Voltage ≤ 4.2V`. Log out-of-bound indices.
   - Current spikes: `abs(Current) > 5A`. Log spike times.
   - Temperature bounds: `Temperature > 45°C`. Log out-of-bound indices.
   - Timestep consistency: Calculate `df['Time'].diff()`. Check if all diffs are within `±0.01` seconds of the median timestep. Log if inconsistent.
5. Save validated CSV to `validated/` prefix in the same bucket.
6. Return S3 location and metadata + validation log.

## Step 4: Processing Lambda (`src/processing/app.py`)
Input: Output from Validation Lambda.
Logic:
1. Read validated CSV.
2. Apply Savitzky-Golay filter (window=11, polyorder=3) to `Voltage` and `Current`.
   - Guard clause: if `len(df) < 11`, skip filtering or use smaller window.
3. Min-Max Normalization:
   - `V_norm = (Voltage_Filtered - 2.5) / (4.2 - 2.5)`
   - `I_norm = (Current_Filtered - (-25)) / (25 - (-25))`
   - `T_norm = (Temperature - 15) / (45 - 15)`
4. Save processed CSV to `processed/` prefix.
5. Return S3 location and metadata.

## Step 5: SOC Estimation Lambda (`src/soc_estimation/app.py`)
Input: Output from Processing Lambda.
Logic:
1. Read processed CSV.
2. Infer Initial SOC:
   - `initial_ah = df['Ah'].iloc[0]`.
   - `initial_soc = 1.0 - (initial_ah / 2.9)`.
   - Clip `initial_soc` to `[0, 1]`.
3. Coulomb Counting:
   - `dt_hours = df['Time'].diff().fillna(0) / 3600.0`
   - `delta_ah = df['Current_Filtered'] * dt_hours`
   - `df['SOC'] = initial_soc - (delta_ah.cumsum() / 2.9)`
   - Clip `df['SOC']` to `[0, 1]`.
4. Save SOC CSV to `soc/` prefix.
5. Return S3 location, metadata, and `duration_secs`.

## Step 6: Metadata & Report Lambda (`src/metadata/app.py`)
Input: Output from SOC Estimation Lambda.
Logic:
1. Read final SOC CSV.
2. Calculate Metrics:
   - `avg_soc = mean(df['SOC'])`
   - `avg_voltage = mean(df['Voltage_Filtered'])`
   - `peak_temp = max(df['Temperature'])`
   - `capacity_discharged = (initial_soc - df['SOC'].iloc[-1]) * 2.9`
3. Construct JSON Metadata matching the exact structure from the original plan.
4. Write outputs to `BatteryProcessedBucket`:
   - `results/<drive_cycle>_<test_date>.csv`
   - `metadata/<drive_cycle>_<test_date>.json`
5. Return success status and S3 paths.

## Step 7: Step Functions ASL (`statemachine/pipeline.asl.json`)
Chains ValidateData → ProcessData → EstimateSOC → WriteMetadata.
Uses `ResultPath: "$"` to pass output between steps.

## Step 8: AWS SAM Template (`template.yaml`)
Key Resources:
1. `BatteryRawBucket`: S3 bucket for incoming CSVs and `.meta.json` files.
2. `BatteryProcessedBucket`: S3 bucket for final CSV and JSON outputs.
3. `OrchestratorFunction`: Lambda triggered by S3 EventBridge events.
4. `ValidateFunction`, `ProcessFunction`, `SOCFunction`, `MetadataFunction`: Step Functions Lambdas.
5. `PipelineStateMachine`: Step Functions state machine.
6. `EventBridgeRule`: Rule that triggers Orchestrator Lambda on S3 PUT.
Lambda Configuration:
- Memory: 2048 MB (minimum for 100k+ row Pandas operations).
- Timeout: 600 seconds (10 minutes).
- IAM Role: Read/Write access to both S3 buckets. StartExecution access to State Machine.
- Dependencies: pandas, scipy. Use `sam build --use-container`.

## Step 9: CI/CD (`.github/workflows/deploy.yaml`)
Trigger: Push to `main`.
Steps: Checkout, Install SAM CLI, Configure AWS credentials, `sam build --use-container`, `sam deploy --no-confirm-changeset --no-fail-on-empty-changeset`.

## Step 10: Local Runnable Simulation (`scripts/local_simulator.py`)
Goal: Run the entire pipeline locally on a single CSV file without AWS.
Implementation: Python script that mimics the Step Functions flow. Reads local CSV and `.meta.json`, sequentially calls the logic of the 4 Lambdas, saves intermediate and final outputs to a local `output/` directory.

## Validation Rules Summary
| Check | Rule | Action on Failure |
|---|---|---|
| Voltage | `2.5V ≤ V ≤ 4.2V` | Log indices in `validation_log` |
| Current Spike | `abs(I) > 5A` | Log times in `validation_log` |
| Temperature | `T > 45°C` | Log indices in `validation_log` |
| Timestep | Median diff ± 0.01s | Log "Inconsistent timesteps" |
| Current Sign | Inverted in Validation | Discharge = Positive |

## Deployment & Testing Checklist
1. Local Testing: Run `local_simulator.py` on a 10degC UDDS file. Verify SOC inference, sign inversion, filtering, normalization.
2. AWS Deployment: Create template.yaml, set GitHub Secrets, push to main.
3. Edge Cases: CSVs < 11 rows, missing `.meta.json`, missing `Ah` column.
