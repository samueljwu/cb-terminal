"""Dollar-neutral convertible-bond repricing."""

from __future__ import annotations

from cb_terminal.validation import require_finite, require_positive


def nuke(
    anchor_bond_price: float,
    anchor_stock_price: float,
    anchor_fx: float,
    current_stock_price: float,
    current_fx: float,
    delta: float,
) -> float:
    """Reprice an anchor bond quote for the current FX-adjusted stock move.

    ``anchor_fx`` and ``current_fx`` use the project-wide ``STOCK_PER_CB``
    convention: units of stock currency per unit of bond currency. Therefore,
    dividing the stock price by FX expresses it in the bond currency.

    ``delta`` is the anchor sensitivity in bond-price points for a one-unit
    change in that FX-adjusted stock price. The linear nuke freezes that delta
    and holds the anchor's other context fixed, including volatility, rates,
    credit, and time:

        anchor bond + delta * (current stock / current FX
                               - anchor stock / anchor FX)

    The approximation intentionally does not model convexity over large moves.
    """

    require_positive("anchor_bond_price", anchor_bond_price)
    require_positive("anchor_stock_price", anchor_stock_price)
    require_positive("anchor_fx", anchor_fx)
    require_positive("current_stock_price", current_stock_price)
    require_positive("current_fx", current_fx)
    require_finite("delta", delta)

    anchor_stock_in_bond_currency = anchor_stock_price / anchor_fx
    current_stock_in_bond_currency = current_stock_price / current_fx
    bond_price = anchor_bond_price + delta * (
        current_stock_in_bond_currency - anchor_stock_in_bond_currency
    )
    require_finite("nuked bond price", bond_price)
    return bond_price
