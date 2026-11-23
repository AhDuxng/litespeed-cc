"""Command-line entry point.

  run        execute a campaign (campaign.py), then summarise it
  dish       print one Starlink status sample, to verify the gRPC path
  summarize  per-cell statistics of a results directory (summarize.py)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import summarize
from .config import load

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "client.env"


def _run(args: argparse.Namespace) -> int:
    from .campaign import Campaign
    from .curl import CurlError

    campaign = Campaign(load(args.config), with_dish=not args.no_dish)
    try:
        plan = campaign.prepare()
    except CurlError as error:
        print(f"server unreachable for at least one stack or port: {error}", file=sys.stderr)
        return 1
    print(f"results: {plan.out_dir}  seed: {plan.seed}", flush=True)
    try:
        campaign.run(plan)
    except KeyboardInterrupt:
        print(f"\ninterrupted; completed transfers are kept in {plan.out_dir}", file=sys.stderr)
        return 130
    _report(plan.out_dir, server_logs=None)
    return 0


def _dish(args: argparse.Namespace) -> int:
    from . import dish

    config = load(args.config)
    with dish.DishClient(config.dish_address, timeout_s=dish.CONNECT_TIMEOUT_S) as client:
        status = client.status()
    print(json.dumps(status if args.raw else dish.project(status), indent=2))
    return 0


def _summarize(args: argparse.Namespace) -> int:
    _report(args.results, args.server_logs)
    return 0


def _report(results: Path, server_logs: Optional[Path]) -> None:
    cells = summarize.summarize(results, server_logs)
    summarize.write(cells, results / "summary.csv")
    print(summarize.render(cells))
    print(f"\nwritten: {results / 'summary.csv'}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a measurement campaign")
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    run.add_argument("--no-dish", action="store_true", help="skip Starlink telemetry (off-Starlink testing)")
    run.set_defaults(handler=_run)

    probe = commands.add_parser("dish", help="print one Starlink dish status sample")
    probe.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    probe.add_argument("--raw", action="store_true", help="print the full status message")
    probe.set_defaults(handler=_dish)

    report = commands.add_parser("summarize", help="summarise a results directory")
    report.add_argument("results", type=Path)
    report.add_argument(
        "--server-logs", type=Path, help="server logs/ directory fetched from the server (cpu.csv, tcp_cc.csv)"
    )
    report.set_defaults(handler=_summarize)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
