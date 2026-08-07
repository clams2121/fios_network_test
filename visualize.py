#!/usr/bin/env python3
"""Render connection-health charts from the monitor's daily JSONL logs.

Reads every ping-YYYY-MM-DD.jsonl in the log directory (optionally limited
to a date range), and produces a static PNG and/or PDF suitable for
attaching to an email: a latency-over-time plot with outage periods shaded
in red, monitoring gaps shaded in gray, and a summary of outage count and
total downtime.

Usage:
    python3 visualize.py --config config.toml
    python3 visualize.py --config config.toml --start 2026-08-01 --end 2026-08-07
    python3 visualize.py --config config.toml -o report.png -o report.pdf

matplotlib is the one third-party dependency: it is the standard way to
produce publication-quality static images from Python, and nothing in the
standard library can render charts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

# Palette (validated reference values; light surface — the chart is a static
# image destined for email, which is read on a light background).
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES_BLUE = "#2a78d6"        # categorical slot 1: the latency series
SERIES_BLUE_LIGHT = "#9ec5f4"  # same hue, light step: the max envelope
STATUS_CRITICAL = "#d03b3b"  # outages
NEUTRAL_FILL = "#f0efec"  # no-data gaps

_FILENAME_RE = re.compile(r"ping-(\d{4}-\d{2}-\d{2})\.jsonl$")

# Above this many samples the latency line is bucketed to means so month-long
# ranges stay renderable; outage spans and loss statistics always come from
# the raw records.
MAX_RAW_POINTS = 50_000
TARGET_BUCKETS = 8_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chart connection health from netmon ping logs."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="path to config.toml (to find log_dir)")
    src.add_argument("--log-dir", help="read ping-*.jsonl from this directory")
    parser.add_argument(
        "--start", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
        help="first day to include (default: everything available)",
    )
    parser.add_argument(
        "--end", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
        help="last day to include, inclusive (default: everything available)",
    )
    parser.add_argument(
        "-o", "--output", action="append", metavar="FILE",
        help="output file; extension picks the format (.png/.pdf); may be "
        "given more than once. Default: connection-health.png",
    )
    return parser.parse_args()


def find_log_files(
    log_dir: Path, start: dt.date | None, end: dt.date | None
) -> list[Path]:
    files = []
    for path in sorted(log_dir.glob("ping-*.jsonl")):
        match = _FILENAME_RE.search(path.name)
        if not match:
            continue
        day = dt.date.fromisoformat(match.group(1))
        if start and day < start:
            continue
        if end and day > end:
            continue
        files.append(path)
    return files


def load_records(files: list[Path]) -> list[dict]:
    records = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    record["ts"] = dt.datetime.fromisoformat(record["ts"])
                except (ValueError, KeyError) as exc:
                    print(
                        f"WARNING: skipping malformed record "
                        f"{path.name}:{lineno}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                records.append(record)
    records.sort(key=lambda r: r["ts"])
    return records


def outage_events(
    records: list[dict], ping_interval: float
) -> list[dict]:
    """One entry per outage event, grouped by outage_id, in time order.

    An outage's end is one ping interval after its last failed ping (the
    moment it was first known to still be down).  Blip failures — lost
    pings that never reached the outage threshold — carry no outage_id
    and are deliberately excluded here, though they still count toward
    packet-loss statistics.
    """
    spans: dict[str, list[dt.datetime]] = {}
    order: list[str] = []
    for record in records:
        if record.get("success") is False and record.get("outage_id"):
            oid = record["outage_id"]
            if oid not in spans:
                spans[oid] = [record["ts"], record["ts"]]
                order.append(oid)
            else:
                spans[oid][1] = record["ts"]
    events = []
    for oid in order:
        start, last_fail = spans[oid]
        end = last_fail + dt.timedelta(seconds=ping_interval)
        events.append(
            {
                "outage_id": oid,
                "start": start,
                "end": end,
                "seconds": (end - start).total_seconds(),
            }
        )
    return events


def outage_spans(
    records: list[dict], ping_interval: float
) -> list[tuple[dt.datetime, dt.datetime]]:
    """(start, end) per outage event."""
    return [
        (event["start"], event["end"])
        for event in outage_events(records, ping_interval)
    ]


def monitoring_gaps(
    records: list[dict], ping_interval: float
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Periods where the monitor itself was not running (record gap much
    larger than the cadence).  Shown distinctly so absence of data is never
    mistaken for — or hides — an outage."""
    threshold = dt.timedelta(seconds=max(10 * ping_interval, 30))
    gaps = []
    for prev, cur in zip(records, records[1:]):
        if cur["ts"] - prev["ts"] > threshold:
            gaps.append((prev["ts"], cur["ts"]))
    return gaps


