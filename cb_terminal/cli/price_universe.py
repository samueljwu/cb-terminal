"""Universe-level pricing command."""

from __future__ import annotations

import argparse
from pathlib import Path

from cb_terminal.domain import Assumptions
from cb_terminal.pricing.engine import PricingEngine
from cb_terminal.pricing.universe_batch import price_universe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Price every contract in a coverage universe with market-history data.")
    parser.add_argument("--universe", required=True, help="Coverage universe JSON path")
    parser.add_argument("--output", required=True, help="Aggregate valuation CSV output path")
    parser.add_argument("--latest-summary-output", default=None, help="Latest-row summary CSV output path")
    parser.add_argument("--html-report", default=None, help="Optional static HTML universe report path")
    parser.add_argument("--volatility", type=float, required=True, help="Default volatility as decimal, e.g. 0.35")
    parser.add_argument("--risk-free-rate", type=float, required=True, help="Default risk-free rate as decimal")
    parser.add_argument("--credit-spread", type=float, default=0.0, help="Default credit spread as decimal")
    parser.add_argument("--borrow-rate", type=float, default=0.0, help="Default borrow rate as decimal")
    parser.add_argument("--dividend-yield", type=float, default=0.0, help="Default dividend yield as decimal")
    parser.add_argument("--steps", type=int, default=250, help="Binomial tree steps")
    parser.add_argument(
        "--model-mode",
        choices=sorted(PricingEngine.SUPPORTED_MODEL_MODES),
        default="tf_split_tree",
        help="Pricing model mode (default: tf_split_tree)",
    )
    parser.add_argument("--project-root", default=".", help="Project root for resolving relative universe paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    defaults = Assumptions(
        volatility=args.volatility,
        risk_free_rate=args.risk_free_rate,
        credit_spread=args.credit_spread,
        borrow_rate=args.borrow_rate,
        dividend_yield=args.dividend_yield,
        steps=args.steps,
    )
    report = price_universe(
        args.universe,
        defaults,
        output_csv=args.output,
        latest_summary_csv=args.latest_summary_output,
        html_report=args.html_report,
        project_root=Path(args.project_root),
        model_mode=args.model_mode,
    )
    print(f"wrote universe valuation rows: {args.output}")
    if args.latest_summary_output:
        print(f"wrote latest summary: {args.latest_summary_output}")
    if args.html_report:
        print(f"wrote html report: {args.html_report}")
    print(
        f"priced_contracts={report.priced_contracts} skipped_contracts={report.skipped_contracts} "
        f"valuation_rows={report.result_rows}"
    )
    for skipped in report.skipped:
        print(f"skipped {skipped['contract_id']}: {skipped['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
