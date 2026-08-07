"""Configuration loading and validation for the network monitor.

TOML was chosen because:
  * it is parseable with the standard library alone on Python 3.11+ (tomllib);
  * it has real types (numbers, booleans, arrays of tables), which the
    on-failure action list needs and which INI cannot express cleanly;
  * unlike YAML it needs no third-party dependency and has no surprising
    implicit-typing behavior.

Validation is strict and loud: every required key must be present and
well-formed or the program exits with a message naming the exact problem.
There are no silent defaults.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python >= 3.11
except ModuleNotFoundError:  # pragma: no cover
    sys.exit(
        "ERROR: this tool needs Python 3.11+ for the standard-library TOML "
        "parser (tomllib). Found Python %s." % sys.version.split()[0]
    )


class ConfigError(Exception):
    """A missing or malformed configuration value."""


@dataclass
class ActionConfig:
    """One entry from the [[on_failure_actions]] list."""

    type: str
    name: str
    command: list[str] = field(default_factory=list)


@dataclass
class Config:
    ping_target_ip: str
    traceroute_target_ip: str
    ping_interval_seconds: float
    ping_timeout_seconds: float
    traceroute_interval_seconds: float
    log_dir: Path
    traceroute_dir: Path
    ip_ownership_file: Path
    rdap_lookups: bool
    on_failure_actions: list[ActionConfig]


def _require(raw: dict, key: str, kind, kindname: str):
    if key not in raw:
        raise ConfigError(f"required key '{key}' is missing")
    value = raw[key]
    # bool is a subclass of int in Python; keep them distinct.
    if kind in (int, float) and isinstance(value, bool):
        raise ConfigError(f"'{key}' must be a {kindname}, got a boolean")
    if kind is float and isinstance(value, int):
        value = float(value)
    if not isinstance(value, kind):
        raise ConfigError(
            f"'{key}' must be a {kindname}, got {type(value).__name__}"
        )
    return value


def _require_ip(raw: dict, key: str) -> str:
    value = _require(raw, key, str, "string")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ConfigError(f"'{key}' is not a valid IP address: {value!r}")
    return value


def _require_positive(raw: dict, key: str) -> float:
    value = _require(raw, key, float, "number")
    if value <= 0:
        raise ConfigError(f"'{key}' must be > 0, got {value}")
    return value


def _parse_action(index: int, raw: object) -> ActionConfig:
    where = f"on_failure_actions[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table ([[on_failure_actions]])")
    action_type = raw.get("type")
    if action_type not in ("traceroute", "command"):
        raise ConfigError(
            f"{where}: 'type' must be \"traceroute\" or \"command\", "
            f"got {action_type!r}"
        )
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise ConfigError(
            f"{where}: 'command' must be a non-empty list of strings "
            f"(argv form), e.g. [\"traceroute\", \"-n\"]"
        )
    name = raw.get("name", action_type)
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}: 'name' must be a non-empty string")
    if any(ch in name for ch in "/\0 "):
        raise ConfigError(
            f"{where}: 'name' is used in file names and must not contain "
            f"spaces or slashes: {name!r}"
        )
    return ActionConfig(type=action_type, name=name, command=command)


def _check_binary(argv0: str, context: str) -> None:
    if shutil.which(argv0) is None:
        raise ConfigError(
            f"{context}: executable '{argv0}' was not found on PATH. "
            f"Install it (e.g. 'sudo apt install {argv0}') or fix the "
            f"configured command."
        )


def _prepare_dir(path: Path, key: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create '{key}' directory {path}: {exc}")
    if not os.access(path, os.W_OK):
        raise ConfigError(f"'{key}' directory {path} is not writable")


def load_config(path: str | Path, *, prepare_dirs: bool = True) -> Config:
    """Parse and validate the config file. Raises ConfigError on any problem."""
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}")

    actions_raw = raw.get("on_failure_actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        raise ConfigError(
            "at least one [[on_failure_actions]] entry is required"
        )
    actions = [_parse_action(i, entry) for i, entry in enumerate(actions_raw)]
    names = [a.name for a in actions]
    if len(names) != len(set(names)):
        raise ConfigError(
            "on_failure_actions entries must have unique names "
            f"(saw {names}); add a distinct 'name' key to each"
        )
    for action in actions:
        _check_binary(action.command[0], f"on_failure_actions '{action.name}'")
    _check_binary("ping", "ping monitor")

    # Paths are resolved relative to the config file so the monitor behaves
    # the same regardless of the working directory it is launched from
    # (important under systemd).
    base = path.resolve().parent

    def _path(key: str) -> Path:
        value = _require(raw, key, str, "string")
        p = Path(value)
        return p if p.is_absolute() else base / p

    config = Config(
        ping_target_ip=_require_ip(raw, "ping_target_ip"),
        traceroute_target_ip=_require_ip(raw, "traceroute_target_ip"),
        ping_interval_seconds=_require_positive(raw, "ping_interval_seconds"),
        ping_timeout_seconds=_require_positive(raw, "ping_timeout_seconds"),
        traceroute_interval_seconds=_require_positive(
            raw, "traceroute_interval_seconds"
        ),
        log_dir=_path("log_dir"),
        traceroute_dir=_path("traceroute_dir"),
        ip_ownership_file=_path("ip_ownership_file"),
        rdap_lookups=_require(raw, "rdap_lookups", bool, "boolean"),
        on_failure_actions=actions,
    )

    if config.ping_timeout_seconds > config.ping_interval_seconds:
        raise ConfigError(
            "ping_timeout_seconds must not exceed ping_interval_seconds: "
            "a ping still waiting for a reply would collide with the next "
            "scheduled ping"
        )

    if prepare_dirs:
        _prepare_dir(config.log_dir, "log_dir")
        _prepare_dir(config.traceroute_dir, "traceroute_dir")
        _prepare_dir(config.ip_ownership_file.parent, "ip_ownership_file")

    return config