def bucket_latency(
    records: list[dict],
) -> tuple[list, list, list | None, str, float]:
    """(times, mean_latencies, max_latencies, note, point_spacing_s).

    Downsamples to per-bucket mean AND max when the raw series is too large
    to plot point-for-point: the mean shows typical latency, the max
    envelope preserves spikes that averaging would otherwise hide.
    """
    successes = [r for r in records if r.get("success") is True]
    if len(successes) <= MAX_RAW_POINTS:
        return (
            [r["ts"] for r in successes],
            [r["latency_ms"] for r in successes],
            None,
            "",
            1.0,
        )
    t0, t1 = successes[0]["ts"], successes[-1]["ts"]
    bucket_s = max(1, int((t1 - t0).total_seconds() / TARGET_BUCKETS))
    buckets: dict[int, list[float]] = {}
    for r in successes:
        key = int((r["ts"] - t0).total_seconds()) // bucket_s
        buckets.setdefault(key, []).append(r["latency_ms"])
    times, means, maxes = [], [], []
    for key in sorted(buckets):
        times.append(t0 + dt.timedelta(seconds=key * bucket_s + bucket_s / 2))
        vals = buckets[key]
        means.append(sum(vals) / len(vals))
        maxes.append(max(vals))
    return times, means, maxes, f"{bucket_s}s buckets", float(bucket_s)


