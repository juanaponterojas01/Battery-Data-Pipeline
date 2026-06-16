"""CLI tool for listing and plotting battery test datasets from S3.

This script provides two commands:

- ``list``: Lists available processed datasets stored in the S3 ``results/`` prefix.
- ``plot``: Downloads a dataset (CSV + metadata JSON) from S3 and generates
  a 5-panel A4-optimized report figure saved as ``report.png``.

Usage::

    python generate_report.py list
    python generate_report.py plot US06_25degC_2017-03-20
    python generate_report.py plot US06_25degC_2017-03-20 --force
"""

import json
import sys
from pathlib import Path

import argparse
import boto3
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# S3 Configuration
# ---------------------------------------------------------------------------

PROCESSED_BUCKET = "battery-data-pipeline-batteryprocessedbucket-pdf8wm8jqtgo"
RESULTS_PREFIX = "results/"
METADATA_PREFIX = "metadata/"

# ---------------------------------------------------------------------------
# Plot styling constants
# ---------------------------------------------------------------------------

FIG_WIDTH_IN = 6.5
FIG_HEIGHT_IN = 9.5
DPI = 300
FONT_SIZE = 11
LINE_WIDTH = 1.2
GRID_ALPHA = 0.3
COLOR_PALETTE = "tab10"

REQUIRED_COLUMNS = {"V_norm", "I_norm", "T_norm", "SOC", "Time"}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_s3_client() -> boto3.client:
    """Return a configured boto3 S3 client.

    Returns:
        A boto3 S3 client instance.
    """
    return boto3.client("s3")


def list_datasets(s3_client: boto3.client) -> list[str]:
    """List available dataset names from the S3 ``results/`` prefix.

    Parses S3 object keys of the form ``results/<dataset_name>.csv`` and
    extracts the dataset name portion.

    Args:
        s3_client: A boto3 S3 client.

    Returns:
        A sorted list of dataset name strings.

    Raises:
        SystemExit: If the S3 ``list_objects_v2`` call fails.
    """
    try:
        response = s3_client.list_objects_v2(
            Bucket=PROCESSED_BUCKET, Prefix=RESULTS_PREFIX
        )
    except ClientError as exc:
        print(f"S3 error listing datasets: {exc}", file=sys.stderr)
        sys.exit(1)

    datasets: list[str] = []
    for obj in response.get("Contents", []):
        key = obj["Key"]
        if key.endswith(".csv"):
            # Strip prefix and .csv extension
            name = key[len(RESULTS_PREFIX) : -len(".csv")]
            datasets.append(name)

    return sorted(datasets)


