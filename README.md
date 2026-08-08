# FiOS network connectivity monitor

A small evidence-collection tool for intermittent internet outages. It
pings a target once per second, records every result, runs verbose
traceroutes for the duration of every outage, identifies who owns each hop
after the run, and renders a static chart you can attach to an email to
your ISP's support team.

Everything stays on your machine. The only packets that leave it are the
pings, the traceroutes, and (after the run, optionally) the reverse-DNS and
RDAP ownership lookups.

## What it produces

```
logs/
  ping-2026-08-06.jsonl          one JSON record per ping, daily files,
  ping-2026-08-07.jsonl          rolled at local midnight
  monitor-errors.log             problems with the MONITOR itself (never
                                 mixed into the network evidence)
  ip-ownership.jsonl             one record per unique IP seen in any
                                 traceroute: PTR name + RDAP org
  charts/
    connection-2026-08-06.png    one chart per day, rendered nightly by
    connection-2026-08-07.png    the web UI (or on demand)
  traceroutes/
    traceroute-2026-08-06T14-23-07/     one directory per outage event,
      traceroute-2026-08-06T14-23-07.txt   named for the event start;
      traceroute-2026-08-06T14-23-17.txt   one file per traceroute run
      ...                                  (every 10 s until recovery)
```

A ping record looks like:

```json
{"ts": "2026-08-06T14:23:07.100-04:00", "target": "8.8.8.8",
 "success": false, "latency_ms": null, "outage_id": "2026-08-06T14-23-07"}
```

`outage_id` groups a continuous run of failures into one outage event and
matches the event's traceroute directory name. Records with
`"monitor_error": true` (and `success: null`) mean the *tool* had a
problem — they are never counted as network failures.

## Installation (Ubuntu)

```bash
git clone https://github.com/clams2121/fios_network_test.git
cd fios_network_test
sudo apt install traceroute                      # ping ships with Ubuntu
cp config.example.toml config.toml               # then edit it
./deploy.sh --install-service                    # sets up everything else
```

`deploy.sh` creates a **project virtualenv** in `.venv/` and installs
matplotlib into it from `requirements.txt`, then points the systemd units
at that interpreter. The monitor itself (`netmon.py`) is standard library
only; matplotlib is needed for charts, and keeping it in a virtualenv
isolates it from whatever else the system Python has installed.

If you'd rather use the system Python (with apt's `python3-matplotlib`),
pass `--no-venv` to every `deploy.sh` run.

Requires Python 3.11+ (Ubuntu 23.04 or newer; Ubuntu 24.04 LTS ships
3.12). Python 3.11 is what makes the standard-library TOML parser
available, so the monitor itself has **zero third-party dependencies**.
`matplotlib` is the one dependency of the visualization script, because
rendering publication-quality static images is not something the standard
library can do.

Both `ping` and `traceroute` work unprivileged on Ubuntu — no root needed.

## Configuration

The config is TOML (`config.example.toml` documents every key). TOML was
chosen over INI and YAML because it is parseable by the standard library
alone (3.11+), it has real types — the on-failure action list is an array
of tables, which INI cannot express — and it has none of YAML's implicit
typing surprises or dependency cost.

Validation is strict: every key is required, and the monitor exits with a
message naming the exact problem (missing key, bad IP, absent binary,
unwritable directory) rather than falling back to any default. A ping
timeout longer than the ping interval is also rejected, since a
still-waiting ping would collide with the next scheduled one.

Relative paths are resolved against the config file's directory, so the
monitor behaves identically no matter where it is launched from (shell or
systemd).

## Running the monitor

```bash
python3 netmon.py --config config.toml
```

Stop it with Ctrl-C or SIGTERM. On shutdown it finishes any in-flight
traceroute, flushes and closes the logs, and **then** runs the IP ownership
lookups — deliberately after monitoring ends, because DNS and RDAP are
themselves unreachable during an outage and mid-outage lookups would fail
and pollute the evidence. Lookups are cached in `ip-ownership.jsonl`
across runs, so each IP is only ever queried once.

If a run ends without a clean shutdown (crash, power loss), rebuild the
ownership file from the traceroute files on disk:

```bash
python3 netmon_ownership.py --config config.toml --rescan
```

### Running persistently (systemd)

To have the monitor run all the time, install it as a systemd service —
no `nohup` needed. The easy way is the deploy script:

```bash
./deploy.sh --install-service
```

That generates both units (`systemd/netmon.service` and
`systemd/netmon-web.service`) with this checkout's real paths and your
user, installs them to `/etc/systemd/system/`, enables them, and starts
them.
Compared to `nohup`/backgrounding, systemd gives you the three things a
monitor actually needs:

* it isn't tied to your terminal session (log out freely);
* it **starts automatically on boot**, so a power blip to the monitoring
  box doesn't silently end evidence collection;
* `Restart=on-failure` brings it back automatically if it ever aborts.

If you'd rather install by hand: edit the paths in
`systemd/netmon.service` and `systemd/netmon-web.service`, then
`sudo cp systemd/netmon*.service /etc/systemd/system/ &&
sudo systemctl daemon-reload &&
sudo systemctl enable --now netmon netmon-web`.

