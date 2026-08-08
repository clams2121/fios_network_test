# FiOS network connectivity monitor

Evidence-collection for intermittent internet outages. Pings a target once
per second, runs verbose traceroutes for the duration of every outage,
identifies who owns each hop, and produces daily charts you can attach to
an email to your ISP.

Everything stays local. The only packets that leave the machine are the
pings, the traceroutes, and the ownership lookups (PTR/RDAP, after a run).

Requires Ubuntu with Python 3.11+ (24.04 LTS ships 3.12). `ping` and
`traceroute` run unprivileged — no root needed for monitoring.

## Install

```bash
git clone https://github.com/clams2121/fios_network_test.git
cd fios_network_test
cp config.example.toml config.toml
nano config.toml                 # set ping_target_ip, web_bind_ip, etc.
./deploy.sh --install-service
```

`deploy.sh` does the rest: installs `traceroute` and `python3-venv` if
missing, creates a virtualenv in `.venv/` with matplotlib, installs and
starts both systemd services, and prints the web UI's URL.

Two services get installed:

| Service | Job |
|---|---|
| `netmon` | pings, logs, runs traceroutes during outages |
| `netmon-web` | serves the charts and evidence; renders daily charts |

They are independent — restarting the web UI never interrupts monitoring.

## Update / redeploy

```bash
./deploy.sh
```

Pulls the latest code, refreshes dependencies, validates the config, runs
the tests, and restarts both services. It stops **before** touching the
services if any of those steps fail, so a working monitor keeps running.

| Flag | Use |
|---|---|
| *(none)* | normal update + restart |
| `--install-service` | first-time setup: also install and enable the systemd units |
| `--rebuild-venv` | delete and rebuild `.venv/` — **the fix for broken Python dependencies** (see Troubleshooting) |
| `--no-venv` | use the system `python3` with apt's `python3-matplotlib` instead of the virtualenv |

Flags can be combined. If the installed units are stale — for example
still pointing at the system Python after a switch to the virtualenv —
`deploy.sh` notices and refreshes them.

## Day-to-day

```bash
systemctl status netmon netmon-web   # are they running?
journalctl -u netmon -f              # live monitor log (outages, blips)
journalctl -u netmon-web -f          # live web log (chart renders, requests)
sudo systemctl stop netmon           # clean shutdown
sudo systemctl start netmon
```

Stopping or restarting `netmon` is always a clean shutdown: in-flight
traceroutes finish and the IP ownership lookups run first, so it can take
up to a minute (`TimeoutStopSec=180`).

## Troubleshooting

### `ImportError: numpy.core.multiarray failed to import`

Seen when generating a chart. matplotlib and numpy must be a matched pair
built against the same ABI; this means the matplotlib being imported was
built against a different numpy major version than the one loaded. It is
usually caused by mixing apt's `python3-matplotlib` with a pip-installed
numpy. **Reinstalling numpy alone does not fix it** — both have to come
from the same place.

```bash
./deploy.sh --rebuild-venv
```

That deletes `.venv/`, recreates it (without `--system-site-packages`, so
the conflicting system packages are not inherited), installs a consistent
matplotlib/numpy pair, verifies they import and can draw, updates the
systemd units to use that interpreter, and restarts the services.

To check which interpreter a service actually uses:

```bash
systemctl show -p ExecStart netmon-web
.venv/bin/python3 -c "import matplotlib, numpy; print(matplotlib.__version__, numpy.__version__)"
```

### Charts fail but the monitor is fine

Expected — they are separate services, so no ping data is lost while you
fix the Python environment. Afterwards, restarting `netmon-web` backfills
every day that is missing a chart.

### A service will not start

```bash
journalctl -u netmon -n 30 --no-pager
```

Config problems are reported by name (missing key, bad IP, absent binary,
unwritable directory). Validate without starting anything:

```bash
.venv/bin/python3 netmon.py --config config.toml --check
```

## Web UI

Browse the evidence day by day at the address you configured (default
`http://127.0.0.1:8477/`):

* a **sidebar of every day** with ping logs, badged with its outage count;
* that day's **chart** and headline stats (pings, loss rate, outages,
  downtime, longest outage);
* a **"Generate today's chart"** button — today is the only day still
  accumulating data;
* **per-outage detail**: start, duration, the hop IPs from its traceroutes
  joined against `ip-ownership.jsonl` (so you can see whether it broke at
  your router, inside Verizon, or beyond), and links to the raw captures.

Charts for completed days are rendered automatically at
`web_daily_chart_hour` (1 AM). Any day with logs but no chart is
backfilled when the service starts, so nothing is permanently missed if
the server was down overnight.

### Access and security

The server binds **only** to `web_bind_ip`. There is no authentication —
the bind address is the entire access-control model:

| `web_bind_ip` | Reachable from |
|---|---|
| `127.0.0.1` | this machine only (`ssh -L 8477:127.0.0.1:8477 host` to view remotely) |
| a LAN address, e.g. `192.168.1.50` | anything on your LAN |

Do not bind an internet-facing address. Apart from the generate button
the UI is read-only, and it serves only files inside the configured
directories (path traversal is rejected).

## Charts on the command line

For a single chart spanning multiple days — the one to attach to an
email:

```bash
.venv/bin/python3 visualize.py --config config.toml
.venv/bin/python3 visualize.py --config config.toml --start 2026-08-01 --end 2026-08-07
.venv/bin/python3 visualize.py --config config.toml -o report.png -o report.pdf
```