def download_files(
    s3_client: boto3.client, dataset_name: str, local_dir: Path
) -> None:
    """Download the CSV and metadata JSON for a dataset from S3.

    Downloads:
        - ``results/<dataset_name>.csv`` → ``<local_dir>/<dataset_name>.csv``
        - ``metadata/<dataset_name>.json`` → ``<local_dir>/<dataset_name>.json``

    Args:
        s3_client: A boto3 S3 client.
        dataset_name: The dataset name (e.g. ``"US06_25degC_2017-03-20"``).
        local_dir: The local directory to save files into.

    Raises:
        SystemExit: If any S3 download fails.
    """
    local_dir.mkdir(parents=True, exist_ok=True)

    csv_key = f"{RESULTS_PREFIX}{dataset_name}.csv"
    csv_path = local_dir / f"{dataset_name}.csv"
    json_key = f"{METADATA_PREFIX}{dataset_name}.json"
    json_path = local_dir / f"{dataset_name}.json"

    try:
        s3_client.download_file(PROCESSED_BUCKET, csv_key, str(csv_path))
    except ClientError as exc:
        print(f"S3 error downloading CSV: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        s3_client.download_file(PROCESSED_BUCKET, json_key, str(json_path))
    except ClientError as exc:
        print("Metadata JSON not found in S3", file=sys.stderr)
        sys.exit(1)


def load_and_validate_csv(csv_path: Path) -> pd.DataFrame:
    """Load a CSV file and validate that required columns are present.

    Args:
        csv_path: Path to the CSV file.

    Returns:
        A pandas DataFrame with the validated data.

    Raises:
        SystemExit: If required columns are missing.
    """
    df = pd.read_csv(csv_path)

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        print(
            f"Missing required columns in CSV: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        sys.exit(1)

    return df


def load_metadata(json_path: Path) -> dict:
    """Load and return the metadata JSON file.

    Args:
        json_path: Path to the JSON metadata file.

    Returns:
        A dictionary with the metadata contents.
    """
    with open(json_path, "r") as f:
        return json.load(f)


def generate_report(
    df: pd.DataFrame, metadata: dict, dataset_name: str, output_path: Path
) -> None:
    """Generate a 5-panel A4-optimized report figure and save it as PNG.

    The figure contains:
        - Normalized Current vs Time
        - Normalized Voltage vs Time
        - State of Charge vs Time
        - Normalized Temperature vs Time
        - Normalized Voltage vs SOC
        - A summary panel with key metadata values

    Args:
        df: DataFrame containing the battery test data with columns
            ``Time``, ``I_norm``, ``V_norm``, ``SOC``, ``T_norm``.
        metadata: Dictionary with nested sections ``Test_Summary`` and
            ``Key_Performance_Metrics`` containing fields such as
            ``Cell_ID``, ``Drive_Cycle``, ``Test_Date``, ``Duration_Secs``,
            ``Average_SOC``, ``Average_Voltage``, ``Peak_Temperature``,
            ``Capacity_Discharged_Ah``.
        dataset_name: The dataset name used for the figure title.
        output_path: Path where the PNG figure will be saved.
    """
    matplotlib.use("Agg")
    plt.rcParams.update({"font.size": FONT_SIZE})

    fig, axes = plt.subplots(
        3, 2, figsize=(FIG_WIDTH_IN, FIG_HEIGHT_IN), dpi=DPI
    )

    # Parse dataset_name for suptitle: "<Drive_Cycle> (<Test_Date>)"
    # dataset_name format: e.g. "US06_25degC_2017-03-20"
    test_summary = metadata.get("Test_Summary", {})
    drive_cycle = test_summary.get("Drive_Cycle", dataset_name.split("_")[0])
    test_date = test_summary.get("Test_Date", "")
    fig.suptitle(
        f"EV Battery Data Report: {drive_cycle} ({test_date})",
        fontsize=FONT_SIZE + 2,
        fontweight="bold",
        y=0.98,
    )

    color_cycle = plt.get_cmap(COLOR_PALETTE).colors

    # (0,0) Current vs Time
    ax = axes[0, 0]
    ax.plot(
        df["Time"],
        df["I_norm"],
        color=color_cycle[0],
        linewidth=LINE_WIDTH,
    )
    ax.set_title("Normalized Current vs Time", fontsize=FONT_SIZE)
    ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
    ax.set_ylabel("I_norm", fontsize=FONT_SIZE)
    ax.grid(True, alpha=GRID_ALPHA)

    # (0,1) Voltage vs Time
    ax = axes[0, 1]
    ax.plot(
        df["Time"],
        df["V_norm"],
        color=color_cycle[1],
        linewidth=LINE_WIDTH,
    )
    ax.set_title("Normalized Voltage vs Time", fontsize=FONT_SIZE)
    ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
    ax.set_ylabel("V_norm", fontsize=FONT_SIZE)
    ax.grid(True, alpha=GRID_ALPHA)

    # (1,0) SOC vs Time
    ax = axes[1, 0]
    ax.plot(
        df["Time"],
        df["SOC"],
        color=color_cycle[2],
        linewidth=LINE_WIDTH,
    )
    ax.set_title("State of Charge vs Time", fontsize=FONT_SIZE)
    ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
    ax.set_ylabel("SOC", fontsize=FONT_SIZE)
    ax.grid(True, alpha=GRID_ALPHA)

    # (1,1) Temperature vs Time
    ax = axes[1, 1]
    ax.plot(
        df["Time"],
        df["T_norm"],
        color=color_cycle[3],
        linewidth=LINE_WIDTH,
    )
    ax.set_title("Normalized Temperature vs Time", fontsize=FONT_SIZE)
    ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
    ax.set_ylabel("T_norm", fontsize=FONT_SIZE)
    ax.grid(True, alpha=GRID_ALPHA)

    # (2,0) Voltage vs SOC
    ax = axes[2, 0]
    ax.plot(
        df["SOC"],
        df["V_norm"],
        color=color_cycle[4],
        linewidth=LINE_WIDTH,
    )
    ax.set_title("Normalized Voltage vs SOC", fontsize=FONT_SIZE)
    ax.set_xlabel("SOC", fontsize=FONT_SIZE)
    ax.set_ylabel("V_norm", fontsize=FONT_SIZE)
    ax.grid(True, alpha=GRID_ALPHA)

    # (2,1) Summary panel
    ax = axes[2, 1]
    ax.axis("off")

    test_summary = metadata.get("Test_Summary", {})
    kpi = metadata.get("Key_Performance_Metrics", {})

    duration = test_summary.get('Duration_Secs', 'N/A')
    try:
        duration_str = f"{float(duration):.2f} s"
    except (ValueError, TypeError):
        duration_str = f"{duration} s"

    summary_lines = [
        f"Cell: {test_summary.get('Cell_ID', 'N/A')}",
        f"Drive Cycle: {test_summary.get('Drive_Cycle', 'N/A')}",
        f"Date: {test_summary.get('Test_Date', 'N/A')}",
        f"Duration: {duration_str}",
        "",
        f"Avg SOC: {kpi.get('Average_SOC', 'N/A')}",
        f"Avg Voltage: {kpi.get('Average_Voltage', 'N/A')} V",
        f"Peak Temp: {kpi.get('Peak_Temperature', 'N/A')} \u00b0C",
        f"Cap. Discharged: {kpi.get('Capacity_Discharged_Ah', 'N/A')} Ah",
    ]
    summary_text = "\n".join(summary_lines)

    ax.text(
        0.5,
        0.5,
        summary_text,
        transform=ax.transAxes,
        fontsize=FONT_SIZE,
        verticalalignment="center",
        horizontalalignment="center",
        family="monospace",
        bbox=dict(
            boxstyle="round,pad=0.5",
            facecolor="whitesmoke",
            edgecolor="lightgray",
        ),
    )

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(str(output_path), bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for the CLI.

    Returns:
        A configured ``ArgumentParser`` instance.
    """
    parser = argparse.ArgumentParser(
        description="Battery data report generator — list and plot datasets from S3."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list command
    subparsers.add_parser("list", help="List available datasets from S3")

    # plot command
    plot_parser = subparsers.add_parser(
        "plot", help="Download and generate plots for a dataset"
    )
    plot_parser.add_argument(
        "dataset_name",
        help="Name of the dataset to plot (e.g. US06_25degC_2017-03-20)",
    )
    plot_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files from S3 even if cached locally",
    )

    return parser


def cmd_list(s3_client: boto3.client) -> None:
    """Handle the ``list`` subcommand.

    Args:
        s3_client: A boto3 S3 client.
    """
    datasets = list_datasets(s3_client)
    if not datasets:
        print("No datasets found in S3.")
        return

    print("Available datasets:")
    for i, name in enumerate(datasets, start=1):
        print(f"  {i}. {name}")


def cmd_plot(s3_client: boto3.client, dataset_name: str, force: bool) -> None:
    """Handle the ``plot`` subcommand.

    Args:
        s3_client: A boto3 S3 client.
        dataset_name: The dataset name to plot.
        force: If True, re-download files even if cached.

    Raises:
        SystemExit: If the dataset is not found or any operation fails.
    """
    # Verify dataset exists in S3
    datasets = list_datasets(s3_client)
    if dataset_name not in datasets:
        print(
            "Dataset not found. Run `list` to see available datasets.",
            file=sys.stderr,
        )
        if datasets:
            print("\nAvailable datasets:", file=sys.stderr)
            for name in datasets:
                print(f"  - {name}", file=sys.stderr)
        sys.exit(1)

    local_dir = Path("data") / "processed_data" / dataset_name
    csv_path = local_dir / f"{dataset_name}.csv"
    json_path = local_dir / f"{dataset_name}.json"

    # Check cache
    if csv_path.exists() and json_path.exists() and not force:
        print("Using cached files.")
    else:
        print(f"Downloading {dataset_name} from S3...")
        download_files(s3_client, dataset_name, local_dir)

    # Load and validate
    print("Loading data...")
    df = load_and_validate_csv(csv_path)
    metadata = load_metadata(json_path)

    # Generate report
    output_path = local_dir / "report.png"
    print("Generating report...")
    generate_report(df, metadata, dataset_name, output_path)

    print(f"Report saved to: {output_path}")


def main() -> None:
    """Entry point for the CLI tool."""
    parser = build_parser()
    args = parser.parse_args()

    s3_client = get_s3_client()

    if args.command == "list":
        cmd_list(s3_client)
    elif args.command == "plot":
        cmd_plot(s3_client, args.dataset_name, args.force)


if __name__ == "__main__":
    main()
