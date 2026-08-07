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
git clone <this repo> && cd fios_network_test
sudo apt install traceroute python3-matplotlib   # ping ships with Ubuntu
cp config.example.toml config.toml               # then edit it
```

(`python3-matplotlib` comes from apt rather than pip because Ubuntu 23.04+
marks the system Python as externally managed, so a bare `pip install`
fails; the apt package sidesteps that and is only needed for
`visualize.py`. If you prefer pip, use a virtualenv.)

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

That generates the unit from `systemd/netmon.service` with this
checkout's real paths and your user, installs it to
`/etc/systemd/system/netmon.service`, enables it, and starts it.
Compared to `nohup`/backgrounding, systemd gives you the three things a
monitor actually needs:

* it isn't tied to your terminal session (log out freely);
* it **starts automatically on boot**, so a power blip to the monitoring
  box doesn't silently end evidence collection;
* `Restart=on-failure` brings it back automatically if it ever aborts.

If you'd rather install by hand: edit the two paths in
`systemd/netmon.service`, then
`sudo cp systemd/netmon.service /etc/systemd/system/ &&
sudo systemctl daemon-reload && sudo systemctl enable --now netmon`.

If you had previously started the monitor manually in a terminal, stop
that one first so two monitors aren't pinging at once.

### Day-to-day management

```bash
systemctl status netmon          # is it running?
journalctl -u netmon -f          # watch the live log (outage starts/ends, blips)
sudo systemctl restart netmon    # restart (rarely needed; deploy.sh does this)
sudo systemctl stop netmon       # clean shutdown — runs the IP ownership lookups
sudo systemctl start netmon      # start again after a stop
```

Stopping (and restarting) is always a *clean* shutdown: in-flight
traceroutes finish and the post-run IP ownership lookups execute before
the process exits, so it can take up to a minute. The unit sets
`TimeoutStopSec=180` so systemd gives that time before escalating to
SIGKILL.

### Updating / redeploying

```bash
./deploy.sh                    # pull latest code, refresh dependencies,
                               # validate config, run tests, restart service
./deploy.sh --install-service  # first-time setup: additionally generates
                               # the systemd unit with this checkout's
                               # paths and your user, enables and starts it
```

The script is deliberately cautious: it stops before touching the service
if the pull, the dependency install, the config validation, or the test
suite fails, so a working monitor keeps running. The restart itself is a
clean shutdown (in-flight traceroutes finish, IP ownership lookups run),
so it can take a minute. Without an installed service it just tells you
how to run the monitor in the foreground.

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
semantics, the monitor-error separation, config validation, ping output
parsing, IP extraction, and the chart's outage-span/gap reconstruction.
