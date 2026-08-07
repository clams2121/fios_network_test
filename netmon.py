#!/usr/bin/env python3
"""Network connectivity monitor.

Pings a target once per second on a fixed cadence and records every result
as JSONL.  On ping failure it opens an outage event and runs the configured
on-failure actions (verbose traceroute at minimum) every
`traceroute_interval_seconds` until connectivity returns.  On clean shutdown
it resolves ownership (PTR + RDAP) of every IP seen in any traceroute.

Run:  python3 netmon.py --config config.toml

Design notes
------------
* "Network is down" and "the monitor has a problem" are strictly separated:
  a ping that gets no reply is a network failure; a ping binary that cannot
  run, produces unparseable output, or an action that crashes is a monitor
  error.  Monitor errors are written to monitor-errors.log and stderr, are
  recorded in the ping log as {"monitor_error": true} records (success:
  null), and never open or extend an outage event.  Ten consecutive monitor
  errors abort the run loudly rather than logging garbage forever.
* The cadence is fixed-rate: the next ping is scheduled at start + N
  seconds on the monotonic clock, so ping duration does not make the
  schedule drift.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from netmon_actions import Action, ActionError, build_actions
from netmon_config import Config, ConfigError, load_config
from netmon_ownership import identify_ips

MAX_CONSECUTIVE_MONITOR_ERRORS = 10
_LATENCY_RE = re.compile(r"time=([0-9.]+) ms")


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def say(message: str) -> None:
    """Console status chatter.  A dead stdout (broken pipe when a parent
    process goes away) must never take the monitor down with it — the
    evidence lives in files, not on the console."""
    try:
        print(message, flush=True)
    except OSError:
        pass


class MonitorErrorLog:
    """Loud, timestamped log for problems with the monitor itself."""

    def __init__(self, log_dir: Path):
        self.path = log_dir / "monitor-errors.log"
        self._lock = threading.Lock()

    def report(self, message: str) -> None:
        line = f"{now_local().isoformat()} {message}"
        with self._lock:
            try:
                print(f"MONITOR ERROR: {message}", file=sys.stderr, flush=True)
            except OSError:
                pass  # dead stderr must not stop the file record below
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class DailyJsonlWriter:
    """Appends JSON lines to ping-YYYY-MM-DD.jsonl, rolling at local
    midnight.  Every record is flushed immediately so a crash or power cut
    loses at most the line being written."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._fh = None
        self._date = None

    def write(self, record: dict) -> None:
        today = now_local().date()
        if self._fh is None or today != self._date:
            if self._fh is not None:
                self._fh.close()
            path = self.log_dir / f"ping-{today.isoformat()}.jsonl"
            self._fh = open(path, "a", encoding="utf-8")
            self._date = today
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class PingOutcome:
    __slots__ = ("success", "latency_ms", "error")

    def __init__(self, success, latency_ms=None, error=None):
        self.success = success  # True / False / None (None = monitor error)
        self.latency_ms = latency_ms
        self.error = error


def run_ping(target: str, timeout_s: float) -> PingOutcome:
    """One ping.  iputils exit codes: 0 = reply, 1 = no reply (network
    failure), 2 = tool/usage error (monitor problem)."""
    argv = ["ping", "-n", "-c", "1", "-W", str(timeout_s), target]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s + 5
        )
    except subprocess.TimeoutExpired:
        return PingOutcome(
            None,
            error=f"ping did not exit within {timeout_s + 5}s "
            f"(the -W timeout should have ended it; tool problem)",
        )
    except OSError as exc:
        return PingOutcome(None, error=f"could not execute ping: {exc}")

    if proc.returncode == 0:
        match = _LATENCY_RE.search(proc.stdout)
        if not match:
            return PingOutcome(
                None,
                error="ping exited 0 but no 'time=... ms' found in output: "
                + proc.stdout.strip()[:200],
            )
        return PingOutcome(True, latency_ms=float(match.group(1)))
    if proc.returncode == 1:
        return PingOutcome(False)
    return PingOutcome(
        None,
        error=f"ping exited {proc.returncode}: "
        + (proc.stderr.strip() or proc.stdout.strip())[:200],
    )