Defaults to everything available. Prints the same summary as text for
pasting into the email body.

The chart is built to be honest about its own gaps: outages are shaded
red, periods where **the monitor was not running** are shaded gray (so
missing data is never mistaken for uptime), and monitor errors are drawn
as separate markers, excluded from outage statistics. Over long ranges
the latency line is downsampled to bucket means with a lighter max
envelope so spikes are not averaged away; outage spans and loss rates
always come from the raw records.

## Configuration

TOML, parsed by the standard library. `config.example.toml` documents
every key. Validation is strict — every key is required and the process
exits with a message naming the exact problem rather than falling back to
a default. Relative paths resolve against the config file's directory.

Keys worth knowing:

| Key | Meaning |
|---|---|
| `ping_target_ip` | pinged once per `ping_interval_seconds` |
| `traceroute_target_ip` | traced during outages; deliberately a different host |
| `outage_open_after_failures` | consecutive lost pings before an outage opens (3) |
| `traceroute_interval_seconds` | gap between traceroutes during an outage (10) |
| `web_bind_ip` / `web_port` | web UI address — see Access and security |
| `web_daily_chart_hour` | hour to render the completed day's chart (1) |
| `rdap_lookups` | set `false` to skip ownership org lookups (PTR still runs) |

## Files it produces

```
logs/
  ping-2026-08-06.jsonl          one JSON record per ping, rolled at
  ping-2026-08-07.jsonl          local midnight
  monitor-errors.log             problems with the MONITOR itself
  ip-ownership.jsonl             PTR + RDAP org per unique hop IP
  charts/
    connection-2026-08-06.png    one chart per day
  traceroutes/
    traceroute-2026-08-06T14-23-07/       one directory per outage,
      traceroute-2026-08-06T14-23-07.txt  named for its start; one file
      traceroute-2026-08-06T14-23-17.txt  per traceroute run
```

A ping record:

```json
{"ts": "2026-08-06T14:23:07.100-04:00", "target": "8.8.8.8",
 "success": false, "latency_ms": null, "outage_id": "2026-08-06T14-23-07"}
```

`outage_id` groups a run of failures into one event and matches that
event's traceroute directory name. Records with `"monitor_error": true`
(and `success: null`) mean the tool had a problem, never the network.

An ownership record, joinable to any hop by `ip`:

```json
{"ip": "100.41.135.2", "ptr": "ae201-0.NWRKNJ-VFTTP-311.verizon-gni.net",
 "special": null, "rdap": {"org": "Verizon Business", "name": "VIS-BLOCK"},
 "error": null, "looked_up_at": "2026-08-07T09:12:03-04:00"}
```

`special` labels non-public ranges (your router's RFC 1918 address,
carrier-grade NAT inside the ISP). Lookups run after monitoring stops —
DNS and RDAP are unreachable mid-outage — and are cached so each IP is
queried once ever. Rebuild them after an unclean shutdown with:

```bash
.venv/bin/python3 netmon_ownership.py --config config.toml --rescan
```

## How outage detection works

Pings run on a fixed-rate schedule (each ping is scheduled at start + N
seconds on the monotonic clock, so ping duration cannot make the cadence
drift).

An outage opens after `outage_open_after_failures` consecutive failures,
but is timestamped from the **first** failed ping — pre-threshold records
are buffered briefly and join the event retroactively. Failures that
recover before the threshold are logged as "blips": counted in loss
statistics, but no event and no traceroutes.

While an outage is open, every configured on-failure action runs
immediately and then every `traceroute_interval_seconds`, so a shifting
failure point is captured over the event. Each run writes its own
timestamped file (atomically, so a crash cannot truncate earlier
captures) with a `#`-prefixed header above the raw output.

### "Network down" vs. "monitor broken"

Kept strictly separate, because evidence handed to an ISP must not
contain your own tool's failures:

| Signal | Meaning | Where it goes |
|---|---|---|
| `ping` exits 0 with a latency | network OK | ping log |
| `ping` exits 1 (no reply) | **network failure** | ping log; opens/extends an outage |
| `ping` exits 2, won't start, unparseable output; an action crashes | **monitor problem** | `monitor-errors.log` + stderr + a `monitor_error` record; never touches outage state |

Ten consecutive monitor errors abort the run loudly rather than logging
garbage; `Restart=on-failure` then applies.

## Adding your own on-failure checks

Config-driven, no code changes:

```toml
[[on_failure_actions]]
type = "command"
name = "dns-check"
command = ["dig", "+time=2", "+tries=1", "@8.8.8.8", "example.com"]
```

Each action runs once per cycle during an outage, writing to
`<event dir>/<name>-<timestamp>.txt`. For logic beyond running a command,
subclass `Action` in `netmon_actions.py` and register it in
`ACTION_TYPES`.

## Tests

```bash
.venv/bin/python3 -m unittest discover -s tests
```

Standard-library `unittest` only. Covers outage lifecycle and the blip
threshold, monitor-error separation, config validation, ping parsing, IP
extraction, chart span/gap reconstruction, and the web layer (page
rendering, HTML escaping, path-traversal rejection, scheduler timing).
A passing run prints only dots — output from the error paths under test
is captured deliberately.

## Running without systemd

```bash
.venv/bin/python3 netmon.py --config config.toml
.venv/bin/python3 netmon_web.py --config config.toml
```

Ctrl-C or SIGTERM shuts down cleanly. Prefer the services for anything
long-running: they survive logout, start on boot, and restart on failure.
Stop a manually started monitor before starting the service, so two are
not pinging at once.
