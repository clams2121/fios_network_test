"""Post-run IP ownership identification.

For every unique IP that appeared in any traceroute (or other action output)
during a monitoring run, this module records:

  * reverse DNS (PTR) name, and
  * the owning organisation via RDAP (the structured successor to WHOIS),
    queried through https://rdap.org, which redirects to the authoritative
    regional registry (ARIN, RIPE, etc.).

It runs ONLY after monitoring stops — never during an outage — because DNS
and RDAP are themselves unreachable while the connection is down and
mid-outage lookups would fail and pollute the evidence.

Results are appended to a JSONL file keyed by IP.  The file doubles as the
cache: IPs already present are never queried again, across runs.

Can also be run standalone to (re)scan all recorded traceroute output —
useful if a run ended in a crash instead of a clean shutdown:

    python3 netmon_ownership.py --config config.toml --rescan
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

RDAP_URL = "https://rdap.org/ip/{ip}"
LOOKUP_TIMEOUT_SECONDS = 10


def _classify(ip: str) -> str | None:
    """A label for addresses that have no public registration, or None if
    the IP is publicly routable and worth an RDAP query."""
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.is_multicast:
        return "multicast"
    if isinstance(addr, ipaddress.IPv4Address):
        if any(
            addr in ipaddress.ip_network(net)
            for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            return "private (RFC 1918) — e.g. your own router"
        if addr in ipaddress.ip_network("100.64.0.0/10"):
            return "carrier-grade NAT (RFC 6598) — inside the ISP's network"
    elif addr in ipaddress.ip_network("fc00::/7"):
        return "private (IPv6 ULA)"
    if addr.is_private or not addr.is_global:
        return "special-use / non-public range"
    return None


def _ptr_lookup(ip: str) -> str | None:
    try:
        name, _aliases, _addrs = socket.gethostbyaddr(ip)
        return name
    except (socket.herror, socket.gaierror, OSError):
        return None


def _rdap_lookup(ip: str) -> tuple[dict | None, str | None]:
    """Return (rdap_summary, error). Network/HTTP problems are reported in
    the record rather than raised: a missing registration for one hop must
    not abort the lookups for the rest."""
    request = urllib.request.Request(
        RDAP_URL.format(ip=ip),
        headers={"Accept": "application/rdap+json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=LOOKUP_TIMEOUT_SECONDS
        ) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        return None, f"RDAP HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"RDAP lookup failed: {exc}"

    org = None
    for entity in data.get("entities", []) or []:
        roles = entity.get("roles", []) or []
        if "registrant" in roles or org is None:
            vcard = entity.get("vcardArray")
            if isinstance(vcard, list) and len(vcard) == 2:
                for item in vcard[1]:
                    if isinstance(item, list) and item and item[0] == "fn":
                        org = item[3]
                        if "registrant" in roles:
                            break
    summary = {
        "name": data.get("name"),
        "handle": data.get("handle"),
        "org": org,
        "country": data.get("country"),
        "range": {
            "start": data.get("startAddress"),
            "end": data.get("endAddress"),
        },
    }
    return summary, None


def _load_cache(path: Path) -> set[str]:
    known: set[str] = set()
    if not path.exists():
        return known
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                known.add(record["ip"])
            except (ValueError, KeyError):
                print(
                    f"WARNING: {path}:{lineno} is not a valid ownership "
                    f"record; ignoring it",
                    file=sys.stderr,
                )
    return known


def identify_ips(
    ips: set[str], ownership_file: Path, *, rdap: bool, log=print
) -> int:
    """Look up every IP not already in the ownership file and append the
    results.  Returns the number of new records written."""
    known = _load_cache(ownership_file)
    pending = sorted(ips - known, key=ipaddress.ip_address)
    if not pending:
        return 0

    log(f"Identifying {len(pending)} new IP(s) "
        f"({len(known)} already cached in {ownership_file})...")
    socket.setdefaulttimeout(LOOKUP_TIMEOUT_SECONDS)
    written = 0
    with open(ownership_file, "a", encoding="utf-8") as fh:
        for ip in pending:
            special = _classify(ip)
            ptr = _ptr_lookup(ip)
            rdap_summary, error = None, None
            if special:
                error = None
            elif rdap:
                rdap_summary, error = _rdap_lookup(ip)
            record = {
                "ip": ip,
                "ptr": ptr,
                "special": special,
                "rdap": rdap_summary,
                "error": error,
                "looked_up_at": dt.datetime.now().astimezone().isoformat(),
            }
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            written += 1
            owner = special or (rdap_summary or {}).get("org") \
                or (rdap_summary or {}).get("name") or error or "unknown"
            log(f"  {ip:<40} ptr={ptr or '-'}  owner={owner}")
    return written


def scan_traceroute_dir(traceroute_dir: Path) -> set[str]:
    """Collect IPs from every recorded action output file on disk."""
    from netmon_actions import extract_ips

    ips: set[str] = set()
    for path in sorted(traceroute_dir.glob("*/*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(
            line for line in text.splitlines() if not line.startswith("#")
        )
        ips |= extract_ips(body)
    return ips


def main() -> int:
    parser = argparse.ArgumentParser(
        description="(Re)build the IP ownership file from recorded "
        "traceroute output."
    )
    parser.add_argument("--config", required=True, help="path to config.toml")
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="scan all traceroute output on disk (default behavior; flag "
        "kept for clarity)",
    )
    args = parser.parse_args()

    from netmon_config import ConfigError, load_config

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    ips = scan_traceroute_dir(config.traceroute_dir)
    if not ips:
        print(f"No IPs found in {config.traceroute_dir}; nothing to do.")
        return 0
    written = identify_ips(
        ips, config.ip_ownership_file, rdap=config.rdap_lookups
    )
    print(f"Done: {written} new record(s) in {config.ip_ownership_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
