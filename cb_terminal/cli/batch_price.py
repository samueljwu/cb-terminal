"""CSV batch-pricing command for Phase 2."""

from __future__ import annotations

import argparse

from cb_terminal.domain import Assumptions
from cb_terminal.io.contract_loader import load_contract_json
from cb_terminal.io.market_history import load_market_history_csv
from cb_terminal.io.market_history_validation import validate_market_history_file_for_contract
from cb_terminal.pricing.batch import price_history, write_results_csv
from cb_terminal.pricing.engine import PricingEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Price a contract against historical market CSV rows.")
    parser.add_argument("--contract", required=True, help="Normalized contract JSON path")
    parser.add_argument("--market-history", required=True, help="Input market history CSV path")
    parser.add_argument("--output", required=True, help="Output valuation CSV path")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    contract = load_contract_json(args.contract)
    validate_market_history_file_for_contract(args.market_history, contract).raise_for_errors()
    rows = load_market_history_csv(args.market_history)
    defaults = Assumptions(
        volatility=args.volatility,
        risk_free_rate=args.risk_free_rate,
        credit_spread=args.credit_spread,
        borrow_rate=args.borrow_rate,
        dividend_yield=args.dividend_yield,
        steps=args.steps,
    )
    results = price_history(contract, rows, defaults, engine=PricingEngine(model_mode=args.model_mode))
    write_results_csv(results, args.output)
    print(f"wrote {len(results)} valuation row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