If you had previously started the monitor manually in a terminal, stop
that one first so two monitors aren't pinging at once.

### Day-to-day management

There are two services: `netmon` (collects the evidence) and
`netmon-web` (serves the charts). They are independent — restarting the
web UI never interrupts monitoring.

```bash
systemctl status netmon netmon-web   # are they running?
journalctl -u netmon -f              # live monitor log (outage starts/ends, blips)
journalctl -u netmon-web -f          # live web log (chart renders, requests)
sudo systemctl restart netmon        # restart (rarely needed; deploy.sh does this)
sudo systemctl stop netmon           # clean shutdown — runs the IP ownership lookups
sudo systemctl start netmon          # start again after a stop
```

Stopping (and restarting) is always a *clean* shutdown: in-flight
traceroutes finish and the post-run IP ownership lookups execute before
the process exits, so it can take up to a minute. The unit sets
`TimeoutStopSec=180` so systemd gives that time before escalating to
SIGKILL.

### Updating / redeploying

```bash
./deploy.sh                    # pull latest code, refresh dependencies,
                               # validate config, run tests, restart services
./deploy.sh --install-service  # first-time setup: additionally generates
                               # the systemd units with this checkout's
                               # paths, interpreter and user, then enables
                               # and starts them
./deploy.sh --rebuild-venv     # discard and rebuild .venv from scratch
                               # (the fix for broken Python dependencies)
./deploy.sh --no-venv          # use the system python3 instead of .venv
```

The script is deliberately cautious: it stops before touching the
services if the pull, the dependency install, the **chart-dependency
check**, the config validation, or the test suite fails, so a working
monitor keeps running. The restart itself is a clean shutdown (in-flight
traceroutes finish, IP ownership lookups run), so it can take a minute.
Without installed services it just tells you how to run them in the
foreground.

The chart-dependency check actually imports numpy and matplotlib and
draws a figure, rather than merely checking that matplotlib is present —
because the failure mode below passes a presence check but breaks at
render time. If the units on disk are stale (for example they still point
at the system Python after a switch to the virtualenv), `deploy.sh`
notices and refreshes them.

## Troubleshooting

### `ImportError: numpy.core.multiarray failed to import`

Seen when generating a chart. matplotlib and numpy must be a matched
pair built against the same ABI; this error means the matplotlib being
imported was built against a different numpy major version than the one
actually loaded. It is typically caused by mixing apt's
`python3-matplotlib` with a pip-installed numpy in the same interpreter.
Reinstalling numpy on its own does not fix it — the two have to come from
the same place.

The fix is to give the services their own virtualenv, which is what
`deploy.sh` does by default:

```bash
./deploy.sh --rebuild-venv
```

That deletes `.venv/`, recreates it (deliberately *without*
`--system-site-packages`, so the system's conflicting packages are not
inherited), installs a consistent matplotlib/numpy pair from
`requirements.txt`, verifies they import and can draw, and updates the
systemd units to run from that interpreter. Restart afterwards if you
did not let the script do it:

```bash
sudo systemctl restart netmon-web
```

To confirm which interpreter a service is actually using:

```bash
systemctl show -p ExecStart netmon-web
.venv/bin/python3 -c "import matplotlib, numpy; print(matplotlib.__version__, numpy.__version__)"
```

### The web UI shows a chart error but the monitor is fine

That is by design — they are separate services. Chart rendering problems
never interrupt ping logging, so no evidence is lost while you fix the
Python environment; regenerate the affected days afterwards with the
button (today) or by restarting `netmon-web`, which backfills any day
missing a chart.

## Generating the chart

```bash
python3 visualize.py --config config.toml                       # everything available
python3 visualize.py --config config.toml --start 2026-08-01 --end 2026-08-07
python3 visualize.py --config config.toml -o report.png -o report.pdf
```

The chart reads across all rolled daily files in the range and shows
latency over time with outages shaded red, plus a summary line: ping count,
loss rate, outage count, total downtime, and the longest outage. It also
prints the same summary as text for pasting into an email body.

Evidence-integrity details, all visible in the legend:

* Periods where the **monitor was not running** are shaded gray — absence
  of data is never mistaken for (or hides) an outage.
* **Monitor errors** are drawn as separate markers and excluded from all
  outage statistics.
* For long ranges the latency line is downsampled to bucket means, and a
  lighter **max envelope** is drawn as well so short latency spikes are
  not averaged out of sight. Outage spans and loss statistics are always
  computed from the raw records, never from the downsampled series.

## Web UI

A small local web server (standard library only) for browsing the
evidence day by day:

```bash
python3 netmon_web.py --config config.toml
```

It is normally run as a service — `./deploy.sh --install-service`
installs it alongside the monitor — and offers:

* **a sidebar of every day** that has ping logs, each with a badge
  showing that day's outage count;
* **that day's chart**, plus the headline stats (pings, loss rate,
  outages, downtime, longest outage);
* **a "Generate today's chart" button** — today is the only day still
  accumulating data, so it is the only one worth regenerating on demand;