class OutageWorker(threading.Thread):
    """Runs all on-failure actions once at outage start and then every
    `traceroute_interval_seconds` until the outage clears (or the monitor
    shuts down).  One worker per outage event."""

    def __init__(
        self,
        event_id: str,
        event_dir: Path,
        actions: list[Action],
        interval_s: float,
        seen_ips: set[str],
        ip_lock: threading.Lock,
        error_log: MonitorErrorLog,
    ):
        # daemon=True: if the main thread ever dies unexpectedly, the worker
        # must not keep the process alive and traceroute forever.  The clean
        # shutdown path still joins the worker explicitly.
        super().__init__(name=f"outage-{event_id}", daemon=True)
        self.event_dir = event_dir
        self.actions = actions
        self.interval_s = interval_s
        self.seen_ips = seen_ips
        self.ip_lock = ip_lock
        self.error_log = error_log
        self.cleared = threading.Event()

    def run(self) -> None:
        try:
            self.event_dir.mkdir(parents=True, exist_ok=True)
            while True:
                cycle_start = time.monotonic()
                for action in self.actions:
                    if self.cleared.is_set():
                        break
                    try:
                        ips = action.run(self.event_dir)
                    except ActionError as exc:
                        self.error_log.report(str(exc))
                        continue
                    with self.ip_lock:
                        self.seen_ips |= ips
                elapsed = time.monotonic() - cycle_start
                if self.cleared.wait(max(0.0, self.interval_s - elapsed)):
                    return
        except Exception as exc:  # never a silently wedged worker
            self.error_log.report(
                f"outage worker for {self.event_dir.name} crashed: {exc!r}"
            )


