"""Pluggable on-failure actions for the network monitor.

During an outage event every registered action runs once per cycle (at the
start of the outage and then every `traceroute_interval_seconds`).  Each run
writes one file into the outage event's directory.

Adding a new kind of action later:

  1. Subclass ``Action`` and implement ``run()`` (or just reuse
     ``CommandAction``, which captures any external command's output).
  2. Register it in ``ACTION_TYPES``.
  3. Reference the new ``type`` in a ``[[on_failure_actions]]`` table in the
     config file.

Nothing else in the monitor needs to change.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import subprocess
from pathlib import Path

from netmon_config import ActionConfig, Config

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Per-run cap so a wedged external command cannot stall an action cycle
# forever.  Generous relative to the 10 s cycle: cycles simply run
# back-to-back if an action overruns.
ACTION_TIMEOUT_SECONDS = 120


def extract_ips(text: str) -> set[str]:
    """Every syntactically valid IP address appearing in *text*.

    IPv4 is regex-matched; IPv6 (including compressed ``::`` forms, which a
    regex handles poorly) is found by validating each colon-bearing
    whitespace-delimited token.
    """
    valid: set[str] = set()
    for candidate in _IPV4_RE.findall(text):
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        valid.add(candidate)
    for token in re.split(r"[\s,()\[\]<>]+", text):
        if ":" not in token:
            continue
        token = token.strip(".,;\"'")
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        valid.add(token)
    return valid


class ActionError(Exception):
    """The action itself failed to execute (a monitor problem, not a network
    result)."""


class Action:
    """Base class for on-failure actions."""

    def __init__(self, action_config: ActionConfig, config: Config):
        self.name = action_config.name
        self.command = list(action_config.command)
        self.config = config

    def argv(self) -> list[str]:
        return self.command

    def run(self, event_dir: Path) -> set[str]:
        """Run once, write output into *event_dir*, return IPs seen."""
        started = dt.datetime.now().astimezone()
        stamp = started.strftime("%Y-%m-%dT%H-%M-%S")
        out_path = event_dir / f"{self.name}-{stamp}.txt"
        argv = self.argv()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=ACTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ActionError(
                f"action '{self.name}' exceeded {ACTION_TIMEOUT_SECONDS}s: "
                f"{argv}"
            ) from exc
        except OSError as exc:
            raise ActionError(
                f"action '{self.name}' could not execute {argv}: {exc}"
            ) from exc
        finished = dt.datetime.now().astimezone()

        # Header lines are '#'-prefixed so the raw tool output below them
        # stays pristine and machine-separable.
        header = (
            f"# action: {self.name}\n"
            f"# command: {' '.join(argv)}\n"
            f"# started: {started.isoformat()}\n"
            f"# finished: {finished.isoformat()}\n"
            f"# exit_status: {proc.returncode}\n"
        )
        body = proc.stdout
        if proc.stderr:
            body += f"\n# --- stderr ---\n{proc.stderr}"
        out_path.write_text(header + body)
        return extract_ips(proc.stdout)


class TracerouteAction(Action):
    """Runs the configured traceroute command against
    ``traceroute_target_ip`` (appended automatically)."""

    def argv(self) -> list[str]:
        return self.command + [self.config.traceroute_target_ip]


class CommandAction(Action):
    """Runs an arbitrary configured command verbatim and captures its
    output.  The escape hatch for future checks (DNS probes, HTTP probes,
    custom scripts) with no code changes."""


ACTION_TYPES: dict[str, type[Action]] = {
    "traceroute": TracerouteAction,
    "command": CommandAction,
}


def build_actions(config: Config) -> list[Action]:
    return [
        ACTION_TYPES[entry.type](entry, config)
        for entry in config.on_failure_actions
    ]