* **per-outage detail**: each event's start, duration, the hop IPs seen
  in its traceroutes joined against `ip-ownership.jsonl` (so you can see
  where it broke — your router, Verizon, or beyond), and links to the raw
  capture files.

### Daily charts

The web server renders the chart for the day that just ended at
`web_daily_chart_hour` (1 AM by default). It also **backfills on
startup**: any day that has logs but no chart is rendered when the
service starts, so charts are never permanently missed because the
server was down overnight.

### Access and security

The server binds **only** to `web_bind_ip`, and that bind address is the
entire access-control model — there is no authentication:

| `web_bind_ip` | Who can reach it |
|---|---|
| `127.0.0.1` | only this machine (use `ssh -L 8477:127.0.0.1:8477 host` to view remotely) |
| your LAN address, e.g. `192.168.1.50` | anything on your LAN |

Do not bind it to an internet-facing address. Apart from the regenerate
button the UI is read-only, it serves only files inside the configured
log/chart/traceroute directories (path traversal is rejected), and the
web server runs as its own service — restarting or crashing it never
interrupts the monitor's data collection.

## How outage response works

Each second the monitor pings `ping_target_ip` on a fixed-rate schedule
(the next ping is scheduled at start + N seconds on the monotonic clock,
so ping duration cannot make the cadence drift — and one ping per second
means exactly that, never a flood).

After `outage_open_after_failures` consecutive failed pings (default 3 in
the example config; set to 1 for maximum sensitivity) an outage event
opens, named for — and starting at — the **first** failed ping of the run.
Failures below the threshold are "blips": still logged and counted in loss
statistics, but they trigger no traceroutes and no event. Pre-threshold
failure records are briefly buffered (at most a couple of seconds) so that
when an outage is confirmed, every record from the first failure onward
carries the event's id.
A worker thread runs every configured on-failure action immediately, then
again every `traceroute_interval_seconds` until a ping succeeds, so a
shifting failure point is captured over the course of the event. The
traceroute targets a *different* IP than the ping (`traceroute_target_ip`)
so one destination network's problem can't blind both probes, and runs
with `-n` because resolving hop names mid-outage would hang on dead DNS.

**One file per traceroute inside a per-event directory** (rather than one
growing per-event file) was chosen deliberately: each file is written in a
single atomic operation after its traceroute completes, so a monitor crash
or kill mid-outage can never corrupt or truncate previously captured
traceroutes; each filename carries its own timestamp; and files can be
globbed, diffed, and attached individually. The `#`-prefixed header in
each file (command, start/finish time, exit status) keeps the raw tool
output below it pristine.

### Joining traceroutes to ownership

Every hop IP in every traceroute appears in `logs/ip-ownership.jsonl`:

```json
{"ip": "100.41.135.2", "ptr": "ae201-0.NWRKNJ-VFTTP-311.verizon-gni.net",
 "special": null, "rdap": {"name": "VIS-BLOCK", "org": "Verizon Business",
 "handle": "NET-100-8-0-0-1", "country": null,
 "range": {"start": "100.8.0.0", "end": "100.41.255.255"}},
 "error": null, "looked_up_at": "2026-08-07T09:12:03-04:00"}
```

`special` labels non-public ranges (your router's RFC 1918 address,
carrier-grade NAT inside the ISP, etc.). RDAP queries go through
https://rdap.org, which redirects to the authoritative registry (ARIN for
Verizon space); set `rdap_lookups = false` to keep runs fully offline
apart from PTR lookups.

## Adding your own on-failure actions

The failure response is a config-driven action list — no code changes
needed for new external checks:

```toml
[[on_failure_actions]]
type = "command"
name = "dns-check"
command = ["dig", "+time=2", "+tries=1", "@8.8.8.8", "example.com"]
```

Every action runs once per cycle during an outage and writes its output to
`<event dir>/<name>-<timestamp>.txt` with the same header format. For an
action that needs logic rather than just a command, subclass `Action` in
`netmon_actions.py` and add one entry to its `ACTION_TYPES` registry.

## "Network down" vs. "monitor broken"

The two are strictly separated, because evidence you hand to an ISP must
not contain your own tool's failures:

| Signal | Meaning | Where it goes |
|---|---|---|
| `ping` exits 1 (no reply) | **network failure** | ping log, opens/extends an outage |
| `ping` exits 0 with a latency | network OK | ping log |
| `ping` exits 2, can't start, unparseable output; an action crashes | **monitor problem** | `monitor-errors.log` + stderr + a `monitor_error` ping record; never touches outage state |

Ten consecutive monitor errors abort the run loudly (exit 1) rather than
logging garbage forever; under systemd, `Restart=on-failure` then applies.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Standard-library `unittest` only. Covers outage open/group/close
semantics, the blip threshold, the monitor-error separation, config
validation, ping output parsing, IP extraction, the chart's
outage-span/gap reconstruction, and the web layer — page rendering,
HTML escaping of on-disk content, path-traversal rejection, and the
daily scheduler's next-run arithmetic.
