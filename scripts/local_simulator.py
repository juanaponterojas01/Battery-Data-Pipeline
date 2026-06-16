#!/usr/bin/env python3
"""Local simulator for the EV Battery Data Pipeline.

Runs the 4 Lambda processing steps sequentially on a local machine,
using local CSV/JSON files instead of S3:

    Validation -> Processing -> SOC Estimation -> Metadata/Report

Usage::

    python scripts/local_simulator.py \\
        --csv data/raw_cell_test.csv \\
        --meta data/cell_test.meta.json \\
        --output-dir ./local_output
"""

import argparse
import io
import json
import logging
import os
import shutil
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

# ---------------------------------------------------------------------------
# Add the src/ directory to the import path so Lambda handlers can be imported
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from metadata.app import lambda_handler as generate_metadata
from processing.app import lambda_handler as process
from soc_estimation.app import lambda_handler as estimate_soc
from validation.app import lambda_handler as validate

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Bucket names used in the simulated event payloads.
_SOURCE_BUCKET = "battery-data-source"
_DEST_BUCKET = "battery-data-dest"


class MockS3:
    """Minimal S3 client mock that reads/writes files on the local filesystem.

    Maps S3 ``Bucket/Key`` paths to ``base_dir / Bucket / Key`` on disk.
    This allows the Lambda handlers — which call ``boto3.client("s3")`` and
    then ``get_object`` / ``put_object`` — to operate transparently against
    local files.

    Args:
        base_dir: Root directory under which all bucket/key paths are
            resolved.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()

    def get_object(self, *, Bucket: str, Key: str, **kwargs: Any) -> dict[str, Any]:
        """Read a file from the local filesystem mimicking S3 ``GetObject``."""
        file_path = self.base_dir / Bucket / Key
        if not file_path.exists():
            raise FileNotFoundError(
                f"MockS3: file not found at {file_path} "
                f"(Bucket={Bucket!r}, Key={Key!r})"
            )
        return {"Body": io.BytesIO(file_path.read_bytes())}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes | str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Write data to the local filesystem mimicking S3 ``PutObject``."""
        file_path = self.base_dir / Bucket / Key
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(Body, str):
            file_path.write_text(Body, encoding="utf-8")
        else:
            file_path.write_bytes(Body)

        return {}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Parsed namespace with ``csv``, ``meta``, and ``output_dir``.
    """
    parser = argparse.ArgumentParser(
        description="Run the EV Battery Data Pipeline locally.",
    )
    parser.add_argument(
        "--csv",
        required=True,
        type=Path,
        help="Path to the local raw CSV file.",
    )
    parser.add_argument(
        "--meta",
        required=True,
        type=Path,
        help="Path to the local .meta.json file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./local_output"),
        help="Directory to save intermediate and final outputs (default: ./local_output).",
    )
    return parser.parse_args(argv)


def _setup_output_dir(output_dir: Path) -> None:
    """Create the output directory tree, cleaning it if it already exists."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _load_meta(meta_path: Path) -> dict[str, str]:
    """Load and validate the ``.meta.json`` sidecar file.

    Args:
        meta_path: Path to the JSON metadata file.

    Returns:
        Dict with keys ``cell_id``, ``test_date``, and ``drive_cycle``.

    Raises:
        FileNotFoundError: If the metadata file does not exist.
        ValueError: If any required key is missing from the JSON.
    """
    if not meta_path.exists():
        raise FileNotFoundError(f"Metadata file not found: {meta_path}")

    with open(meta_path, encoding="utf-8") as fh:
        meta: dict[str, Any] = json.load(fh)

    required_keys = {"cell_id", "test_date", "drive_cycle"}
    missing = required_keys - set(meta.keys())
    if missing:
        raise ValueError(
            f"Metadata file {meta_path} is missing required keys: {sorted(missing)}"
        )

    return {
        "cell_id": str(meta["cell_id"]),
        "test_date": str(meta["test_date"]),
        "drive_cycle": str(meta["drive_cycle"]),
    }


