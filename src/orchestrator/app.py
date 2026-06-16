"""Orchestrator Lambda for the EV Battery Data Pipeline.

Triggered by S3 PUT events (via EventBridge). When a CSV file is uploaded
to the raw bucket alongside a .meta.json sidecar, this Lambda starts the
Step Functions state machine execution.
"""

import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
sfn_client = boto3.client("stepfunctions")


def _sanitize(value: str) -> str:
    """Replace non-alphanumeric characters with hyphens."""
    return re.sub(r"[^a-zA-Z0-9]", "-", value)


def _read_meta_json(bucket: str, meta_key: str) -> dict[str, str]:
    """Read and parse a ``.meta.json`` sidecar file from S3.

    Args:
        bucket: S3 bucket name.
        meta_key: S3 object key for the metadata JSON file.

    Returns:
        Parsed metadata dict with ``cell_id``, ``test_date``, and
        ``drive_cycle`` keys.

    Raises:
        ValueError: If the metadata sidecar file is not found in S3.
    """
    try:
        response = s3_client.get_object(Bucket=bucket, Key=meta_key)
        meta_body = response["Body"].read()
    except s3_client.exceptions.NoSuchKey:
        raise ValueError(
            f"Metadata sidecar file not found: s3://{bucket}/{meta_key}"
        )

    metadata: dict[str, str] = json.loads(meta_body)
    return metadata


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle S3 EventBridge events and start the Step Functions state machine.

    Args:
        event: S3 EventBridge event containing bucket and object details.
        context: Lambda runtime context.

    Returns:
        Dict with status, execution ARN, and start date.

    Raises:
        ValueError: If STATE_MACHINE_ARN is not set or the metadata sidecar
            file is missing.
    """
    # 1. Extract bucket and key from the EventBridge event
    detail = event["detail"]
    bucket = detail["bucket"]["name"]
    raw_key = detail["object"]["key"]
    key = urllib.parse.unquote(raw_key)

    logger.info("Received event for s3://%s/%s", bucket, key)

    # 2. Validate that the key is a CSV file under raw/
    if not key.startswith("raw/") or not key.endswith(".csv"):
        logger.info("Skipping non-CSV or non-raw file: s3://%s/%s", bucket, key)
        return {
            "status": "SKIPPED",
            "reason": "Not a CSV file in raw/ prefix",
        }

    # 3. Construct and read the .meta.json sidecar key
    meta_key = key.removesuffix(".csv") + ".meta.json"
    logger.info("Looking for metadata sidecar: s3://%s/%s", bucket, meta_key)

    metadata = _read_meta_json(bucket, meta_key)
    cell_id = metadata["cell_id"]
    test_date = metadata["test_date"]
    drive_cycle = metadata["drive_cycle"]

    logger.info(
        "Metadata parsed: cell_id=%s, test_date=%s, drive_cycle=%s",
        cell_id,
        test_date,
        drive_cycle,
    )

    # 4. Start the Step Functions execution
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN")
    if not state_machine_arn:
        raise ValueError("STATE_MACHINE_ARN environment variable is not set")

    execution_name = f"{_sanitize(drive_cycle)}_{_sanitize(test_date)}_{int(time.time())}"

    input_payload = json.dumps(
        {
            "bucket": bucket,
            "key": key,
            "cell_id": cell_id,
            "test_date": test_date,
            "drive_cycle": drive_cycle,
        }
    )

    logger.info("Starting Step Functions execution: %s", execution_name)

    response = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=input_payload,
    )

    logger.info(
        "Step Functions started: executionArn=%s, startDate=%s",
        response["executionArn"],
        response["startDate"],
    )

    # 5. Return success response
    return {
        "status": "SUCCESS",
        "executionArn": response["executionArn"],
        "startDate": response["startDate"].isoformat() + "Z",
    }
