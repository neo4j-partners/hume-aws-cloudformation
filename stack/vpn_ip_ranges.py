#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List the AWS IP ranges a VPN must allow to reach a Hume hostname.

The Hume ALB has no fixed IPs. Its addresses come from the AWS EC2 ranges for
the Region it runs in. By default this prints only the published prefixes that
contain the addresses the hostname resolves to right now. Use --region-wide to
print every EC2 prefix in that Region instead (broader, but it does not go
stale when AWS rotates ALB nodes).

Usage:
    uv run stack/vpn_ip_ranges.py hume.example.com
    uv run stack/vpn_ip_ranges.py hume.example.com --region-wide
    uv run stack/vpn_ip_ranges.py hume.example.com --region us-east-1
"""

import argparse
import ipaddress
import json
import socket
import sys
import urllib.request
from dataclasses import dataclass

IP_RANGES_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"
DEFAULT_REGION = "us-east-1"
SERVICE = "EC2"


@dataclass(frozen=True)
class Prefix:
    network: ipaddress.IPv4Network
    region: str
    service: str


def fetch_prefixes(region: str) -> list[Prefix]:
    """Download the AWS IPv4 EC2 prefixes for one Region."""
    with urllib.request.urlopen(IP_RANGES_URL, timeout=30) as response:
        data = json.load(response)
    return [
        Prefix(ipaddress.ip_network(p["ip_prefix"]), p["region"], p["service"])
        for p in data["prefixes"]
        if p["service"] == SERVICE and p["region"] == region
    ]


def resolve_ipv4(host: str) -> list[ipaddress.IPv4Address]:
    """Resolve a hostname to its sorted, unique IPv4 addresses."""
    infos = socket.getaddrinfo(
        host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
    )
    return sorted({ipaddress.IPv4Address(info[4][0]) for info in infos})


def covering_prefixes(
    addresses: list[ipaddress.IPv4Address], prefixes: list[Prefix]
) -> tuple[list[Prefix], list[ipaddress.IPv4Address]]:
    """Return the prefixes containing the addresses, and any addresses not covered."""
    matched: dict[ipaddress.IPv4Network, Prefix] = {}
    missing: list[ipaddress.IPv4Address] = []
    for address in addresses:
        hits = [p for p in prefixes if address in p.network]
        if not hits:
            missing.append(address)
            continue
        for prefix in hits:
            matched[prefix.network] = prefix
    return sorted(matched.values(), key=lambda p: p.network), missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument(
        "--region-wide",
        action="store_true",
        help=f"print every {SERVICE} prefix in the Region, not only matching ones",
    )
    args = parser.parse_args()

    prefixes = fetch_prefixes(args.region)

    if args.region_wide:
        for prefix in sorted(prefixes, key=lambda p: p.network):
            print(prefix.network)
        print(f"# {len(prefixes)} {SERVICE} prefixes in {args.region}", file=sys.stderr)
        return 0

    try:
        addresses = resolve_ipv4(args.host)
    except socket.gaierror as err:
        print(f"Could not resolve {args.host}: {err}", file=sys.stderr)
        return 1

    matched, missing = covering_prefixes(addresses, prefixes)

    resolved = ", ".join(map(str, addresses))
    print(f"# {args.host} resolves to: {resolved}", file=sys.stderr)
    for prefix in matched:
        print(prefix.network)
    if missing:
        print(
            f"# Not in any {SERVICE} prefix for {args.region}: "
            f"{', '.join(map(str, missing))}. Try another --region.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
