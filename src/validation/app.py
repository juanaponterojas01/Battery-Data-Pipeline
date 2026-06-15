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

REQUIRED_COLUMNS = {"Voltage", "Current", "Battery_Temp_degC", "Time", "TimeStamp"}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Validate a raw battery test CSV from S3 and write the cleaned version back.

    Performs the following steps in order:

    1. **Read** the raw CSV from S3 (key must start with ``raw/``).
    2. **Structural checks** — reject empty files or files that lack the
       required columns defined in :obj:`REQUIRED_COLUMNS`.
    3. **Transform** — rename ``Battery_Temp_degC`` to ``Temperature`` and
       invert the sign of ``Current`` so that discharge is positive.
    4. **Sanity checks** — voltage bounds (2.5–4.2 V), excessive current
       spikes (>±5 A), temperature >45 °C.
    5. **Timestep consistency** — verifies that the sampling interval stays
       within a 0.01 s tolerance of the median dt.
    6. **Write** the validated (cleaned) CSV under the ``validated/`` prefix
       in the same S3 bucket.

    Args:
        event: A dict with the following keys provided by the upstream Step
            Function step:

            - ``bucket`` (:class:`str`): S3 bucket containing the raw CSV.
            - ``key`` (:class:`str`): S3 object key for the raw CSV. Must
              start with ``raw/``.
            - ``cell_id`` (:class:`str`): Panasonic 18650PF cell identifier,
              e.g. ``"Cell_1"``.
            - ``test_date`` (:class:`str`): Date the test was conducted,
              e.g. ``"2024-01-15"``.
            - ``drive_cycle`` (:class:`str`): Drive-cycle label for the
              test, e.g. ``"US06"``, ``"LA92"``.

        context: The :class:`LambdaContext` object supplied by the AWS Lambda
            runtime. Not used in this handler.

    Returns:
        A dict with the following keys, intended for the downstream Step
        Function state:

        - ``bucket`` (:class:`str`): The original bucket name.
        - ``key`` (:class:`str`): The S3 key of the **validated** CSV
          (under the ``validated/`` prefix).
        - ``validation_log`` (:class:`list`\[:class:`str`]): A list of
          human-readable warnings or ``["All checks passed"]`` if no issues
          were found.
        - ``cell_id`` (:class:`str`): Passed through from *event*.
        - ``test_date`` (:class:`str`): Passed through from *event*.
        - ``drive_cycle`` (:class:`str`): Passed through from *event*.

    Raises:
        ValueError: If the CSV file is empty, if any of the required columns
            are missing, or if the S3 key does not start with the expected
            ``raw/`` prefix.
        ClientError (boto3): Propagated automatically when S3 operations
            (``get_object`` / ``put_object``) fail due to networking issues,
            missing bucket, permission errors, etc.
    """
    bucket: str = event["bucket"]
    raw_key: str = event["key"]
    cell_id: str = event["cell_id"]
    test_date: str = event["test_date"]
    drive_cycle: str = event["drive_cycle"]

    validation_log: list[str] = []
    s3 = boto3.client("s3")

    # ------------------------------------------------------------------
    # 1. Read raw CSV from S3 into a pandas DataFrame
    # ------------------------------------------------------------------
    logger.info("Reading raw CSV from s3://%s/%s", bucket, raw_key)
    obj = s3.get_object(Bucket=bucket, Key=raw_key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    if df.empty:
        raise ValueError("CSV file is empty — no data rows to validate.")

    # ------------------------------------------------------------------
    # 2. Check that every required column is present
    # ------------------------------------------------------------------
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    # ------------------------------------------------------------------
    # 3. Warn (non-fatal) if the Ah column is absent — the downstream
    #    SoC lambda will impute or calculate it incrementally.
    # ------------------------------------------------------------------
    if "Ah" not in df.columns:
        validation_log.append(
            "Warning: 'Ah' column missing — downstream SOC lambda "
            "will handle this."
        )

    # ------------------------------------------------------------------
    # 4. Transform columns
    #    - Rename the verbose column header to a shorter canonical name.
    #    - Invert the sign of Current so that discharge is positive
    #      (raw data typically records discharge as negative).
    # ------------------------------------------------------------------
    df = df.rename(columns={"Battery_Temp_degC": "Temperature"})
    df["Current"] = df["Current"] * -1

    # ------------------------------------------------------------------
    # 5. Run validation checks
    # ------------------------------------------------------------------

    # 5a. Voltage bounds: Li-ion operating window is 2.5 V – 4.2 V.
    #     Any reading outside this range is flagged.
    voltage_mask = (df["Voltage"] < 2.5) | (df["Voltage"] > 4.2)
    if voltage_mask.any():
        indices = df.index[voltage_mask].tolist()
        validation_log.append(f"Voltage out of bounds at indices: {indices}")

    # 5b. Current spikes: instantaneous current >|5| A is unusual for
    #     this cell under typical drive cycles and may indicate sensor
    #     noise or a transient fault.  Record the corresponding timestamps.
    spike_mask = df["Current"].abs() > 5.0
    if spike_mask.any():
        spike_times = df.loc[spike_mask, "Time"].tolist()
        validation_log.append(f"Current spikes > 5A at times: {spike_times}")

    # 5c. Temperature ceiling: sustained operation above 45 °C may
    #     indicate thermal runaway or sensor malfunction.
    temp_mask = df["Temperature"] > 45.0
    if temp_mask.any():
        indices = df.index[temp_mask].tolist()
        validation_log.append(f"Temperature exceeded 45°C at indices: {indices}")

    # 5d. Timestep consistency check.
    #     Rationale: battery testers sample at a fixed rate (e.g. 1 Hz).
    #     Compute the difference between successive Time stamps, drop the
    #     leading NaN, then compare every interval against the median
    #     interval.  If any interval deviates by more than 0.01 s from
    #     the median, the data may contain gaps, duplicate rows, or a
    #     misconfigured logger.
    time_diffs = df["Time"].diff().dropna()

    # Guard against degenerate inputs: a single-row DataFrame or a column
    #     full of NaN after diff() would make the comparison meaningless.
    if len(time_diffs) < 1 or time_diffs.isna().all():
        validation_log.append("Timestep check skipped (insufficient data)")
    else:
        median_dt = time_diffs.median()
        # Use np.isclose with an absolute tolerance of 0.01 s (10 ms).
        inconsistent = ~np.isclose(time_diffs, median_dt, atol=0.01)
        if inconsistent.any():
            validation_log.append("Inconsistent timesteps detected")

    # ------------------------------------------------------------------
    # 6. Write the validated (cleaned) DataFrame back to S3 under the
    #    ``validated/`` prefix.
    # ------------------------------------------------------------------
    if not raw_key.startswith("raw/"):
        raise ValueError(f"S3 key must start with 'raw/', got: {raw_key}")
    validated_key = raw_key.replace("raw/", "validated/", 1)
    logger.info("Writing validated CSV to s3://%s/%s", bucket, validated_key)

    csv_buffer = io.BytesIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)

    s3.put_object(Bucket=bucket, Key=validated_key, Body=csv_buffer.getvalue())

    # If no warnings were raised during validation, provide a clean bill
    # of health so downstream states can easily check for success.
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
