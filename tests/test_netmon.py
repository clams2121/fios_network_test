"""Unit tests (standard-library unittest; no extra dependencies).

Run from the repository root:  python3 -m unittest discover -s tests -v

Console output from the code under test is captured per-test (see
QuietTestCase) so a passing run stays silent. Several tests deliberately
drive error paths that log loudly by design — an unexpected traceback in
the output of a passing run would otherwise look like a real failure.
Assert on self.captured_err() when the logging itself is the behaviour
being tested.
"""

import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netmon import Monitor, PingOutcome, _LATENCY_RE  # noqa: E402
from netmon_actions import extract_ips  # noqa: E402
from netmon_config import ActionConfig, Config, ConfigError, load_config  # noqa: E402
from netmon_ownership import _classify  # noqa: E402
from netmon_web import Handler, Site  # noqa: E402
from visualize import monitoring_gaps, outage_spans, summarize  # noqa: E402


class QuietTestCase(unittest.TestCase):
    """Captures stdout/stderr for the duration of each test.

    unittest's runner captured the real streams before any test ran, so
    its own reporting is unaffected.
    """

    def setUp(self):
        super().setUp()
        self._out, self._err = io.StringIO(), io.StringIO()
        for redirect in (
            contextlib.redirect_stdout(self._out),
            contextlib.redirect_stderr(self._err),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def captured_out(self) -> str:
        return self._out.getvalue()

    def captured_err(self) -> str:
        return self._err.getvalue()


def make_config(tmp: Path, threshold: int = 1) -> Config:
    for sub in ("logs", "logs/traceroutes"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    return Config(
        ping_target_ip="192.0.2.10",
        traceroute_target_ip="192.0.2.11",
        ping_interval_seconds=1.0,
        ping_timeout_seconds=1.0,
        traceroute_interval_seconds=0.05,
        outage_open_after_failures=threshold,
        log_dir=tmp / "logs",
        traceroute_dir=tmp / "logs/traceroutes",
        ip_ownership_file=tmp / "logs/ip-ownership.jsonl",
        chart_dir=tmp / "logs/charts",
        rdap_lookups=False,
        web_bind_ip="127.0.0.1",
        web_port=8477,
        web_daily_chart_hour=1,
        # 'true' exits instantly with no output: a harmless stand-in action.
        on_failure_actions=[
            ActionConfig(type="command", name="noop", command=["true"])
        ],
    )


class OutageLifecycleTest(QuietTestCase):
    def test_outage_opens_groups_and_closes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            monitor = Monitor(make_config(tmp))
            t = dt.datetime(2026, 8, 6, 14, 23, 7).astimezone()
            one = dt.timedelta(seconds=1)

            monitor._record(t, PingOutcome(True, latency_ms=9.5))
            monitor._record(t + one, PingOutcome(False))
            self.assertIsNotNone(monitor.outage_id)
            self.assertTrue(monitor.worker.is_alive())
            monitor._record(t + 2 * one, PingOutcome(False))
            monitor._record(t + 3 * one, PingOutcome(True, latency_ms=11.0))
            self.assertIsNone(monitor.outage_id)
            self.assertIsNone(monitor.worker)
            # Second, separate outage gets a new event.
            monitor._record(t + 4 * one, PingOutcome(False))
            second_id = monitor.outage_id
            monitor._record(t + 5 * one, PingOutcome(True, latency_ms=10.0))
            monitor.shutdown()

            records = [
                json.loads(line)
                for line in (tmp / "logs" / f"ping-{dt.date.today()}.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(len(records), 6)
            self.assertEqual(
                [r["success"] for r in records],
                [True, False, False, True, False, True],
            )
            first_ids = {r["outage_id"] for r in records[1:3]}
            self.assertEqual(len(first_ids), 1)
            self.assertNotIn(None, first_ids)
            self.assertEqual(records[4]["outage_id"], second_id)
            self.assertNotEqual(records[1]["outage_id"], second_id)
            self.assertIsNone(records[0]["outage_id"])
            self.assertIsNone(records[3]["outage_id"])
            self.assertEqual(monitor.outage_count, 2)
            # Each outage produced its own event directory.
            event_dirs = sorted((tmp / "logs/traceroutes").iterdir())
            self.assertEqual(len(event_dirs), 2)

    def test_threshold_blip_is_not_an_outage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            monitor = Monitor(make_config(tmp, threshold=3))
            t = dt.datetime(2026, 8, 6, 15, 0, 0).astimezone()
            one = dt.timedelta(seconds=1)
            # Two lost pings then recovery: below the threshold of 3.
            monitor._record(t, PingOutcome(False))
            monitor._record(t + one, PingOutcome(False))
            self.assertIsNone(monitor.outage_id)
            self.assertIsNone(monitor.worker)
            monitor._record(t + 2 * one, PingOutcome(True, latency_ms=9.0))
            monitor.shutdown()
            records = [
                json.loads(line)
                for line in (tmp / "logs" / f"ping-{dt.date.today()}.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(
                [r["success"] for r in records], [False, False, True]
            )
            self.assertEqual([r["outage_id"] for r in records], [None] * 3)
            self.assertEqual(monitor.outage_count, 0)
            self.assertEqual(list((tmp / "logs/traceroutes").iterdir()), [])

    def test_threshold_outage_starts_at_first_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            monitor = Monitor(make_config(tmp, threshold=3))
            t = dt.datetime(2026, 8, 6, 15, 0, 0).astimezone()
            one = dt.timedelta(seconds=1)
            monitor._record(t, PingOutcome(False))
            monitor._record(t + one, PingOutcome(False))
            self.assertIsNone(monitor.outage_id)
            monitor._record(t + 2 * one, PingOutcome(False))
            # Third consecutive failure confirms the outage, named for the
            # FIRST failed ping's timestamp.
            self.assertEqual(monitor.outage_id, t.strftime("%Y-%m-%dT%H-%M-%S"))
            monitor._record(t + 3 * one, PingOutcome(True, latency_ms=9.0))
            monitor.shutdown()
            records = [
                json.loads(line)
                for line in (tmp / "logs" / f"ping-{dt.date.today()}.jsonl")
                .read_text()
                .splitlines()
            ]
            # All three buffered failures were written with the event id.
            self.assertEqual(
                [r["outage_id"] is not None for r in records],
                [True, True, True, False],
            )
            self.assertEqual(monitor.outage_count, 1)

    def test_monitor_error_does_not_open_outage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            monitor = Monitor(make_config(tmp))
            t = dt.datetime(2026, 8, 6, 14, 0, 0).astimezone()
            monitor._record(t, PingOutcome(None, error="boom"))
            self.assertIsNone(monitor.outage_id)
            self.assertEqual(monitor.consecutive_monitor_errors, 1)
            monitor.shutdown()
            records = [
                json.loads(line)
                for line in (tmp / "logs" / f"ping-{dt.date.today()}.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertIsNone(records[0]["success"])
            self.assertTrue(records[0]["monitor_error"])
            self.assertIn(
                "boom", (tmp / "logs" / "monitor-errors.log").read_text()
            )
            # Monitor problems must also be loud on the console, so they
            # surface in journalctl and not just in the file.
            self.assertIn("MONITOR ERROR: boom", self.captured_err())


class ParsingTest(QuietTestCase):
    def test_latency_regex(self):
        out = "64 bytes from 8.8.8.8: icmp_seq=1 ttl=115 time=12.4 ms"
        self.assertEqual(float(_LATENCY_RE.search(out).group(1)), 12.4)

    def test_extract_ips(self):
        text = (
            "traceroute to 1.1.1.1 (1.1.1.1), 30 hops max\n"
            " 1  192.168.1.1  0.5 ms\n"
            " 2  100.64.12.1  3.2 ms\n"
            " 3  2001:db8::1  9.9 ms\n"
            " 4  999.1.1.1 not-an-ip 1.2.3\n"
        )
        self.assertEqual(
            extract_ips(text),
            {"1.1.1.1", "192.168.1.1", "100.64.12.1", "2001:db8::1"},
        )

    def test_classify(self):
        self.assertIn("RFC 1918", _classify("192.168.1.1"))
        self.assertIn("carrier-grade NAT", _classify("100.64.0.5"))
        self.assertEqual(_classify("127.0.0.1"), "loopback")
        self.assertIsNone(_classify("8.8.8.8"))


class ConfigTest(QuietTestCase):
    def _write(self, tmp: Path, text: str) -> Path:
        path = tmp / "config.toml"
        path.write_text(text)
        return path

    VALID = """
ping_target_ip = "8.8.8.8"
traceroute_target_ip = "1.1.1.1"
ping_interval_seconds = 1.0
ping_timeout_seconds = 1.0
traceroute_interval_seconds = 10.0
outage_open_after_failures = 3
web_bind_ip = "127.0.0.1"
web_port = 8477
web_daily_chart_hour = 1
chart_dir = "logs/charts"
log_dir = "logs"
traceroute_dir = "logs/traceroutes"
ip_ownership_file = "logs/ip-ownership.jsonl"
rdap_lookups = true
[[on_failure_actions]]
type = "command"
name = "noop"
command = ["true"]
"""

    def test_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = load_config(self._write(tmp, self.VALID))
            self.assertEqual(config.ping_target_ip, "8.8.8.8")
            self.assertTrue(config.log_dir.is_absolute())
            self.assertTrue(config.log_dir.is_dir())

    def test_rejects_missing_key_bad_ip_and_bad_timeout(self):
        cases = [
            self.VALID.replace('ping_target_ip = "8.8.8.8"\n', ""),
            self.VALID.replace("8.8.8.8", "not.an.ip.addr"),
            self.VALID.replace(
                "ping_timeout_seconds = 1.0", "ping_timeout_seconds = 5.0"
            ),
            self.VALID.replace('command = ["true"]', "command = []"),
        ]
        for text in cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                with self.assertRaises(ConfigError):
                    load_config(self._write(Path(tmpdir), text))


class VisualizeTest(QuietTestCase):
    def test_spans_and_gaps(self):
        t0 = dt.datetime(2026, 8, 6, 12, 0, 0).astimezone()

        def rec(offset, success, outage_id=None):
            return {
                "ts": t0 + dt.timedelta(seconds=offset),
                "success": success,
                "outage_id": outage_id,
            }

        records = [
            rec(0, True),
            rec(1, False, "E1"),
            rec(2, False, "E1"),
            rec(3, True),
            # 5-minute hole: the monitor was not running.
            rec(303, True),
            rec(304, False, "E2"),
        ]
        spans = outage_spans(records, 1.0)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0][0], records[1]["ts"])
        self.assertEqual(
            spans[0][1], records[2]["ts"] + dt.timedelta(seconds=1)
        )
        gaps = monitoring_gaps(records, 1.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0], (records[3]["ts"], records[4]["ts"]))


class WebTest(QuietTestCase):
    """The web layer, exercised without opening a socket."""

    def _seed(self, tmp: Path, day: dt.date) -> Site:
        config = make_config(tmp, threshold=3)
        tz = dt.timezone(dt.timedelta(hours=-4))
        t = dt.datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=tz)
        lines = []
        for i in range(10):
            failed = 3 <= i < 6
            lines.append(json.dumps({
                "ts": (t + dt.timedelta(seconds=i)).isoformat(),
                "target": "8.8.8.8",
                "success": not failed,
                "latency_ms": None if failed else 10.0,
                "outage_id": "EVT1" if failed else None,
            }))
        (config.log_dir / f"ping-{day.isoformat()}.jsonl").write_text(
            "\n".join(lines) + "\n"
        )
        event = config.traceroute_dir / "traceroute-EVT1"
        event.mkdir(parents=True, exist_ok=True)
        (event / "traceroute-x.txt").write_text(
            "# action: traceroute\n 1  192.168.1.1  0.5 ms\n"
            " 2  100.41.135.2  4.2 ms\n"
        )
        config.ip_ownership_file.write_text(
            json.dumps({
                "ip": "100.41.135.2",
                "ptr": "verizon-gni.net",
                "special": None,
                "rdap": {"org": "Verizon Business"},
                "error": None,
            }) + "\n"
        )
        return Site(config)

    def test_day_listing_stats_and_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            day = dt.date(2026, 8, 5)
            site = self._seed(tmp, day)
            self.assertEqual(site.available_days(), [day])
            stats = site.day_stats(day)
            self.assertEqual(stats["pings"], 10)
            self.assertEqual(stats["failures"], 3)
            self.assertEqual(stats["outage_count"], 1)
            page = site.day_page(day).decode()
            # Outage detail and the joined ownership are both present.
            self.assertIn("Outage events", page)
            self.assertIn("100.41.135.2", page)
            self.assertIn("Verizon Business", page)
            self.assertIn("traceroute-x.txt", page)
            # The generate button always targets today, never the day shown.
            self.assertIn(
                f"value='{dt.date.today().isoformat()}'", page
            )
            self.assertNotIn(f"value='{day.isoformat()}'", page)

    def test_empty_state_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            site = Site(make_config(Path(tmpdir)))
            self.assertEqual(site.available_days(), [])
            self.assertIn(b"No ping logs found", site.index())

    def test_generate_reports_missing_data_as_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            site = Site(make_config(Path(tmpdir)))
            ok, message = site.generate(dt.date(2026, 1, 1))
            self.assertFalse(ok)
            self.assertIn("No ping data", message)

    def test_import_error_reports_environment_not_data_problem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            day = dt.date(2026, 8, 5)
            site = self._seed(tmp, day)
            import netmon_web

            def boom(*_a, **_kw):
                raise ImportError("numpy.core.multiarray failed to import")

            original = netmon_web.render_day
            netmon_web.render_day = boom
            try:
                ok, message = site.generate(day)
            finally:
                netmon_web.render_day = original
            self.assertFalse(ok)
            self.assertIn("Python environment", message)
            self.assertIn("--rebuild-venv", message)
            self.assertIn("ping logging is unaffected", message)
            # The traceback still reaches the log for the journal, even
            # though the user-facing message is the friendly one.
            self.assertIn("numpy.core.multiarray", self.captured_err())

    def test_html_escaping_of_untrusted_disk_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            day = dt.date(2026, 8, 5)
            site = self._seed(tmp, day)
            site.config.ip_ownership_file.write_text(
                json.dumps({
                    "ip": "100.41.135.2",
                    "ptr": "<script>alert(1)</script>",
                    "special": None, "rdap": None, "error": None,
                }) + "\n"
            )
            page = site.day_page(day).decode()
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)


class PathSafetyTest(QuietTestCase):
    """_safe_child must confine every served path to its base directory."""

    def setUp(self):
        self.handler = Handler.__new__(Handler)  # no socket needed

    def test_rejects_traversal_and_separators(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "base"
            (base / "evt").mkdir(parents=True)
            (base / "evt" / "ok.txt").write_text("fine")
            (Path(tmpdir) / "secret.txt").write_text("secret")
            for parts in (
                ("..", "secret.txt"),
                ("evt", ".."),
                ("evt/../..", "secret.txt"),
                ("", "ok.txt"),
                ("evt", "sub/ok.txt"),
                ("evt", "..\\ok.txt"),
            ):
                self.assertIsNone(
                    self.handler._safe_child(base, *parts), f"allowed {parts}"
                )
            self.assertIsNotNone(
                self.handler._safe_child(base, "evt", "ok.txt")
            )

    def test_parse_day_rejects_non_dates(self):
        for bad in ("../../etc/passwd", "2026-8-5", "", "2026-13-01",
                    "2026-08-05/x", "abcd-ef-gh"):
            self.assertIsNone(self.handler._parse_day(bad), f"allowed {bad}")
        self.assertEqual(
            self.handler._parse_day("2026-08-05"), dt.date(2026, 8, 5)
        )


class SchedulerTest(QuietTestCase):
    def test_next_run_is_the_configured_hour(self):
        from netmon_web import DailyChartScheduler

        with tempfile.TemporaryDirectory() as tmpdir:
            sched = DailyChartScheduler(Site(make_config(Path(tmpdir))), 1)
            # Before 1 AM -> today at 01:00.
            self.assertEqual(
                sched._next_run(dt.datetime(2026, 8, 6, 0, 30)),
                dt.datetime(2026, 8, 6, 1, 0),
            )
            # After 1 AM -> tomorrow at 01:00.
            self.assertEqual(
                sched._next_run(dt.datetime(2026, 8, 6, 9, 0)),
                dt.datetime(2026, 8, 7, 1, 0),
            )
            # Exactly 1 AM -> tomorrow (never fires twice for one day).
            self.assertEqual(
                sched._next_run(dt.datetime(2026, 8, 6, 1, 0)),
                dt.datetime(2026, 8, 7, 1, 0),
            )


if __name__ == "__main__":
    unittest.main()