class Monitor:
    def __init__(self, config: Config):
        self.config = config
        self.stop_event = threading.Event()
        self.writer = DailyJsonlWriter(config.log_dir)
        self.error_log = MonitorErrorLog(config.log_dir)
        self.actions = build_actions(config)
        self.seen_ips: set[str] = set()
        self.ip_lock = threading.Lock()
        self.worker: OutageWorker | None = None
        self.outage_id: str | None = None
        self.outage_count = 0
        self.consecutive_monitor_errors = 0
        # Failure records buffered until outage_open_after_failures
        # consecutive failures confirm a real outage (see _record).
        self.pending_failures: list[dict] = []
        self.pending_start: dt.datetime | None = None
        self._shutdown_done = False

    # -- outage event lifecycle -------------------------------------------
    def _open_outage(self, started: dt.datetime) -> None:
        self.outage_id = started.strftime("%Y-%m-%dT%H-%M-%S")
        self.outage_count += 1
        event_dir = self.config.traceroute_dir / f"traceroute-{self.outage_id}"
        say(
            f"{started.isoformat()} OUTAGE START (event {self.outage_id}); "
            f"running on-failure actions every "
            f"{self.config.traceroute_interval_seconds}s"
        )
        self.worker = OutageWorker(
            self.outage_id,
            event_dir,
            self.actions,
            self.config.traceroute_interval_seconds,
            self.seen_ips,
            self.ip_lock,
            self.error_log,
        )
        self.worker.start()

    def _close_outage(self) -> None:
        say(f"{now_local().isoformat()} OUTAGE END (event {self.outage_id})")
        self.worker.cleared.set()
        self.worker.join()
        self.worker = None
        self.outage_id = None

    # -- main loop --------------------------------------------------------
    def run(self) -> int:
        config = self.config
        say(
            f"netmon: pinging {config.ping_target_ip} every "
            f"{config.ping_interval_seconds}s "
            f"(timeout {config.ping_timeout_seconds}s); on failure: "
            + ", ".join(a.name for a in self.actions)
            + f" -> {config.traceroute_dir}"
        )
        exit_code = 0
        # try/finally: no matter how the loop ends — signal, abort, or an
        # unexpected exception — the logs get flushed, the worker gets
        # stopped, and the ownership lookups run.
        try:
            next_tick = time.monotonic()
            while not self.stop_event.is_set():
                timestamp = now_local()
                outcome = run_ping(
                    config.ping_target_ip, config.ping_timeout_seconds
                )
                if outcome.success is None and self.stop_event.is_set():
                    # The shutdown signal reached the ping child too
                    # (systemd and shells signal the whole process group).
                    # An interrupted probe is not evidence of anything.
                    break
                self._record(timestamp, outcome)
                if self.consecutive_monitor_errors >= (
                    MAX_CONSECUTIVE_MONITOR_ERRORS
                ):
                    self.error_log.report(
                        f"aborting: {self.consecutive_monitor_errors} "
                        f"consecutive monitor errors — this is a problem "
                        f"with the monitoring host, not evidence of a "
                        f"network outage"
                    )
                    exit_code = 1
                    break
                next_tick += config.ping_interval_seconds
                delay = next_tick - time.monotonic()
                if delay > 0:
                    self.stop_event.wait(delay)
                else:
                    # We fell behind (e.g. suspend/resume). Re-anchor
                    # instead of firing a burst of catch-up pings.
                    next_tick = time.monotonic()
        finally:
            self.shutdown()
        return exit_code

    def _flush_pending(self, outage_id: str | None) -> None:
        for pending in self.pending_failures:
            pending["outage_id"] = outage_id
            self.writer.write(pending)
        self.pending_failures = []
        self.pending_start = None

    def _record(self, timestamp: dt.datetime, outcome: PingOutcome) -> None:
        record = {
            "ts": timestamp.isoformat(),
            "target": self.config.ping_target_ip,
            "success": outcome.success,
            "latency_ms": outcome.latency_ms,
            "outage_id": None,
        }
        if outcome.success is None:
            # Monitor problem: log loudly, do not touch outage state.
            record["monitor_error"] = True
            record["error"] = outcome.error
            record["outage_id"] = self.outage_id
            self.consecutive_monitor_errors += 1
            self.error_log.report(outcome.error)
            self.writer.write(record)
            return
        self.consecutive_monitor_errors = 0
        if outcome.success:
            if self.outage_id is not None:
                self._close_outage()
            elif self.pending_failures:
                # Recovered before the threshold: a blip, not an outage.
                # The lost pings still land in the log (loss statistics),
                # but with no outage_id and no traceroutes.
                say(
                    f"{timestamp.isoformat()} blip: "
                    f"{len(self.pending_failures)} lost ping(s), recovered "
                    f"before outage threshold "
                    f"({self.config.outage_open_after_failures})"
                )
                self._flush_pending(None)
            self.writer.write(record)
            return
        # Ping failure.  An already-open outage extends directly; otherwise
        # buffer the record (at most threshold-1 entries, i.e. a couple of
        # seconds) until enough consecutive failures confirm a real outage —
        # then all buffered records join the event, which starts at the
        # FIRST failed ping.
        if self.outage_id is not None:
            record["outage_id"] = self.outage_id
            self.writer.write(record)
            return
        if self.pending_start is None:
            self.pending_start = timestamp
        self.pending_failures.append(record)
        if len(self.pending_failures) >= self.config.outage_open_after_failures:
            self._open_outage(self.pending_start)
            self._flush_pending(self.outage_id)

    # -- shutdown ---------------------------------------------------------
    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if self.worker is not None:
            say("netmon: waiting for in-flight on-failure actions...")
            self.worker.cleared.set()
            self.worker.join()
            self.worker = None
        # Failures still short of the threshold at shutdown were never an
        # outage; write them out as plain lost pings.
        self._flush_pending(None)
        self.writer.close()
        with self.ip_lock:
            ips = set(self.seen_ips)
        if self.outage_count:
            say(
                f"netmon: {self.outage_count} outage event(s) this run; "
                f"{len(ips)} unique IP(s) seen in action output"
            )
        if ips:
            # Post-run only: DNS/RDAP are unreachable during an outage, so
            # doing this earlier would fail and pollute the evidence.
            try:
                identify_ips(
                    ips,
                    self.config.ip_ownership_file,
                    rdap=self.config.rdap_lookups,
                    log=say,
                )
            except OSError as exc:
                self.error_log.report(f"IP ownership lookup failed: {exc}")
        say("netmon: shutdown complete")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ping-based connectivity monitor with on-failure "
        "traceroutes."
    )
    parser.add_argument("--config", required=True, help="path to config.toml")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    monitor = Monitor(config)

    def handle_signal(signum, _frame):
        say(
            f"netmon: received {signal.Signals(signum).name}, shutting down "
            f"(will run IP ownership lookups first)..."
        )
        monitor.stop_event.set()
        # A second signal falls through to the default handler and kills us.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
