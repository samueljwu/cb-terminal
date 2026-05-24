"""Coverage-universe index helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UniverseItem:
    id: str
    isin: str
    issuer: str
    underlying_ticker: str
    contract_path: str
    market_history_path: str | None
    raw_price_history_path: str | None
    status: str


def load_universe(path: str | Path) -> list[UniverseItem]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("coverage universe must be a JSON list")
    return [universe_item_from_dict(item) for item in data]


def universe_item_from_dict(raw: dict[str, Any]) -> UniverseItem:
    for key in ("id", "issuer", "underlying_ticker", "contract_path", "status"):
        if not raw.get(key):
            raise ValueError(f"coverage item missing required field {key!r}")
    return UniverseItem(
        id=str(raw["id"]),
        isin=str(raw.get("isin", "")),
        issuer=str(raw["issuer"]),
        underlying_ticker=str(raw["underlying_ticker"]),
        contract_path=str(raw["contract_path"]),
        market_history_path=(str(raw["market_history_path"]) if raw.get("market_history_path") else None),
        raw_price_history_path=(str(raw["raw_price_history_path"]) if raw.get("raw_price_history_path") else None),
        status=str(raw["status"]),
    )
