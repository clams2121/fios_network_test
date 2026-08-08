#!/usr/bin/env python3
"""Local web UI for browsing connection-health evidence.

Serves a sidebar of every day that has ping logs, each linking to that
day's chart plus its outage detail (start, duration, and the raw
traceroute captures with hop ownership). A button regenerates today's
chart on demand, and a scheduler thread renders the day just ended at
the configured hour (1 AM by default).

    python3 netmon_web.py --config config.toml

Standard library only (http.server); matplotlib is used indirectly via
visualize.py for the chart rendering itself.

Security posture: the server binds ONLY to the configured
`web_bind_ip` — 127.0.0.1 keeps it on the machine, a LAN address exposes
it to your LAN. There is no authentication, so do not bind it to an
address reachable from the internet. Apart from the regenerate button
(POST) it is read-only, and every path it serves is confined to the
configured log/chart/traceroute directories.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import signal
import socket
import sys
import threading
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from netmon_config import Config, ConfigError, load_config
from visualize import (
    fmt_duration,
    find_log_files,
    load_records,
    render_day,
    summarize,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOG_NAME_RE = re.compile(r"ping-(\d{4}-\d{2}-\d{2})\.jsonl$")

STYLE = """
:root {
  color-scheme: light dark;
  --surface: #fcfcfb; --panel: #f9f9f7; --ink: #0b0b0b;
  --ink-2: #52514e; --muted: #898781; --line: #e1e0d9;
  --accent: #2a78d6; --critical: #d03b3b; --good: #0ca30c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #1a1a19; --panel: #0d0d0d; --ink: #ffffff;
    --ink-2: #c3c2b7; --muted: #898781; --line: #2c2c2a;
    --accent: #3987e5; --critical: #d03b3b; --good: #0ca30c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--panel); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  display: flex; min-height: 100vh;
}
a { color: var(--accent); }
nav {
  width: 250px; flex: 0 0 250px; background: var(--surface);
  border-right: 1px solid var(--line); padding: 20px 0;
  overflow-y: auto; max-height: 100vh; position: sticky; top: 0;
}
nav h1 { font-size: 15px; margin: 0 20px 4px; }
nav .sub { font-size: 12px; color: var(--muted); margin: 0 20px 16px; }
nav a.day {
  display: block; padding: 7px 20px; text-decoration: none;
  color: var(--ink-2); border-left: 3px solid transparent;
  font-variant-numeric: tabular-nums;
}
nav a.day:hover { background: var(--panel); }
nav a.day.active {
  border-left-color: var(--accent); color: var(--ink);
  background: var(--panel); font-weight: 600;
}
nav a.day .badge {
  float: right; font-size: 11px; padding: 1px 7px; border-radius: 9px;
  background: var(--critical); color: #fff; font-weight: 600;
}
nav a.day .badge.clear { background: transparent; color: var(--muted); }
main { flex: 1; padding: 26px 34px; max-width: 1200px; }
h2 { margin: 0 0 4px; font-size: 21px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.stats { display: flex; flex-wrap: wrap; gap: 26px; margin: 0 0 22px; }
.stat .v {
  font-size: 25px; font-weight: 600; font-variant-numeric: tabular-nums;
}
.stat .v.bad { color: var(--critical); }
.stat .v.ok { color: var(--good); }
.stat .k {
  font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .05em;
}
img.chart {
  max-width: 100%; height: auto; border: 1px solid var(--line);
  border-radius: 6px; background: #fcfcfb; display: block;
}
form { display: inline; }
button {
  font: inherit; font-weight: 600; padding: 8px 15px; cursor: pointer;
  color: #fff; background: var(--accent); border: 0; border-radius: 6px;
}
button:hover { filter: brightness(1.08); }
button:disabled { opacity: .6; cursor: progress; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
th, td {
  text-align: left; padding: 7px 12px 7px 0;
  border-bottom: 1px solid var(--line); font-size: 14px;
  vertical-align: top;
}
th { font-size: 11px; text-transform: uppercase; color: var(--muted); }
td.num { font-variant-numeric: tabular-nums; }
.note {
  background: var(--surface); border: 1px solid var(--line);
  border-left: 3px solid var(--accent); border-radius: 5px;
  padding: 11px 15px; margin: 0 0 20px; font-size: 14px;
}
.note.err { border-left-color: var(--critical); }
.empty { color: var(--muted); font-style: italic; }
pre {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; padding: 13px; overflow-x: auto; font-size: 12.5px;
  line-height: 1.45;
}
details { margin-bottom: 9px; }
summary { cursor: pointer; padding: 5px 0; color: var(--ink-2); }
footer { margin-top: 34px; color: var(--muted); font-size: 12px; }
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


class Site:
    """Reads the evidence directories and renders the pages.

    Kept separate from the HTTP plumbing so it is testable without a
    socket.
    """

    def __init__(self, config: Config):
        self.config = config
        self._render_lock = threading.Lock()

    # -- data ------------------------------------------------------------
    def available_days(self) -> list[dt.date]:
        days = []
        for path in self.config.log_dir.glob("ping-*.jsonl"):
            match = _LOG_NAME_RE.search(path.name)
            if match:
                try:
                    days.append(dt.date.fromisoformat(match.group(1)))
                except ValueError:
                    continue
        return sorted(days, reverse=True)

    def day_stats(self, day: dt.date) -> dict | None:
        files = find_log_files(self.config.log_dir, day, day)
        if not files:
            return None
        records = load_records(files)
        return summarize(records) if records else None

    def chart_path(self, day: dt.date) -> Path:
        return self.config.chart_dir / f"connection-{day.isoformat()}.png"

    def generate(self, day: dt.date) -> tuple[bool, str]:
        """Render one day's chart. Serialised so concurrent clicks (or a
        click racing the scheduler) cannot interleave matplotlib calls."""
        with self._render_lock:
            try:
                stats = render_day(
                    self.config.log_dir, day, self.config.chart_dir
                )
            except ImportError as exc:
                # Almost always a numpy/matplotlib ABI mismatch, which is
                # a broken Python environment rather than anything wrong
                # with the evidence. Say so, and say how to fix it.
                traceback.print_exc()
                return False, (
                    f"Chart generation failed to import its plotting "
                    f"dependencies ({exc}). This is a problem with the "
                    f"Python environment, not with your connection data — "
                    f"ping logging is unaffected. Rebuild the virtualenv "
                    f"on the monitoring machine with "
                    f"'./deploy.sh --rebuild-venv', then restart "
                    f"netmon-web."
                )
            except Exception as exc:
                traceback.print_exc()
                return False, f"Chart generation failed: {exc!r}"
        if stats is None:
            return False, f"No ping data recorded for {day.isoformat()} yet."
        return True, (
            f"Chart for {day.isoformat()} regenerated: {stats['pings']:,} "
            f"pings, {stats['outage_count']} outage(s), "
            f"{fmt_duration(stats['downtime_seconds'])} downtime."
        )

    def ownership(self) -> dict[str, dict]:
        path = self.config.ip_ownership_file
        owners: dict[str, dict] = {}
        if not path.exists():
            return owners
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    owners[record["ip"]] = record
                except (ValueError, KeyError):
                    continue
        return owners

    def owner_label(self, record: dict | None) -> str:
        if not record:
            return "not yet identified"
        if record.get("special"):
            return record["special"]
        rdap = record.get("rdap") or {}
        return (
            rdap.get("org")
            or rdap.get("name")
            or record.get("ptr")
            or record.get("error")
            or "unknown"
        )

    def event_dir(self, outage_id: str) -> Path:
        return self.config.traceroute_dir / f"traceroute-{outage_id}"

    # -- pages -----------------------------------------------------------
    def _sidebar(self, days: list[dt.date], active: dt.date | None) -> str:
        rows = []
        for day in days:
            stats = self.day_stats(day)
            count = stats["outage_count"] if stats else 0
            badge = (
                f'<span class="badge">{count}</span>'
                if count
                else '<span class="badge clear">—</span>'
            )
            cls = "day active" if day == active else "day"
            rows.append(
                f'<a class="{cls}" href="/day/{day.isoformat()}">'
                f"{day.isoformat()}{badge}</a>"
            )
        if not rows:
            rows.append('<p class="empty" style="padding:0 20px">'
                        "No ping logs yet.</p>")
        return (
            "<nav><h1>Connection health</h1>"
            f'<p class="sub">ping {esc(self.config.ping_target_ip)}</p>'
            + "".join(rows)
            + "</nav>"
        )

    def page(self, title: str, days, active, body: str) -> bytes:
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>"
            + self._sidebar(days, active)
            + f"<main>{body}</main></body></html>"
        ).encode("utf-8")

    def _stat_block(self, stats: dict) -> str:
        loss_cls = "bad" if stats["failures"] else "ok"
        out_cls = "bad" if stats["outage_count"] else "ok"
        cells = [
            ("Pings", f"{stats['pings']:,}", ""),
            ("Lost", f"{stats['failures']:,}", loss_cls),
            ("Loss rate", f"{stats['loss_fraction']:.2%}", loss_cls),
            ("Outages", str(stats["outage_count"]), out_cls),
            ("Downtime", fmt_duration(stats["downtime_seconds"]), out_cls),
        ]
        if stats["longest"]:
            cells.append(
                ("Longest", fmt_duration(stats["longest"]["seconds"]), "bad")
            )
        if stats["monitor_errors"]:
            cells.append(("Monitor errors", str(stats["monitor_errors"]), ""))
        return '<div class="stats">' + "".join(
            f'<div class="stat"><div class="v {cls}">{esc(value)}</div>'
            f'<div class="k">{esc(key)}</div></div>'
            for key, value, cls in cells
        ) + "</div>"

    def _outage_table(self, day: dt.date, stats: dict) -> str:
        if not stats["outages"]:
            return ('<p class="empty">No outage events recorded on this '
                    "day.</p>")
        owners = self.ownership()
        blocks = []
        for event in stats["outages"]:
            files = sorted(self.event_dir(event["outage_id"]).glob("*.txt"))
            hops: dict[str, None] = {}
            for path in files:
                for ip in _ips_in(path):
                    hops.setdefault(ip, None)
                if len(hops) > 40:
                    break
            hop_rows = "".join(
                f"<tr><td class='num'>{esc(ip)}</td>"
                f"<td>{esc((owners.get(ip) or {}).get('ptr') or '—')}</td>"
                f"<td>{esc(self.owner_label(owners.get(ip)))}</td></tr>"
                for ip in hops
            )
            capture_links = "".join(
                f"<li><a href='/capture/{esc(event['outage_id'])}/"
                f"{esc(path.name)}'>{esc(path.name)}</a></li>"
                for path in files
            ) or "<li class='empty'>no captures on disk</li>"
            blocks.append(
                "<details><summary><strong>"
                f"{event['start']:%H:%M:%S}</strong> — "
                f"{esc(fmt_duration(event['seconds']))} "
                f"({len(files)} capture(s))</summary>"
                "<p class='meta'>Event "
                f"<code>{esc(event['outage_id'])}</code>, "
                f"{event['start']:%Y-%m-%d %H:%M:%S} to "
                f"{event['end']:%H:%M:%S}</p>"
                + (
                    "<table><tr><th>Hop IP</th><th>Reverse DNS</th>"
                    f"<th>Owner</th></tr>{hop_rows}</table>"
                    if hop_rows
                    else ""
                )
                + f"<p class='meta'>Raw captures:</p><ul>{capture_links}</ul>"
                "</details>"
            )
        return "".join(blocks)

    def index(self, notice: str = "", error: bool = False) -> bytes:
        days = self.available_days()
        if days:
            return self.day_page(days[0], notice=notice, error=error)
        body = (
            "<h2>Connection health</h2>"
            "<p class='meta'>No ping logs found in "
            f"{esc(self.config.log_dir)} yet.</p>"
            "<div class='note'>Start the monitor "
            "(<code>sudo systemctl start netmon</code>) and this page will "
            "fill in as data is recorded.</div>"
        )
        return self.page("Connection health", days, None, body)

    def day_page(
        self, day: dt.date, notice: str = "", error: bool = False
    ) -> bytes:
        days = self.available_days()
        stats = self.day_stats(day)
        today = dt.date.today()
        chart = self.chart_path(day)

        parts = [f"<h2>{day.isoformat()}</h2>"]
        parts.append(
            f"<p class='meta'>ping {esc(self.config.ping_target_ip)}"
            + (f" · {stats['first']:%H:%M:%S} to {stats['last']:%H:%M:%S}"
               if stats else "")
            + (" · today (still recording)" if day == today else "")
            + "</p>"
        )
        if notice:
            parts.append(
                f"<div class='note{' err' if error else ''}'>{esc(notice)}"
                "</div>"
            )
        # The button always regenerates TODAY's chart (today is the only
        # day still changing); completed days are rendered by the
        # scheduler and backfilled at startup.  The hidden field is
        # today's date, never the day being viewed, so the label and the
        # action can't disagree.
        parts.append(
            f"<form method='post' action='/generate'>"
            f"<input type='hidden' name='day' value='{today.isoformat()}'>"
            "<button type='submit' "
            "onclick=\"this.disabled=true;this.textContent='Generating…';"
            "this.form.submit()\">Generate today&rsquo;s chart</button>"
            "</form>"
            "<p class='meta' style='margin-top:8px'>Today&rsquo;s chart is "
            "still accumulating data, so regenerate it whenever you want it "
            "current. Completed days are rendered automatically at "
            f"{self.config.web_daily_chart_hour:02d}:00 (and any day found "
            "without a chart is backfilled when this server starts).</p>"
        )
        if stats:
            parts.append(self._stat_block(stats))
        if chart.exists():
            stamp = int(chart.stat().st_mtime)
            parts.append(
                f"<img class='chart' src='/chart/{day.isoformat()}.png"
                f"?v={stamp}' alt='Connection health for "
                f"{day.isoformat()}'>"
                f"<p class='meta'>Chart generated "
                f"{dt.datetime.fromtimestamp(stamp):%Y-%m-%d %H:%M:%S} · "
                f"<a href='/chart/{day.isoformat()}.png' download>download "
                "PNG</a></p>"
            )
        else:
            parts.append(
                "<div class='note'>No chart generated for this day yet — "
                "use the button above.</div>"
            )
        if stats:
            parts.append("<h3>Outage events</h3>")
            parts.append(self._outage_table(day, stats))
        else:
            parts.append("<p class='empty'>No ping records for this day.</p>")
        parts.append(
            "<footer>Evidence directories: "
            f"logs <code>{esc(self.config.log_dir)}</code> · charts "
            f"<code>{esc(self.config.chart_dir)}</code> · traceroutes "
            f"<code>{esc(self.config.traceroute_dir)}</code></footer>"
        )
        return self.page(
            f"{day.isoformat()} — connection health", days, day,
            "".join(parts),
        )


def _ips_in(path: Path) -> list[str]:
    from netmon_actions import extract_ips

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    body = "\n".join(
        line for line in text.splitlines() if not line.startswith("#")
    )
    return sorted(extract_ips(body))


class Handler(BaseHTTPRequestHandler):
    server_version = "netmon-web"
    site: Site  # set on the server instance

    def log_message(self, fmt, *args):  # quieter, timestamped console
        sys.stderr.write(
            f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} web "
            f"{self.address_string()} {fmt % args}\n"
        )

    # -- helpers ---------------------------------------------------------
    def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Charts are regenerated in place; never let a browser cache them.
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(
            f"<!doctype html><meta charset='utf-8'>"
            f"<style>{STYLE}</style><body><main><h2>{status.value} "
            f"{esc(status.phrase)}</h2><p class='meta'>{esc(message)}</p>"
            "<p><a href='/'>← back</a></p></main></body>".encode("utf-8"),
            "text/html; charset=utf-8",
            status,
        )

    def _parse_day(self, text: str) -> dt.date | None:
        # Strict pattern first: keeps anything path-shaped out of the
        # filename builders below.
        if not _DATE_RE.match(text):
            return None
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            return None

    def _safe_child(self, base: Path, *parts: str) -> Path | None:
        """Resolve base/parts and confirm it stays inside base."""
        if any(("/" in p or "\\" in p or p in ("", ".", "..")) for p in parts):
            return None
        try:
            candidate = base.joinpath(*parts).resolve()
            base_resolved = base.resolve()
        except OSError:
            return None
        if candidate != base_resolved and base_resolved not in candidate.parents:
            return None
        return candidate

    # -- routes ----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        site = self.server.site
        try:
            if path == "/":
                notice = (query.get("msg") or [""])[0]
                error = (query.get("err") or [""])[0] == "1"
                self._send(
                    site.index(notice, error), "text/html; charset=utf-8"
                )
                return
            if path.startswith("/day/"):
                day = self._parse_day(path[len("/day/"):])
                if not day:
                    return self._error(HTTPStatus.NOT_FOUND, "Bad date.")
                notice = (query.get("msg") or [""])[0]
                error = (query.get("err") or [""])[0] == "1"
                self._send(
                    site.day_page(day, notice, error),
                    "text/html; charset=utf-8",
                )
                return
            if path.startswith("/chart/") and path.endswith(".png"):
                day = self._parse_day(path[len("/chart/"):-len(".png")])
                if not day:
                    return self._error(HTTPStatus.NOT_FOUND, "Bad date.")
                chart = site.chart_path(day)
                if not chart.is_file():
                    return self._error(
                        HTTPStatus.NOT_FOUND, "No chart for that day yet."
                    )
                self._send(chart.read_bytes(), "image/png")
                return
            if path.startswith("/capture/"):
                parts = path[len("/capture/"):].split("/")
                if len(parts) != 2:
                    return self._error(HTTPStatus.NOT_FOUND, "Bad capture.")
                outage_id, name = parts
                target = self._safe_child(
                    site.config.traceroute_dir,
                    f"traceroute-{outage_id}",
                    name,
                )
                if (
                    target is None
                    or not target.is_file()
                    or target.suffix != ".txt"
                ):
                    return self._error(
                        HTTPStatus.NOT_FOUND, "No such capture."
                    )
                self._send(target.read_bytes(), "text/plain; charset=utf-8")
                return
            self._error(HTTPStatus.NOT_FOUND, "Unknown path.")
        except Exception:
            traceback.print_exc()
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "The web UI hit an internal error; see the service log. "
                "This does not affect the monitor.",
            )

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/generate":
            return self._error(HTTPStatus.NOT_FOUND, "Unknown path.")
        site = self.server.site
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 4096:
            return self._error(HTTPStatus.BAD_REQUEST, "Request too large.")
        body = self.rfile.read(length).decode("utf-8", "replace")
        fields = urllib.parse.parse_qs(body)
        # Only today is ever generated on demand; anything else is ignored
        # rather than trusted from the request.
        submitted = self._parse_day((fields.get("day") or [""])[0])
        day = dt.date.today()
        if submitted and submitted != day:
            print(
                f"netmon-web: generate request named {submitted}; only "
                f"today ({day}) is generated on demand",
                flush=True,
            )
        try:
            ok, message = site.generate(day)
        except Exception:
            traceback.print_exc()
            ok, message = False, "Chart generation raised an internal error."
        target = (
            f"/day/{day.isoformat()}?msg="
            + urllib.parse.quote(message)
            + ("" if ok else "&err=1")
        )
        # POST/redirect/GET: a browser refresh must not re-run generation.
        self._send(b"", "text/plain", HTTPStatus.SEE_OTHER,
                   {"Location": target})


