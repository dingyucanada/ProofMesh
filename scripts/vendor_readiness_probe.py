#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from proofmesh.vendor_sandbox import vendor_readiness_report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Stripe test + HubSpot developer-test readiness without printing secrets"
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Call the two provider account-info APIs (disabled by default)",
    )
    args = parser.parse_args()
    report = vendor_readiness_report(probe_network=args.network)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready"] or not args.network else 2


if __name__ == "__main__":
    raise SystemExit(main())
