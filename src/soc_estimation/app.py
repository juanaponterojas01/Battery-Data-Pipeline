"""AWS Lambda function for State-of-Charge (SOC) estimation of Panasonic 18650PF cells.

This module is the third step in the battery-data pipeline, triggered by an
AWS Step Function after the Processing Lambda completes.  It reads the
processed CSV from the ``processed/`` prefix, infers the initial SOC from
the cumulative Ampere-hour reading, performs Coulomb counting to estimate
SOC at every sample, and writes the enriched DataFrame back to S3 under
the ``soc/`` prefix.
"""

import io
import logging
from typing import Any

import boto3
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

Q_CAPACITY_AH = 2.9
"""Nominal capacity of a Panasonic 18650PF cell in Ampere-hours."""


def _estimate_soc(df: pd.DataFrame) -> tuple[pd.Series, float]:
    """Estimate SOC via Coulomb counting and compute test duration.

    The initial SOC is inferred from the first-row ``Ah`` value:
    ``SOC_0 = 1 - Ah_0 / Q_CAPACITY_AH``, clipped to [0, 1].
    If the ``Ah`` column is missing or NaN, defaults to 1.0.

    Args:
        df: Processed DataFrame with ``Time``, ``Current_Filtered``,
            and optionally ``Ah`` columns.

    Returns:
        A tuple of (SOC Series clipped to [0, 1], duration in seconds).
    """
    if "Ah" not in df.columns or pd.isna(df["Ah"].iloc[0]):
        logger.warning("Ah column missing — defaulting initial SOC to 1.0")
        initial_soc = 1.0
    else:
        initial_ah = df["Ah"].iloc[0]
        initial_soc = float(np.clip(1.0 - (initial_ah / Q_CAPACITY_AH), 0.0, 1.0))

    logger.info("Initial SOC: %.4f", initial_soc)

    dt_hours = df["Time"].diff().fillna(0) / 3600.0
    delta_ah = df["Current_Filtered"] * dt_hours
    soc = initial_soc - (delta_ah.cumsum() / Q_CAPACITY_AH)
    soc = soc.clip(0.0, 1.0)

    duration_secs = float(df["Time"].iloc[-1] - df["Time"].iloc[0])

    return soc, duration_secs


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Estimate SOC for a processed battery test CSV from S3.

    Performs the following steps in order:

    1. **Validate S3 key** — must start with ``processed/``.
    2. **Read** the processed CSV from S3.
    3. **Infer initial SOC** from the first-row ``Ah`` value.
    4. **Coulomb counting** — integrates current over time.
    5. **Write** the SOC-enriched CSV under the ``soc/`` prefix.

    Args:
        event: A dict with the following keys provided by the upstream Step
            Function step:

            - ``bucket`` (:class:`str`): S3 bucket containing the processed CSV.
            - ``key`` (:class:`str`): S3 object key for the processed CSV.
              Must start with ``processed/``.
            - ``validation_log`` (:class:`list`\[:class:`str`]): Log from the
              Validation Lambda.
            - ``cell_id`` (:class:`str`): Panasonic 18650PF cell identifier.
            - ``test_date`` (:class:`str`): Date the test was conducted.
            - ``drive_cycle`` (:class:`str`): Drive-cycle label.

        context: The :class:`LambdaContext` object supplied by the AWS Lambda
            runtime. Not used in this handler.

    Returns:
        A dict with ``bucket``, ``key`` (soc), ``validation_log``,
        ``cell_id``, ``test_date``, ``drive_cycle``, and ``duration_secs``.

    Raises:
        ValueError: If the S3 key does not start with ``processed/``.
        ClientError (boto3): Propagated automatically when S3 operations fail.
    """
    bucket: str = event["bucket"]
    processed_key: str = event["key"]
    validation_log: list[str] = event["validation_log"]
    cell_id: str = event["cell_id"]
    test_date: str = event["test_date"]
    drive_cycle: str = event["drive_cycle"]

    # Guard clause: S3 key must start with processed/
    if not processed_key.startswith("processed/"):
        raise ValueError(f"S3 key must start with 'processed/', got: {processed_key}")

    s3 = boto3.client("s3")

    # 1. Read processed CSV from S3
    logger.info("Reading processed CSV from s3://%s/%s", bucket, processed_key)
    obj = s3.get_object(Bucket=bucket, Key=processed_key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    # 2. Estimate SOC and duration
    df["SOC"], duration_secs = _estimate_soc(df)

    # 3. Write the SOC-enriched DataFrame back to S3
    soc_key = processed_key.replace("processed/", "soc/", 1)
    logger.info("Writing SOC CSV to s3://%s/%s", bucket, soc_key)

    csv_buffer = io.BytesIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    s3.put_object(Bucket=bucket, Key=soc_key, Body=csv_buffer.getvalue())

    return {
        "bucket": bucket,
        "key": soc_key,
        "validation_log": validation_log,
        "cell_id": cell_id,
        "test_date": test_date,
        "drive_cycle": drive_cycle,
        "duration_secs": duration_secs,
    }
