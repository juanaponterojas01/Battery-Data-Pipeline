"""AWS Lambda function for processing validated Panasonic 18650PF battery test CSVs.

This module is the second step in the battery-data pipeline, triggered by an
AWS Step Function after the Validation Lambda completes.  It reads the
validated CSV from the ``validated/`` prefix, applies signal smoothing via a
Savitzky-Golay filter, performs min-max normalization on key features, and
writes the processed result back to S3 under the ``processed/`` prefix.
"""

import io
import logging
from typing import Any

import boto3
import pandas as pd
from scipy.signal import savgol_filter

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

FILTER_WINDOW = 11
FILTER_POLYORDER = 3

# Normalization bounds for Panasonic 18650PF cells
V_NORM_MIN, V_NORM_MAX = 2.5, 4.2
I_NORM_MIN, I_NORM_MAX = -25.0, 25.0
T_NORM_MIN, T_NORM_MAX = 15.0, 45.0


def _apply_savgol_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Apply Savitzky-Golay filter to Voltage and Current columns.

    If the DataFrame has fewer rows than ``FILTER_WINDOW``, the original
    columns are copied without filtering.

    Args:
        df: DataFrame with ``Voltage`` and ``Current`` columns.

    Returns:
        The DataFrame with ``Voltage_Filtered`` and ``Current_Filtered``
        columns added.
    """
    if len(df) < FILTER_WINDOW:
        logger.warning(
            "Data has fewer than %d rows — skipping Savitzky-Golay filter.",
            FILTER_WINDOW,
        )
        df["Voltage_Filtered"] = df["Voltage"]
        df["Current_Filtered"] = df["Current"]
        return df

    df["Voltage_Filtered"] = savgol_filter(
        df["Voltage"], window_length=FILTER_WINDOW, polyorder=FILTER_POLYORDER
    )
    df["Current_Filtered"] = savgol_filter(
        df["Current"], window_length=FILTER_WINDOW, polyorder=FILTER_POLYORDER
    )
    return df


def _normalize_features(df: pd.DataFrame) -> pd.DataFrame:
    """Min-max normalize Voltage, Current, and Temperature using fixed bounds.

    Args:
        df: DataFrame with ``Voltage_Filtered``, ``Current_Filtered``,
            and ``Temperature`` columns.

    Returns:
        The DataFrame with ``V_norm``, ``I_norm``, and ``T_norm`` columns
        added.
    """
    df["V_norm"] = (df["Voltage_Filtered"] - V_NORM_MIN) / (V_NORM_MAX - V_NORM_MIN)
    df["I_norm"] = (df["Current_Filtered"] - I_NORM_MIN) / (I_NORM_MAX - I_NORM_MIN)
    df["T_norm"] = (df["Temperature"] - T_NORM_MIN) / (T_NORM_MAX - T_NORM_MIN)
    return df


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process a validated battery test CSV from S3.

    Performs the following steps in order:

    1. **Validate S3 key** — must start with ``validated/``.
    2. **Read** the validated CSV from S3.
    3. **Savitzky-Golay filter** — smooths ``Voltage`` and ``Current``.
    4. **Min-max normalization** — scales key features to [0, 1].
    5. **Write** the processed CSV under the ``processed/`` prefix.

    Args:
        event: A dict with the following keys provided by the upstream Step
            Function step:

            - ``bucket`` (:class:`str`): S3 bucket containing the validated CSV.
            - ``key`` (:class:`str`): S3 object key for the validated CSV.
              Must start with ``validated/``.
            - ``validation_log`` (:class:`list`\[:class:`str`]): Log from the
              Validation Lambda.
            - ``cell_id`` (:class:`str`): Panasonic 18650PF cell identifier.
            - ``test_date`` (:class:`str`): Date the test was conducted.
            - ``drive_cycle`` (:class:`str`): Drive-cycle label.

        context: The :class:`LambdaContext` object supplied by the AWS Lambda
            runtime. Not used in this handler.

    Returns:
        A dict with ``bucket``, ``key`` (processed), ``validation_log``,
        ``cell_id``, ``test_date``, and ``drive_cycle``.

    Raises:
        ValueError: If the S3 key does not start with ``validated/``.
        ClientError (boto3): Propagated automatically when S3 operations fail.
    """
    bucket: str = event["bucket"]
    validated_key: str = event["key"]
    validation_log: list[str] = event["validation_log"]
    cell_id: str = event["cell_id"]
    test_date: str = event["test_date"]
    drive_cycle: str = event["drive_cycle"]

    # Guard clause: S3 key must start with validated/
    if not validated_key.startswith("validated/"):
        raise ValueError(f"S3 key must start with 'validated/', got: {validated_key}")

    s3 = boto3.client("s3")

    # 1. Read validated CSV from S3
    logger.info("Reading validated CSV from s3://%s/%s", bucket, validated_key)
    obj = s3.get_object(Bucket=bucket, Key=validated_key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    # 2. Apply Savitzky-Golay filter
    df = _apply_savgol_filter(df)

    # 3. Min-Max Normalization using fixed physical bounds
    df = _normalize_features(df)

    # 4. Write the processed DataFrame back to S3
    processed_key = validated_key.replace("validated/", "processed/", 1)
    logger.info("Writing processed CSV to s3://%s/%s", bucket, processed_key)

    csv_buffer = io.BytesIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    s3.put_object(Bucket=bucket, Key=processed_key, Body=csv_buffer.getvalue())

    return {
        "bucket": bucket,
        "key": processed_key,
        "validation_log": validation_log,
        "cell_id": cell_id,
        "test_date": test_date,
        "drive_cycle": drive_cycle,
    }
