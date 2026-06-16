"""AWS Lambda function for metadata extraction and report generation.

This module is the final step in the battery-data pipeline, triggered by an
AWS Step Function after the SOC Estimation Lambda completes.  It reads the
SOC-enriched CSV from the ``soc/`` prefix, computes key performance metrics,
constructs a structured metadata JSON document, and writes both the original
CSV and the metadata JSON to the destination (processed) S3 bucket.
"""

import io
import json
import logging
import os
from typing import Any

import boto3
import pandas as pd

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

Q_CAPACITY_AH: float = 2.9
"""Nominal capacity of a Panasonic 18650PF cell in Ampere-hours."""

NORMALIZATION_PARAMS: dict[str, float] = {
    "Vmax": 4.2,
    "Vmin": 2.5,
    "Imax": 25.0,
    "Imin": -25.0,
    "Tmax": 45.0,
    "Tmin": 15.0,
}
"""Min-max normalization bounds used during preprocessing."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Extract metadata and generate reports for a battery test from S3.

    Performs the following steps in order:

    1. **Read** the SOC-enriched CSV from the source S3 bucket.
    2. **Calculate** key performance metrics (average SOC, average voltage,
       peak temperature, discharged capacity).
    3. **Construct** a structured metadata JSON document.
    4. **Write** the CSV and metadata JSON to the destination bucket.

    Args:
        event: A dict with the following keys provided by the upstream Step
            Function step:

            - ``bucket`` (:class:`str`): Source S3 bucket containing the SOC CSV.
            - ``key`` (:class:`str`): S3 object key for the SOC CSV
              (under the ``soc/`` prefix).
            - ``validation_log`` (:class:`list`\[:class:`str`]): Log from the
              Validation Lambda.
            - ``cell_id`` (:class:`str`): Panasonic 18650PF cell identifier.
            - ``test_date`` (:class:`str`): Date the test was conducted.
            - ``drive_cycle`` (:class:`str`): Drive-cycle label, e.g. ``"UDDS_10degC"``.
            - ``duration_secs`` (:class:`float`): Total test duration in seconds.

        context: The :class:`LambdaContext` object supplied by the AWS Lambda
            runtime. Not used in this handler.

    Returns:
        A dict with the following keys:

        - ``status`` (:class:`str`): ``"SUCCESS"`` on completion.
        - ``csv_output`` (:class:`str`): S3 URI of the written CSV file.
        - ``json_output`` (:class:`str`): S3 URI of the written metadata JSON.

    Raises:
        ValueError: If the ``DEST_BUCKET`` environment variable is not set.
        ClientError (boto3): Propagated automatically when S3 operations fail.
    """
    bucket: str = event["bucket"]
    soc_key: str = event["key"]
    validation_log: list[str] = event["validation_log"]
    cell_id: str = event["cell_id"]
    test_date: str = event["test_date"]
    drive_cycle: str = event["drive_cycle"]
    duration_secs: float = event["duration_secs"]

    dest_bucket: str | None = os.environ.get("DEST_BUCKET")
    if not dest_bucket:
        raise ValueError(
            "DEST_BUCKET environment variable is not set"
        )

    s3 = boto3.client("s3")

    # ------------------------------------------------------------------
    # 1. Read the SOC-enriched CSV from S3
    # ------------------------------------------------------------------
    logger.info("Reading SOC CSV from s3://%s/%s", bucket, soc_key)
    obj = s3.get_object(Bucket=bucket, Key=soc_key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    # ------------------------------------------------------------------
    # 2. Calculate key performance metrics
    # ------------------------------------------------------------------
    avg_soc: float = float(df["SOC"].mean())
    avg_voltage: float = float(df["Voltage_Filtered"].mean())
    peak_temp: float = float(df["Temperature"].max())
    capacity_discharged: float = float(
        (df["SOC"].iloc[0] - df["SOC"].iloc[-1]) * Q_CAPACITY_AH
    )

    # Additional metrics for the report
    energy_delivered: float = capacity_discharged * avg_voltage
    peak_discharge_current: float = float(df["Current_Filtered"].max())
    peak_regen_current: float = abs(float(df["Current_Filtered"].min()))
    voltage_sag: float = float(
        df["Voltage_Filtered"].iloc[0] - df["Voltage_Filtered"].min()
    )
    min_voltage: float = float(df["Voltage_Filtered"].min())
    starting_temp: float = float(df["Temperature"].iloc[0])
    delta_temp: float = peak_temp - starting_temp
    avg_c_rate: float = float(df["Current_Filtered"].abs().mean() / Q_CAPACITY_AH)

    logger.info(
        "Metrics — avg_soc=%.4f, avg_voltage=%.4f, peak_temp=%.2f, "
        "capacity_discharged=%.4f Ah, energy=%.4f Wh, "
        "I_peak=%.2f A, I_regen=%.2f A, dV=%.4f V, "
        "T_start=%.2f C, dT=%.2f C, C_rate=%.4f",
        avg_soc,
        avg_voltage,
        peak_temp,
        capacity_discharged,
        energy_delivered,
        peak_discharge_current,
        peak_regen_current,
        voltage_sag,
        starting_temp,
        delta_temp,
        avg_c_rate,
    )

    # ------------------------------------------------------------------
    # 3. Construct the metadata JSON document
    # ------------------------------------------------------------------
    metadata: dict[str, Any] = {
        "Test_Summary": {
            "Cell_ID": cell_id,
            "Test_Date": test_date,
            "Drive_Cycle": drive_cycle,
            "Duration_Secs": duration_secs,
        },
        "Key_Performance_Metrics": {
            "Average_SOC": round(avg_soc, 4),
            "Average_Voltage": round(avg_voltage, 4),
            "Peak_Temperature": round(peak_temp, 2),
            "Capacity_Discharged_Ah": round(capacity_discharged, 4),
            "Energy_Delivered_Wh": round(energy_delivered, 4),
            "Peak_Discharge_Current_A": round(peak_discharge_current, 2),
            "Peak_Regen_Current_A": round(peak_regen_current, 2),
            "Voltage_Sag_V": round(voltage_sag, 4),
            "Minimum_Voltage_V": round(min_voltage, 4),
            "Starting_Temperature_C": round(starting_temp, 2),
            "Delta_Temperature_C": round(delta_temp, 2),
            "Average_C_Rate": round(avg_c_rate, 4),
        },
        "Data_Processing_Log": {
            "Validation": validation_log,
            "Filter": "Savitzky-Golay (window=11, polyorder=3) applied to V,I",
            "Normalization": NORMALIZATION_PARAMS,
            "SOC_Method": "Coulomb Counting (Initial SOC inferred from Ah column)",
        },
    }

    metadata_json: str = json.dumps(metadata, indent=4)

    # ------------------------------------------------------------------
    # 4. Write outputs to the destination bucket
    # ------------------------------------------------------------------
    csv_key: str = f"results/{drive_cycle}_{test_date}.csv"
    json_key: str = f"metadata/{drive_cycle}_{test_date}.json"

    logger.info("Writing CSV to s3://%s/%s", dest_bucket, csv_key)
    csv_buffer = io.BytesIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    s3.put_object(Bucket=dest_bucket, Key=csv_key, Body=csv_buffer.getvalue())

    logger.info("Writing metadata JSON to s3://%s/%s", dest_bucket, json_key)
    s3.put_object(
        Bucket=dest_bucket,
        Key=json_key,
        Body=metadata_json.encode("utf-8"),
        ContentType="application/json",
    )

    csv_output: str = f"s3://{dest_bucket}/{csv_key}"
    json_output: str = f"s3://{dest_bucket}/{json_key}"

    return {
        "status": "SUCCESS",
        "csv_output": csv_output,
        "json_output": json_output,
    }