class DailyChartScheduler(threading.Thread):
    """Renders the day that just ended, once per day at the configured
    hour. Missed runs (server was down) are caught up on start."""

    def __init__(self, site: Site, hour: int):
        super().__init__(name="daily-charts", daemon=True)
        self.site = site
        self.hour = hour
        self.stop_event = threading.Event()

    def _next_run(self, now: dt.datetime) -> dt.datetime:
        run = now.replace(
            hour=self.hour, minute=0, second=0, microsecond=0
        )
        if run <= now:
            run += dt.timedelta(days=1)
        return run

    def catch_up(self) -> None:
        """Render any completed day that has logs but no chart yet."""
        today = dt.date.today()
        for day in self.site.available_days():
            if day >= today or self.site.chart_path(day).exists():
                continue
            ok, message = self.site.generate(day)
            print(f"netmon-web: backfill {day}: {message}", flush=True)

    def run(self) -> None:
        try:
            self.catch_up()
            while not self.stop_event.is_set():
                now = dt.datetime.now()
                run_at = self._next_run(now)
                print(
                    f"netmon-web: next daily chart at {run_at:%Y-%m-%d %H:%M}",
                    flush=True,
                )
                if self.stop_event.wait((run_at - now).total_seconds()):
                    return
                day = dt.date.today() - dt.timedelta(days=1)
                ok, message = self.site.generate(day)
                print(f"netmon-web: daily chart {day}: {message}", flush=True)
        except Exception:
            # A dead scheduler must be visible, not silent.
            traceback.print_exc()
            print(
                "netmon-web: ERROR the daily chart scheduler stopped; "
                "charts must be generated with the button until the "
                "service is restarted",
                file=sys.stderr, flush=True,
            )


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, site: Site):
        self.site = site
        # IPv6 literals need AF_INET6; the config accepts either family.
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local web UI for connection-health evidence."
    )
    parser.add_argument("--config", required=True, help="path to config.toml")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    site = Site(config)
    try:
        server = Server(
            (config.web_bind_ip, config.web_port), Handler, site
        )
    except OSError as exc:
        print(
            f"ERROR: cannot bind {config.web_bind_ip}:{config.web_port} — "
            f"{exc}. Check web_bind_ip is an address on this machine and "
            f"web_port is free.",
            file=sys.stderr,
        )
        return 1

    scheduler = DailyChartScheduler(site, config.web_daily_chart_hour)
    scheduler.start()

    shown = (
        f"[{config.web_bind_ip}]"
        if ":" in config.web_bind_ip
        else config.web_bind_ip
    )
    print(
        f"netmon-web: serving http://{shown}:{config.web_port}/ "
        f"(charts in {config.chart_dir}; daily render at "
        f"{config.web_daily_chart_hour:02d}:00)",
        flush=True,
    )
    if config.web_bind_ip not in ("127.0.0.1", "::1"):
        print(
            f"netmon-web: NOTE bound to {config.web_bind_ip}, reachable by "
            f"other hosts. There is no authentication — keep this off any "
            f"internet-facing interface.",
            flush=True,
        )

    def handle_signal(signum, _frame):
        print(
            f"netmon-web: received {signal.Signals(signum).name}, "
            f"shutting down...",
            flush=True,
        )
        scheduler.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    print("netmon-web: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