def _seed_raw_csv(csv_path: Path, output_dir: Path, bucket: str) -> str:
    """Copy the raw CSV into the mock S3 directory structure.

    The Validation Lambda expects the file at ``raw/<filename>`` inside the
    source bucket. This function copies the user-supplied CSV to
    ``output_dir / bucket / raw / <filename>``.

    Args:
        csv_path: Path to the user-supplied raw CSV.
        output_dir: Root output directory (mock S3 root).
        bucket: Logical bucket name.

    Returns:
        The S3-style key (e.g. ``"raw/my_file.csv"``).

    Raises:
        FileNotFoundError: If the source CSV does not exist.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Raw CSV not found: {csv_path}")

    raw_key = f"raw/{csv_path.name}"
    dest = output_dir / bucket / raw_key
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(csv_path, dest)
    return raw_key


def _save_dataframe_from_s3(
    mock_s3: MockS3,
    bucket: str,
    key: str,
    output_dir: Path,
    label: str,
) -> Path:
    """Read a CSV stored by the mock S3 and save a human-readable copy.

    After each Lambda writes its output via ``put_object``, this helper
    reads it back and saves a copy under ``output_dir/intermediate/``
    for easy inspection.

    Args:
        mock_s3: The active MockS3 instance.
        bucket: Bucket name where the file lives.
        key: S3-style key of the file.
        output_dir: Root output directory.
        label: Human-readable label for the step (e.g. ``"validated"``).

    Returns:
        Path to the saved intermediate CSV.
    """
    intermediate_dir = output_dir / "intermediate"
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    obj = mock_s3.get_object(Bucket=bucket, Key=key)
    df = pd.read_csv(io.BytesIO(obj["Body"].read()))

    out_path = intermediate_dir / f"{label}.csv"
    df.to_csv(out_path, index=False)
    return out_path


def _execute_step(
    handler: Callable[[dict[str, Any], Any], dict[str, Any]],
    event: dict[str, Any],
    mock_s3_factory: Callable[..., Any],
    step_label: str,
) -> dict[str, Any]:
    """Execute a Lambda handler with ``boto3.client`` patched to MockS3.

    Args:
        handler: The Lambda handler function to invoke.
        event: The event payload.
        mock_s3_factory: Callable that returns the MockS3 instance.
        step_label: Human-readable name for error messages.

    Returns:
        The handler's return dict.

    Raises:
        RuntimeError: If the handler raises an exception.
    """
    try:
        with patch("boto3.client", mock_s3_factory):
            return handler(event, None)
    except Exception:
        print(f"\n[ERROR] {step_label} step failed!")
        traceback.print_exc()
        raise


def main(argv: list[str] | None = None) -> int:
    """Orchestrate the full 4-step pipeline simulation.

    Steps executed in order:

    1. **Validation** — structural checks, column renaming, sanity bounds.
    2. **Processing** — Savitzky-Golay smoothing + min-max normalization.
    3. **SOC Estimation** — Coulomb counting to estimate State of Charge.
    4. **Metadata** — performance metrics + JSON report generation.

    Args:
        argv: Optional CLI argument list (for testing).

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    args = _parse_args(argv)
    output_dir: Path = args.output_dir.resolve()

    print("=" * 60)
    print("  EV Battery Data Pipeline — Local Simulator")
    print("=" * 60)
    print(f"  Raw CSV   : {args.csv}")
    print(f"  Meta JSON : {args.meta}")
    print(f"  Output dir: {output_dir}")
    print("=" * 60)

    # Pre-flight: load metadata, prepare output directory, seed raw CSV
    try:
        meta = _load_meta(args.meta)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"\n[ERROR] Metadata loading failed: {exc}")
        return 1

    _setup_output_dir(output_dir)
    mock_s3 = MockS3(output_dir)

    try:
        raw_key = _seed_raw_csv(args.csv, output_dir, _SOURCE_BUCKET)
    except FileNotFoundError as exc:
        print(f"\n[ERROR] {exc}")
        return 1

    # Factory function that returns the MockS3 instance for boto3 patching
    def _mock_s3_factory(*args: Any, **kwargs: Any) -> MockS3:
        return mock_s3

    # ------------------------------------------------------------------
    # Step 1: Validation
    # ------------------------------------------------------------------
    print("\n[Step 1/4] Validation ...")
    try:
        validation_event: dict[str, Any] = {
            "bucket": _SOURCE_BUCKET,
            "key": raw_key,
            "cell_id": meta["cell_id"],
            "test_date": meta["test_date"],
            "drive_cycle": meta["drive_cycle"],
        }
        validation_result = _execute_step(
            validate, validation_event, _mock_s3_factory, "Validation"
        )

        validated_key = validation_result["key"]
        _save_dataframe_from_s3(
            mock_s3, _SOURCE_BUCKET, validated_key, output_dir, "validated"
        )
        print(f"  -> Validated CSV key : {validated_key}")
        print(f"  -> Validation log    : {validation_result['validation_log']}")
    except Exception:
        return 1

    # ------------------------------------------------------------------
    # Step 2: Processing
    # ------------------------------------------------------------------
    print("\n[Step 2/4] Processing ...")
    try:
        processing_event: dict[str, Any] = {
            "bucket": validation_result["bucket"],
            "key": validated_key,
            "validation_log": validation_result["validation_log"],
            "cell_id": meta["cell_id"],
            "test_date": meta["test_date"],
            "drive_cycle": meta["drive_cycle"],
        }
        processing_result = _execute_step(
            process, processing_event, _mock_s3_factory, "Processing"
        )

        processed_key = processing_result["key"]
        _save_dataframe_from_s3(
            mock_s3, _SOURCE_BUCKET, processed_key, output_dir, "processed"
        )
        print(f"  -> Processed CSV key : {processed_key}")
    except Exception:
        return 1

    # ------------------------------------------------------------------
    # Step 3: SOC Estimation
    # ------------------------------------------------------------------
    print("\n[Step 3/4] SOC Estimation ...")
    try:
        soc_event: dict[str, Any] = {
            "bucket": processing_result["bucket"],
            "key": processed_key,
            "validation_log": processing_result["validation_log"],
            "cell_id": meta["cell_id"],
            "test_date": meta["test_date"],
            "drive_cycle": meta["drive_cycle"],
        }
        soc_result = _execute_step(
            estimate_soc, soc_event, _mock_s3_factory, "SOC Estimation"
        )

        soc_key = soc_result["key"]
        _save_dataframe_from_s3(mock_s3, _SOURCE_BUCKET, soc_key, output_dir, "soc")
        print(f"  -> SOC CSV key       : {soc_key}")
        print(f"  -> Duration (secs)   : {soc_result['duration_secs']:.2f}")
    except Exception:
        return 1

    # ------------------------------------------------------------------
    # Step 4: Metadata / Report
    # ------------------------------------------------------------------
    print("\n[Step 4/4] Metadata & Report ...")
    try:
        # The metadata Lambda reads DEST_BUCKET from the environment.
        os.environ["DEST_BUCKET"] = _DEST_BUCKET

        metadata_event: dict[str, Any] = {
            "bucket": soc_result["bucket"],
            "key": soc_key,
            "validation_log": soc_result["validation_log"],
            "cell_id": meta["cell_id"],
            "test_date": meta["test_date"],
            "drive_cycle": meta["drive_cycle"],
            "duration_secs": soc_result["duration_secs"],
        }
        metadata_result = _execute_step(
            generate_metadata, metadata_event, _mock_s3_factory, "Metadata"
        )

        print(f"  -> CSV output  : {metadata_result['csv_output']}")
        print(f"  -> JSON output : {metadata_result['json_output']}")
    except Exception:
        return 1

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  Pipeline completed successfully!")
    print("=" * 60)

    # List all output files
    print("\n  Output files:")
    for f in sorted(output_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(output_dir)
            size = f.stat().st_size
            print(f"    {rel}  ({size:,} bytes)")

    # Print the metadata JSON content
    json_key = f"metadata/{meta['drive_cycle']}_{meta['test_date']}.json"
    json_path = output_dir / _DEST_BUCKET / json_key
    if json_path.exists():
        print(f"\n  Metadata JSON ({json_path.relative_to(output_dir)}):")
        print("-" * 40)
        print(json_path.read_text(encoding="utf-8"))
        print("-" * 40)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
