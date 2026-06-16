"""AWS Lambda function for validating Panasonic 18650PF battery test CSVs from S3.

This module is the first step in a multi-stage battery-data pipeline. It is
triggered by an AWS Step Function after a raw CSV lands in the ``raw/`` prefix
of the configured S3 bucket.  Validation covers:

* Structural checks (empty file, missing required columns)
* Sanity bounds (voltage window, current spikes, temperature ceiling)
* Temporal consistency (uniform sampling intervals)
* Column renorming (``Battery_Temp_degC`` → ``Temperature``, current sign
  inversion so that discharge is positive)

A validated copy is written under the ``validated/`` prefix for downstream
consumers (e.g. the SoC estimation lambda).
"""

import io
import logging
from typing import Any

import boto3
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REQUIRED_COLUMNS: set[str] = {"Voltage", "Current", "Battery_Temp_degC", "Time", "TimeStamp"}


def _run_validation_checks(df: pd.DataFrame) -> list[str]:
    """Run all validation checks on the transformed DataFrame.

    Checks performed:
    1. Voltage bounds (2.5 V – 4.2 V).
    2. Current spikes (>|5| A).
    3. Temperature ceiling (> 45 °C).
    4. Timestep consistency (deviation > 0.01 s from median).

    Args:
        df: Transformed DataFrame with ``Voltage``, ``Current``,
            ``Temperature``, and ``Time`` columns.

    Returns:
        List of human-readable warning messages. Empty if all checks pass.
    """
    log: list[str] = []

    # 1. Voltage bounds: Li-ion operating window is 2.5 V – 4.2 V.
    voltage_mask = (df["Voltage"] < 2.5) | (df["Voltage"] > 4.2)
    if voltage_mask.any():
        indices = df.index[voltage_mask].tolist()
        log.append(f"Voltage out of bounds at indices: {indices}")

    # 2. Current spikes: instantaneous current >|5| A is unusual for
    #    this cell under typical drive cycles.
    spike_mask = df["Current"].abs() > 5.0
    if spike_mask.any():
        spike_times = df.loc[spike_mask, "Time"].tolist()
        log.append(f"Current spikes > 5A at times: {spike_times}")

    # 3. Temperature ceiling: sustained operation above 45 °C may
    #    indicate thermal runaway or sensor malfunction.
    temp_mask = df["Temperature"] > 45.0
    if temp_mask.any():
        indices = df.index[temp_mask].tolist()
        log.append(f"Temperature exceeded 45°C at indices: {indices}")

    # 4. Timestep consistency check.
    time_diffs = df["Time"].diff().dropna()
    if len(time_diffs) < 1 or time_diffs.isna().all():
        log.append("Timestep check skipped (insufficient data)")
    else:
        median_dt = time_diffs.median()
        inconsistent = ~np.isclose(time_diffs, median_dt, atol=0.01)
        if inconsistent.any():
            log.append("Inconsistent timesteps detected")

    return log


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Validate a raw battery test CSV from S3 and write the cleaned version back.

    Performs the following steps in order:

    1. **Validate S3 key** — must start with ``raw/``.
    2. **Read** the raw CSV from S3.
    3. **Structural checks** — reject empty files or files that lack the
       required columns defined in :obj:`REQUIRED_COLUMNS`.
    4. **Transform** — rename ``Battery_Temp_degC`` to ``Temperature`` and
       invert the sign of ``Current`` so that discharge is positive.
    5. **Sanity checks** — voltage bounds, current spikes, temperature,
       timestep consistency.
    6. **Write** the validated CSV under the ``validated/`` prefix.

    Args:
        event: A dict with the following keys provided by the upstream Step
            Function step:

            - ``bucket`` (:class:`str`): S3 bucket containing the raw CSV.
            - ``key`` (:class:`str`): S3 object key for the raw CSV. Must
              start with ``raw/``.
            - ``cell_id`` (:class:`str`): Panasonic 18650PF cell identifier.
            - ``test_date`` (:class:`str`): Date the test was conducted.
            - ``drive_cycle`` (:class:`str`): Drive-cycle label.

        context: The :class:`LambdaContext` object supplied by the AWS Lambda
            runtime. Not used in this handler.

    Returns:
        A dict with ``bucket``, ``key`` (validated), ``validation_log``,
        ``cell_id``, ``test_date``, and ``drive_cycle``.

    Raises:
        ValueError: If the S3 key does not start with ``raw/``, if the CSV is
            empty, or if required columns are missing.
        ClientError (boto3): Propagated automatically when S3 operations fail.
    """
    bucket: str = event["bucket"]
    raw_key: str = event["key"]
    cell_id: str = event["cell_id"]
    test_date: str = event["test_date"]
    drive_cycle: str = event["drive_cycle"]

    # Guard clause: S3 key must start with raw/
    if not raw_key.startswith("raw/"):
        raise ValueError(f"S3 key must start with 'raw/', got: {raw_key}")

    s3 = boto3.client("s3")

    # 1. Read raw CSV from S3 into a pandas DataFrame
    logger.info("Reading raw CSV from s3://%s/%s", bucket, raw_key)
    obj = s3.get_object(Bucket=bucket, Key=raw_key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    if df.empty:
        raise ValueError("CSV file is empty — no data rows to validate.")

    # 2. Check that every required column is present
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    # 3. Warn (non-fatal) if the Ah column is absent
    validation_log: list[str] = []
    if "Ah" not in df.columns:
        validation_log.append(
            "Warning: 'Ah' column missing — downstream SOC lambda will handle this."
        )

    # 4. Transform columns
    df = df.rename(columns={"Battery_Temp_degC": "Temperature"})
    df["Current"] = df["Current"] * -1

    # 5. Run validation checks
    validation_log.extend(_run_validation_checks(df))

    # 6. Write the validated DataFrame back to S3
    validated_key = raw_key.replace("raw/", "validated/", 1)
    logger.info("Writing validated CSV to s3://%s/%s", bucket, validated_key)

    csv_buffer = io.BytesIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    s3.put_object(Bucket=bucket, Key=validated_key, Body=csv_buffer.getvalue())

    # If no warnings were raised, provide a clean bill of health
    if not validation_log:
        validation_log = ["All checks passed"]

    return {
        "bucket": bucket,
        "key": validated_key,
        "validation_log": validation_log,
        "cell_id": cell_id,
        "test_date": test_date,
        "drive_cycle": drive_cycle,
    }
