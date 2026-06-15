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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Estimate SOC for a processed battery test CSV from S3.

    Performs the following steps in order:

    1. **Read** the processed CSV from S3 (key must start with ``processed/``).
    2. **Infer initial SOC** from the first-row ``Ah`` value using
       ``SOC_0 = 1 - Ah_0 / Q_CAPACITY_AH``, clipped to [0, 1].
    3. **Coulomb counting** — integrates ``Current_Filtered`` over time to
       compute SOC at every sample, clipped to [0, 1].
    4. **Write** the SOC-enriched CSV under the ``soc/`` prefix.

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
            - ``drive_cycle`` (:class:`str`): Drive-cycle label, e.g. ``"US06"``.

        context: The :class:`LambdaContext` object supplied by the AWS Lambda
            runtime. Not used in this handler.

    Returns:
        A dict with the following keys, intended for the downstream Step
        Function state:

        - ``bucket`` (:class:`str`): The original bucket name.
        - ``key`` (:class:`str`): The S3 key of the **SOC** CSV
          (under the ``soc/`` prefix).
        - ``validation_log`` (:class:`list`\[:class:`str`]): Passed through
          from *event*.
        - ``cell_id`` (:class:`str`): Passed through from *event*.
        - ``test_date`` (:class:`str`): Passed through from *event*.
        - ``drive_cycle`` (:class:`str`): Passed through from *event*.
        - ``duration_secs`` (:class:`float`): Total test duration in seconds.

    Raises:
        ValueError: If the S3 key does not start with the expected
            ``processed/`` prefix.
        ClientError (boto3): Propagated automatically when S3 operations
            fail.
    """
    bucket: str = event["bucket"]
    processed_key: str = event["key"]
    validation_log: list[str] = event["validation_log"]
    cell_id: str = event["cell_id"]
    test_date: str = event["test_date"]
    drive_cycle: str = event["drive_cycle"]

    if not processed_key.startswith("processed/"):
        raise ValueError(
            f"S3 key must start with 'processed/', got: {processed_key}"
        )

    s3 = boto3.client("s3")

    # ------------------------------------------------------------------
    # 1. Read processed CSV from S3 into a pandas DataFrame
    # ------------------------------------------------------------------
    logger.info("Reading processed CSV from s3://%s/%s", bucket, processed_key)
    obj = s3.get_object(Bucket=bucket, Key=processed_key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    # ------------------------------------------------------------------
    # 2. Infer initial SOC from the first-row Ah value
    # ------------------------------------------------------------------
    if "Ah" not in df.columns or pd.isna(df["Ah"].iloc[0]):
        logger.warning("Ah column missing — defaulting initial SOC to 1.0")
        initial_soc = 1.0
    else:
        initial_ah = df["Ah"].iloc[0]
        initial_soc = float(np.clip(1.0 - (initial_ah / Q_CAPACITY_AH), 0.0, 1.0))

    logger.info("Initial SOC: %.4f", initial_soc)

    # ------------------------------------------------------------------
    # 3. Coulomb counting — integrate Current_Filtered over time
    # ------------------------------------------------------------------
    dt_hours = df["Time"].diff().fillna(0) / 3600.0
    delta_ah = df["Current_Filtered"] * dt_hours
    df["SOC"] = initial_soc - (delta_ah.cumsum() / Q_CAPACITY_AH)
    df["SOC"] = df["SOC"].clip(0.0, 1.0)

    # ------------------------------------------------------------------
    # 4. Write the SOC-enriched DataFrame back to S3 under the ``soc/``
    #    prefix.
    # ------------------------------------------------------------------
    soc_key = processed_key.replace("processed/", "soc/", 1)
    logger.info("Writing SOC CSV to s3://%s/%s", bucket, soc_key)

    csv_buffer = io.BytesIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)

    s3.put_object(Bucket=bucket, Key=soc_key, Body=csv_buffer.getvalue())

    duration_secs = float(df["Time"].iloc[-1] - df["Time"].iloc[0])

    return {
        "bucket": bucket,
        "key": soc_key,
        "validation_log": validation_log,
        "cell_id": cell_id,
        "test_date": test_date,
        "drive_cycle": drive_cycle,
        "duration_secs": duration_secs,
    }
