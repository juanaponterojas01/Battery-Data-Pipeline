"""CLI tool for listing and generating PDF reports of battery test datasets from S3.

This script provides two commands:

- ``list``: Lists available processed datasets stored in the S3 ``results/`` prefix.
- ``generate``: Downloads a dataset (CSV + metadata JSON) from S3 and generates
  a professional 2-page A4 PDF report.

Usage::

    python generate_report.py list
    python generate_report.py generate US06_25degC_2017-03-20
    python generate_report.py generate US06_25degC_2017-03-20 --force
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
from matplotlib.backends.backend_pdf import PdfPages

# ---------------------------------------------------------------------------
# S3 Configuration
# ---------------------------------------------------------------------------

PROCESSED_BUCKET = "battery-data-pipeline-batteryprocessedbucket-pdf8wm8jqtgo"
RESULTS_PREFIX = "results/"
METADATA_PREFIX = "metadata/"

# ---------------------------------------------------------------------------
# Report styling constants
# ---------------------------------------------------------------------------

A4_WIDTH_IN = 8.27
A4_HEIGHT_IN = 11.69
DPI = 300
FONT_SIZE = 11
TITLE_FONT_SIZE = 13
LINE_WIDTH = 1.2
GRID_ALPHA = 0.3
COLOR_PALETTE = "tab10"

REQUIRED_COLUMNS = {
    "Time",
    "Voltage_Filtered",
    "Current_Filtered",
    "Temperature",
    "SOC",
}

# ---------------------------------------------------------------------------
# Status thresholds
# ---------------------------------------------------------------------------

PEAK_TEMP_THRESHOLD = 45.0  # °C
VOLTAGE_SAG_THRESHOLD = 0.5  # V


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_s3_client() -> boto3.client:
    """Return a configured boto3 S3 client."""
    return boto3.client("s3")


def list_datasets(s3_client: boto3.client) -> list[str]:
    """List available dataset names from the S3 ``results/`` prefix."""
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
            name = key[len(RESULTS_PREFIX) : -len(".csv")]
            datasets.append(name)

    return sorted(datasets)


def download_files(
    s3_client: boto3.client, dataset_name: str, local_dir: Path
) -> None:
    """Download the CSV and metadata JSON for a dataset from S3."""
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
    """Load a CSV file and validate that required columns are present."""
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
    """Load and return the metadata JSON file."""
    with open(json_path, "r") as f:
        return json.load(f)


def get_status(metric_name: str, value: float) -> tuple[str, str]:
    """Return (status_text, color_hex) for a metric value.

    Args:
        metric_name: Name of the metric (e.g. ``"Peak_Temperature"``).
        value: The metric value.

    Returns:
        A tuple of (status text, background color hex string).
    """
    if metric_name == "Peak_Temperature":
        if value >= PEAK_TEMP_THRESHOLD:
            return ("⚠️ Review", "#ffcccc")
        return ("✅ Safe", "#ccffcc")

    if metric_name == "Voltage_Sag":
        if value >= VOLTAGE_SAG_THRESHOLD:
            return ("⚠️ Review", "#ffcccc")
        return ("✅ Normal", "#ccffcc")

    return ("", "#ffffff")


def draw_metrics_table(ax: plt.Axes, metadata: dict) -> None:
    """Draw the Executive Metrics table on the given axes.

    Args:
        ax: Matplotlib axes to draw the table on.
        metadata: Metadata dictionary containing ``Key_Performance_Metrics``.
    """
    ax.axis("off")
    kpi = metadata.get("Key_Performance_Metrics", {})

    metrics = [
        ("Capacity Discharged", f"{kpi.get('Capacity_Discharged_Ah', 'N/A')} Ah"),
        ("Energy Delivered", f"{kpi.get('Energy_Delivered_Wh', 'N/A')} Wh"),
        ("Peak Discharge Current", f"{kpi.get('Peak_Discharge_Current_A', 'N/A')} A"),
        ("Peak Regen Current", f"{kpi.get('Peak_Regen_Current_A', 'N/A')} A"),
        ("Voltage Sag", f"{kpi.get('Voltage_Sag_V', 'N/A')} V"),
        ("Peak Temperature", f"{kpi.get('Peak_Temperature', 'N/A')} °C"),
        ("Delta Temperature", f"{kpi.get('Delta_Temperature_C', 'N/A')} °C"),
        ("Average C-Rate", f"{kpi.get('Average_C_Rate', 'N/A')} C"),
    ]

    # Build cell colors based on status
    cell_text = [[name, value] for name, value in metrics]
    cell_colors = []
    for name, _ in metrics:
        if "Temperature" in name and "Peak" in name:
            try:
                val = float(kpi.get("Peak_Temperature", 0))
                _, color = get_status("Peak_Temperature", val)
            except (ValueError, TypeError):
                color = "#ffffff"
            cell_colors.append(["#f0f0f0", color])
        elif "Voltage Sag" in name:
            try:
                val = float(kpi.get("Voltage_Sag_V", 0))
                _, color = get_status("Voltage_Sag", val)
            except (ValueError, TypeError):
                color = "#ffffff"
            cell_colors.append(["#f0f0f0", color])
        else:
            cell_colors.append(["#f0f0f0", "#ffffff"])

    table = ax.table(
        cellText=cell_text,
        cellColours=cell_colors,
        colWidths=[0.55, 0.45],
        loc="center",
        cellLoc="left",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(FONT_SIZE)
    table.scale(1, 1.8)

    for key, cell in table.get_celld().items():
        cell.set_edgecolor("lightgray")
        if key[1] == 0:
            cell.set_text_props(fontweight="bold")

    ax.set_title("Executive Metrics", fontsize=TITLE_FONT_SIZE, fontweight="bold", pad=10)


def add_caption(ax: plt.Axes, text: str) -> None:
    """Add a descriptive caption below an axes."""
    ax.annotate(
        text,
        xy=(0.5, -0.14),
        xycoords="axes fraction",
        ha="center",
        va="top",
        fontsize=FONT_SIZE - 2,
        style="italic",
        color="dimgray",
    )


def generate_report(
    df: pd.DataFrame, metadata: dict, dataset_name: str, output_path: Path
) -> None:
    """Generate a 2-page A4 PDF report and save it.

    Args:
        df: DataFrame containing the battery test data with columns
            ``Time``, ``Voltage_Filtered``, ``Current_Filtered``,
            ``Temperature``, ``SOC``.
        metadata: Dictionary with nested sections ``Test_Summary``,
            ``Key_Performance_Metrics``, and ``Data_Processing_Log``.
        dataset_name: The dataset name used for the report title.
        output_path: Path where the PDF will be saved.
    """
    matplotlib.use("Agg")
    plt.rcParams.update({"font.size": FONT_SIZE})

    color_cycle = plt.get_cmap(COLOR_PALETTE).colors
    test_summary = metadata.get("Test_Summary", {})
    drive_cycle = test_summary.get("Drive_Cycle", dataset_name.split("_")[0])
    test_date = test_summary.get("Test_Date", "")

    with PdfPages(str(output_path)) as pdf:
        # ==================================================================
        # PAGE 1: Title, Summary, Metrics Table, Current, Voltage
        # ==================================================================
        fig1, axes1 = plt.subplots(3, 2, figsize=(A4_WIDTH_IN, A4_HEIGHT_IN), dpi=DPI)
        fig1.suptitle(
            f"Battery Drive Cycle Performance Report\n{drive_cycle} ({test_date})",
            fontsize=TITLE_FONT_SIZE + 2,
            fontweight="bold",
            y=0.97,
        )

        # (0,0) — Test Summary
        ax = axes1[0, 0]
        ax.axis("off")
        cell_id = test_summary.get("Cell_ID", "N/A")
        duration = test_summary.get("Duration_Secs", "N/A")
        try:
            duration_str = f"{float(duration):.2f} s"
        except (ValueError, TypeError):
            duration_str = f"{duration} s"

        summary_text = (
            f"Cell Model: {cell_id}\n"
            f"Drive Cycle: {drive_cycle}\n"
            f"Test Date: {test_date}\n"
            f"Duration: {duration_str}"
        )
        ax.text(
            0.5,
            0.5,
            summary_text,
            transform=ax.transAxes,
            fontsize=FONT_SIZE,
            verticalalignment="center",
            horizontalalignment="center",
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="whitesmoke", edgecolor="lightgray"),
        )
        ax.set_title("Test Summary", fontsize=TITLE_FONT_SIZE, fontweight="bold", pad=10)

        # (0,1) — Executive Metrics Table
        draw_metrics_table(axes1[0, 1], metadata)

        # (1,0) — Current vs Time
        ax = axes1[1, 0]
        ax.plot(df["Time"], df["Current_Filtered"], color=color_cycle[0], linewidth=LINE_WIDTH)
        ax.set_title("1. Applied Load Profile", fontsize=TITLE_FONT_SIZE, fontweight="bold")
        ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
        ax.set_ylabel("Current (A)", fontsize=FONT_SIZE)
        ax.grid(True, alpha=GRID_ALPHA)
        add_caption(ax, "Shows the 'stress' applied to the battery during the drive cycle.")

        # (1,1) — Voltage vs Time
        ax = axes1[1, 1]
        ax.plot(df["Time"], df["Voltage_Filtered"], color=color_cycle[1], linewidth=LINE_WIDTH)
        ax.set_title("2. Electrical Response", fontsize=TITLE_FONT_SIZE, fontweight="bold")
        ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
        ax.set_ylabel("Voltage (V)", fontsize=FONT_SIZE)
        ax.grid(True, alpha=GRID_ALPHA)
        add_caption(
            ax, "The 'heartbeat' of the battery. Deep drops indicate high internal resistance."
        )

        # (2,0) — Empty spacer
        axes1[2, 0].axis("off")

        # (2,1) — Empty spacer
        axes1[2, 1].axis("off")

        fig1.subplots_adjust(left=0.08, right=0.95, top=0.90, bottom=0.08, hspace=0.45, wspace=0.35)
        pdf.savefig(fig1)
        plt.close(fig1)

        # ==================================================================
        # PAGE 2: SOC, Temperature, V-vs-SOC, Processing Notes
        # ==================================================================
        fig2, axes2 = plt.subplots(3, 2, figsize=(A4_WIDTH_IN, A4_HEIGHT_IN), dpi=DPI)
        fig2.suptitle(
            f"Battery Drive Cycle Performance Report (cont.)\n{drive_cycle} ({test_date})",
            fontsize=TITLE_FONT_SIZE + 2,
            fontweight="bold",
            y=0.97,
        )

        # (0,0) — SOC vs Time
        ax = axes2[0, 0]
        ax.plot(df["Time"], df["SOC"] * 100, color=color_cycle[2], linewidth=LINE_WIDTH)
        ax.set_title("3. State of Charge Depletion", fontsize=TITLE_FONT_SIZE, fontweight="bold")
        ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
        ax.set_ylabel("SOC (%)", fontsize=FONT_SIZE)
        ax.grid(True, alpha=GRID_ALPHA)
        add_caption(ax, "Rate of energy depletion — crucial for BMS algorithm validation.")

        # (0,1) — Temperature vs Time
        ax = axes2[0, 1]
        ax.plot(df["Time"], df["Temperature"], color=color_cycle[3], linewidth=LINE_WIDTH)
        ax.set_title("4. Thermal Response", fontsize=TITLE_FONT_SIZE, fontweight="bold")
        ax.set_xlabel("Time (s)", fontsize=FONT_SIZE)
        ax.set_ylabel("Temperature (°C)", fontsize=FONT_SIZE)
        ax.grid(True, alpha=GRID_ALPHA)
        add_caption(ax, "Heat generation dictates EV cooling system requirements.")

        # (1,0) — Voltage vs SOC
        ax = axes2[1, 0]
        ax.plot(df["SOC"] * 100, df["Voltage_Filtered"], color=color_cycle[4], linewidth=LINE_WIDTH)
        ax.set_title("5. Characteristic Curve", fontsize=TITLE_FONT_SIZE, fontweight="bold")
        ax.set_xlabel("SOC (%)", fontsize=FONT_SIZE)
        ax.set_ylabel("Voltage (V)", fontsize=FONT_SIZE)
        ax.grid(True, alpha=GRID_ALPHA)
        add_caption(
            ax, "The 'fingerprint' of the battery chemistry. The 'knee' shows usable energy limits."
        )

        # (1,1) — Empty spacer
        axes2[1, 1].axis("off")

        # (2,0) + (2,1) — Data Processing Notes (merged into single axes)
        ax_notes = axes2[2, 0]
        ax_notes2 = axes2[2, 1]
        ax_notes.axis("off")
        ax_notes2.axis("off")

        log_section = metadata.get("Data_Processing_Log", {})
        validation_notes = log_section.get("Validation", ["No validation notes"])
        filter_note = log_section.get("Filter", "N/A")
        soc_note = log_section.get("SOC_Method", "N/A")
        norm_note = log_section.get("Normalization", "N/A")

        notes_lines = [
            "Data Processing Notes",
            "",
            "Validation:",
        ]
        for note in validation_notes:
            notes_lines.append(f"  • {note}")
        notes_lines.extend([
            "",
            f"Filtering: {filter_note}",
            f"Normalization: {norm_note}",
            f"SOC Method: {soc_note}",
        ])

        notes_text = "\n".join(notes_lines)

        # Use the left bottom cell for the notes text box
        ax_notes.text(
            0.5,
            0.5,
            notes_text,
            transform=ax_notes.transAxes,
            ha="center",
            va="center",
            fontsize=FONT_SIZE,
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="whitesmoke", edgecolor="lightgray"),
        )

        fig2.subplots_adjust(left=0.08, right=0.95, top=0.90, bottom=0.08, hspace=0.45, wspace=0.35)
        pdf.savefig(fig2)
        plt.close(fig2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        description="Battery data report generator — list and generate PDF reports from S3."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List available datasets from S3")

    gen_parser = subparsers.add_parser(
        "generate", help="Download and generate a PDF report for a dataset"
    )
    gen_parser.add_argument(
        "dataset_name",
        help="Name of the dataset to report on (e.g. US06_25degC_2017-03-20)",
    )
    gen_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files from S3 even if cached locally",
    )

    return parser


def cmd_list(s3_client: boto3.client) -> None:
    """Handle the ``list`` subcommand."""
    datasets = list_datasets(s3_client)
    if not datasets:
        print("No datasets found in S3.")
        return

    print("Available datasets:")
    for i, name in enumerate(datasets, start=1):
        print(f"  {i}. {name}")


def cmd_generate(s3_client: boto3.client, dataset_name: str, force: bool) -> None:
    """Handle the ``generate`` subcommand."""
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

    if csv_path.exists() and json_path.exists() and not force:
        print("Using cached files.")
    else:
        print(f"Downloading {dataset_name} from S3...")
        download_files(s3_client, dataset_name, local_dir)

    print("Loading data...")
    df = load_and_validate_csv(csv_path)
    metadata = load_metadata(json_path)

    output_path = local_dir / "report.pdf"
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
    elif args.command == "generate":
        cmd_generate(s3_client, args.dataset_name, args.force)


if __name__ == "__main__":
    main()
