---
name: generate-report
description: |
  Generate a professional 2-page A4 PDF performance report for a processed
  battery drive-cycle dataset stored in S3, or list the available datasets.
---

# Generate Battery Test Report

This skill provides two operations for battery test data stored in the
processed S3 bucket:

1. **List** available datasets.
2. **Generate** a professional 2-page A4 PDF report for a chosen dataset.

## When to Use

- When the user asks about available battery test data or processed datasets.
- When the user wants a report, PDF, or visualization of a battery drive-cycle test.
- When the user asks for metrics, performance analysis, or plots for a specific test.

## Commands

### `list` — List Available Datasets

Query the processed S3 bucket and display all dataset names found under the
`results/` prefix.

**Invocation patterns:**
- "What datasets are available?"
- "List processed battery data"
- "Show me available battery tests"

**CLI:**
```bash
python .opencode/skills/generate-report/generate_report.py list
```

### `generate` — Generate a 2-Page PDF Report

Download the processed CSV, metadata JSON, and raw CSV for a dataset, then
produce a professional A4 PDF report.

**Report contents:**

**Page 1:**
- Report title with drive cycle and test date
- Test Summary block (Cell Model, Drive Cycle, Test Date, Duration)
- Executive Metrics table with status indicators:
  - Capacity Discharged (Ah)
  - Energy Delivered (Wh)
  - Peak Discharge Current (A)
  - Voltage Sag (V)
  - Peak Temperature (°C)
  - Delta Temperature (°C)
- Plot 1: Applied Load Profile (Current vs Time)
- Plot 2: Electrical Response (Voltage vs Time)

**Page 2:**
- Plot 3: State of Charge Depletion (SOC vs Time)
- Plot 4: Thermal Response (Temperature vs Time)
- Plot 5: Characteristic Curve (Voltage vs SOC)
- Data Processing Notes (validation log, filter method, SOC method)

**Thresholds used for status indicators:**
- **Peak Temperature** — Safe if < 45 °C, Review if >= 45 °C
- **Voltage Sag** — Safe if < 0.5 V, Review if >= 0.5 V

**Invocation patterns:**
- "Generate a report for US06_25degC_2017-03-20"
- "Create a PDF for dataset X"
- "Plot the results of test Y"

**CLI:**
```bash
python .opencode/skills/generate-report/generate_report.py generate <dataset_name>
python .opencode/skills/generate-report/generate_report.py generate <dataset_name> --force
```

**Arguments:**
- `dataset_name` — Name of the dataset (e.g., `US06_25degC_2017-03-20`)
- `--force` — Re-download files from S3 even if cached locally

**Output:**
- `data/processed_data/<dataset_name>/report.pdf`

## Example Interaction

**User:** "Generate a report for the US06 test"

**Agent steps:**
1. Run the `list` command internally to verify the dataset exists.
2. Run the `generate` command for the dataset.
3. Return the absolute path to the generated PDF.

## Implementation Details

- **Page size:** A4 (8.27" × 11.69"), 300 DPI
- **Fonts:** 11 pt body, 13 pt titles
- **Palette:** tab10 with 1.2 px line width
- **Caching:** Files are cached in `data/processed_data/<dataset_name>/`. Use `--force` to override.
- **Data sources:**
  - Processed CSV and metadata JSON from the processed S3 bucket
  - Raw CSV from the raw S3 bucket (used for exact peak current, voltage sag, and temperature rise)

## Dependencies

All dependencies are listed in the root `requirements.txt`:
- `boto3`
- `pandas`
- `numpy`
- `matplotlib`