def with_breaks(times: list, *series: list, spacing_s: float):
    """Insert NaN points wherever consecutive samples are much further
    apart than their nominal spacing, so the line breaks over outages and
    monitoring gaps instead of drawing a false bridge across them."""
    threshold = dt.timedelta(seconds=max(3 * spacing_s, 30))
    out_t: list = []
    out_s: list[list] = [[] for _ in series]
    for i, t in enumerate(times):
        if out_t and t - prev > threshold:
            out_t.append(prev + threshold / 2)
            for column in out_s:
                column.append(float("nan"))
        out_t.append(t)
        for column, values in zip(out_s, series):
            column.append(values[i])
        prev = t
    return out_t, *out_s


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def ping_interval_of(records: list[dict]) -> float:
    """Median cadence taken from the data itself, so a non-default ping
    interval still charts and summarises correctly."""
    deltas = sorted(
        (b["ts"] - a["ts"]).total_seconds()
        for a, b in zip(records, records[1:])
    )
    return deltas[len(deltas) // 2] if deltas else 1.0


def summarize(records: list[dict]) -> dict:
    """Headline connection-health statistics for a set of ping records.

    Shared by the chart and the web UI so both always report the same
    numbers.  Outage spans come from the raw records, never from the
    downsampled chart series.
    """
    ping_interval = ping_interval_of(records)
    outages = outage_events(records, ping_interval)
    total = sum(1 for r in records if r.get("success") is not None)
    failures = sum(1 for r in records if r.get("success") is False)
    downtime = sum(o["seconds"] for o in outages)
    t_min, t_max = records[0]["ts"], records[-1]["ts"]
    period_s = max((t_max - t_min).total_seconds(), 1.0)
    return {
        "ping_interval": ping_interval,
        "target": records[0].get("target", "?"),
        "first": t_min,
        "last": t_max,
        "period_seconds": period_s,
        "pings": total,
        "failures": failures,
        "loss_fraction": (failures / total) if total else 0.0,
        "outages": outages,
        "outage_count": len(outages),
        "downtime_seconds": downtime,
        "downtime_fraction": downtime / period_s,
        "longest": max(outages, key=lambda o: o["seconds"]) if outages else None,
        "monitor_errors": sum(1 for r in records if r.get("success") is None),
        "gaps": monitoring_gaps(records, ping_interval),
    }


def render(
    records: list[dict], outputs: list[Path], *, quiet: bool = False
) -> dict:
    """Draw the chart to every path in *outputs*; returns the summary."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    stats = summarize(records)
    ping_interval = stats["ping_interval"]
    spans = [(o["start"], o["end"]) for o in stats["outages"]]
    gaps = stats["gaps"]
    times, means, maxes, downsample_note, spacing = bucket_latency(records)
    monitor_errors = [r for r in records if r.get("success") is None]

    # matplotlib renders tz-aware datetimes in UTC; convert everything to
    # the log's own timezone and strip tzinfo so the axis matches the
    # timestamps as recorded.
    tz = records[-1]["ts"].tzinfo

    def local(t: dt.datetime) -> dt.datetime:
        return t.astimezone(tz).replace(tzinfo=None)

    total = stats["pings"]
    failures = stats["failures"]
    downtime = stats["downtime_seconds"]
    longest = stats["longest"]
    t_min, t_max = stats["first"], stats["last"]
    period_s = stats["period_seconds"]

    fig, ax = plt.subplots(figsize=(13, 5.5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    # Recessive grid, hairline baseline.
    ax.grid(True, axis="y", color=GRIDLINE, linewidth=0.7)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=INK_MUTED, labelsize=8.5)

    # No-data gaps first (underneath), then outages on top.
    # Zero-width protection: a span shorter than 0.15% of the x-range is
    # widened to that minimum so brief outages stay visible at month scale.
    min_width = dt.timedelta(seconds=period_s * 0.0015)
    for start, end in gaps:
        ax.axvspan(local(start), local(max(end, start + min_width)),
                   color=NEUTRAL_FILL, zorder=1)
    for start, end in spans:
        ax.axvspan(local(start), local(max(end, start + min_width)),
                   color=STATUS_CRITICAL, alpha=0.55, linewidth=0, zorder=2)

    max_line = None
    if times:
        plot_spacing = max(spacing, ping_interval)
        if maxes is not None:
            bt, bmean, bmax = with_breaks(
                times, means, maxes, spacing_s=plot_spacing
            )
            (max_line,) = ax.plot(
                [local(t) for t in bt], bmax, color=SERIES_BLUE_LIGHT,
                linewidth=0.7, zorder=3,
            )
            ax.plot([local(t) for t in bt], bmean, color=SERIES_BLUE,
                    linewidth=0.9, zorder=4)
        else:
            bt, bmean = with_breaks(times, means, spacing_s=plot_spacing)
            ax.plot([local(t) for t in bt], bmean, color=SERIES_BLUE,
                    linewidth=0.9, zorder=4)
    if monitor_errors:
        ax.plot(
            [local(r["ts"]) for r in monitor_errors],
            [0] * len(monitor_errors),
            linestyle="none", marker="x", markersize=4,
            color=INK_MUTED, zorder=5,
        )

    ax.set_ylabel("round-trip latency (ms)", color=INK_SECONDARY, fontsize=9)
    ax.set_ylim(bottom=0)
    ax.set_xlim(local(t_min), local(t_max))
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    target = records[0].get("target", "?")
    title = f"Connection health — ping {target} every {ping_interval:.0f}s"
    subtitle = (
        f"{t_min:%Y-%m-%d %H:%M} to {t_max:%Y-%m-%d %H:%M}   |   "
        f"{total:,} pings, {failures:,} lost ({failures / total:.2%})   |   "
        f"{len(spans)} outage event(s), {fmt_duration(downtime)} total "
        f"downtime ({downtime / period_s:.3%} of period)"
    )
    if longest:
        subtitle += (
            f"   |   longest: {fmt_duration(longest['seconds'])}"
            f" at {longest['start']:%Y-%m-%d %H:%M:%S}"
        )
    fig.suptitle(title, x=0.06, ha="left", fontsize=13,
                 color=INK_PRIMARY, fontweight="bold")
    ax.set_title(subtitle, loc="left", fontsize=8.5, color=INK_SECONDARY,
                 pad=12)

    mean_label = "latency"
    if downsample_note:
        mean_label = f"latency, mean of {downsample_note}"
    legend_items = [
        plt.Line2D([], [], color=SERIES_BLUE, linewidth=1.5,
                   label=mean_label),
        Patch(facecolor=STATUS_CRITICAL, alpha=0.55, label="outage"),
    ]
    if max_line is not None:
        legend_items.insert(
            1,
            plt.Line2D([], [], color=SERIES_BLUE_LIGHT, linewidth=1.5,
                       label=f"worst ping ({downsample_note} max)"),
        )
    if gaps:
        legend_items.append(
            Patch(facecolor=NEUTRAL_FILL, label="monitor not running")
        )
    if monitor_errors:
        legend_items.append(
            plt.Line2D([], [], linestyle="none", marker="x", markersize=4,
                       color=INK_MUTED,
                       label="monitor error (not an outage)")
        )
    # Below the plot so it never covers data.
    legend = ax.legend(
        handles=legend_items, loc="upper center",
        bbox_to_anchor=(0.5, -0.14), ncol=len(legend_items), fontsize=8,
        frameon=False,
    )
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)

    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    try:
        for path in outputs:
            # Write to a temporary file in the same directory and rename
            # into place, so a reader (the web server) can never serve a
            # half-written PNG.
            tmp = path.with_name(f".{path.name}.tmp{path.suffix}")
            fig.savefig(tmp, facecolor=SURFACE)
            tmp.replace(path)
            if not quiet:
                print(f"wrote {path}")
    finally:
        plt.close(fig)  # a long-lived server must not leak figures
    return stats


def print_summary(stats: dict) -> None:
    """Text summary mirroring the chart, for pasting into an email body."""
    print()
    print(f"Period:        {stats['first']:%Y-%m-%d %H:%M} .. "
          f"{stats['last']:%Y-%m-%d %H:%M}")
    print(f"Pings:         {stats['pings']:,} ({stats['failures']:,} lost, "
          f"{stats['loss_fraction']:.2%})")
    print(f"Outages:       {stats['outage_count']}")
    print(f"Downtime:      {fmt_duration(stats['downtime_seconds'])} "
          f"({stats['downtime_fraction']:.3%} of period)")
    if stats["longest"]:
        print(f"Longest:       {fmt_duration(stats['longest']['seconds'])} "
              f"starting {stats['longest']['start']:%Y-%m-%d %H:%M:%S}")
    if stats["monitor_errors"]:
        print(f"Monitor errors: {stats['monitor_errors']} "
              f"(tool problems, excluded from outage statistics)")


def render_day(log_dir: Path, day: dt.date, chart_dir: Path) -> dict | None:
    """Render one day's chart into *chart_dir*.  Returns the summary, or
    None if that day has no usable records.  Used by the web server's
    button and its 1 AM scheduler."""
    files = find_log_files(log_dir, day, day)
    if not files:
        return None
    records = load_records(files)
    if not records:
        return None
    chart_dir.mkdir(parents=True, exist_ok=True)
    return render(
        records, [chart_dir / f"connection-{day.isoformat()}.png"], quiet=True
    )


def main() -> int:
    args = parse_args()
    if args.config:
        from netmon_config import ConfigError, load_config

        try:
            config = load_config(args.config, prepare_dirs=False)
        except ConfigError as exc:
            print(f"CONFIG ERROR: {exc}", file=sys.stderr)
            return 2
        log_dir = config.log_dir
    else:
        log_dir = Path(args.log_dir)
        if not log_dir.is_dir():
            print(f"ERROR: {log_dir} is not a directory", file=sys.stderr)
            return 2

    if args.start and args.end and args.start > args.end:
        print("ERROR: --start is after --end", file=sys.stderr)
        return 2

    files = find_log_files(log_dir, args.start, args.end)
    if not files:
        print(
            f"ERROR: no ping-*.jsonl files in {log_dir} match the requested "
            f"range — nothing to chart",
            file=sys.stderr,
        )
        return 1
    records = load_records(files)
    if not records:
        print("ERROR: log files matched but contained no valid records",
              file=sys.stderr)
        return 1

    outputs = [Path(p) for p in (args.output or ["connection-health.png"])]
    for path in outputs:
        if path.suffix.lower() not in (".png", ".pdf"):
            print(f"ERROR: unsupported output format: {path} (use .png/.pdf)",
                  file=sys.stderr)
            return 2
    print_summary(render(records, outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
