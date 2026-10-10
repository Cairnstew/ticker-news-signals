"""Symbol resolution.

Turns a user-supplied ticker into a :class:`ResolvedTicker`: the normalised
pair key, the base asset, a human name (when known) and the set of search
terms providers use to find relevant headlines.

Crypto is full of symbol collisions and short symbols ("ETH") that appear
inside unrelated words ("together", "whether"), so providers filter on
whole-word matches of an explicit term set rather than raw substring queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .utils import normalize_ticker

#: Separators accepted in a user ticker; everything after the first is dropped.
_SEPARATORS = ("/", "-", ":", "_")

#: Curated symbol -> canonical name (and optional aliases) map for the majors.
#: A missing symbol simply resolves to its bare base; that is not an error.
SYMBOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "BTC": ("Bitcoin", ("bitcoin", "btc")),
    "ETH": ("Ethereum", ("ethereum", "ether")),
    "SOL": ("Solana", ("solana",)),
    "XRP": ("XRP", ("ripple",)),
    "ADA": ("Cardano", ("cardano",)),
    "DOGE": ("Dogecoin", ("dogecoin",)),
    "LTC": ("Litecoin", ("litecoin",)),
    "LINK": ("Chainlink", ("chainlink",)),
    "DOT": ("Polkadot", ("polkadot",)),
    "AVAX": ("Avalanche", ("avalanche",)),
    "MATIC": ("Polygon", ("polygon", "matic")),
    "POL": ("Polygon", ("polygon",)),
    "UNI": ("Uniswap", ("uniswap",)),
    "AAVE": ("Aave", ("aave",)),
    "ATOM": ("Cosmos", ("cosmos",)),
    "ALGO": ("Algorand", ("algorand",)),
    "XLM": ("Stellar", ("stellar", "lumens")),
    "TRX": ("TRON", ("tron",)),
    "NEAR": ("NEAR Protocol", ("near protocol",)),
    "FIL": ("Filecoin", ("filecoin",)),
    "ARB": ("Arbitrum", ("arbitrum",)),
    "OP": ("Optimism", ("optimism",)),
    "SUI": ("Sui", ("sui",)),
    "APT": ("Aptos", ("aptos",)),
    "INJ": ("Injective", ("injective",)),
    "SHIB": ("Shiba Inu", ("shiba inu", "shib")),
    "PEPE": ("Pepe", ("pepe",)),
    "TON": ("Toncoin", ("toncoin", "the open network")),
    "BCH": ("Bitcoin Cash", ("bitcoin cash",)),
    "ETC": ("Ethereum Classic", ("ethereum classic",)),
    "HBAR": ("Hedera", ("hedera",)),
    "ICP": ("Internet Computer", ("internet computer",)),
    "RNDR": ("Render", ("render network",)),
    "STX": ("Stacks", ("stacks",)),
    "IMX": ("Immutable", ("immutable",)),
    "GRT": ("The Graph", ("the graph",)),
    "MKR": ("Maker", ("makerdao", "maker")),
    "CRV": ("Curve", ("curve dao",)),
    "LDO": ("Lido", ("lido",)),
    "SAND": ("The Sandbox", ("the sandbox", "sandbox")),
    "MANA": ("Decentraland", ("decentraland",)),
    "AXS": ("Axie Infinity", ("axie infinity",)),
    "USDT": ("Tether", ("tether", "usdt")),
    "USDC": ("USD Coin", ("usd coin", "usdc")),
    "BNB": ("BNB", ("binance coin",)),
}


@dataclass(frozen=True, slots=True)
class ResolvedTicker:
    """The outcome of resolution: everything the providers need to know."""

    query: str
    symbol: str
    base: str
    name: str | None = None
    terms: tuple[str, ...] = ()
    """Whole-word match terms (lower-cased), e.g. ``("eth", "ethereum", "ether")``."""

    aliases: tuple[str, ...] = ()

    @property
    def search_query(self) -> str:
        """The best single string to hand a keyword search endpoint."""
        return self.name or self.base

    def matches(self, text: str) -> bool:
        """Whole-word (or phrase), case-insensitive match of any term in ``text``.

        Single tokens are matched on word boundaries so ``ETH`` never matches
        "together"; multi-word terms are matched as phrases.
        """
        if not self.terms:
            return True
        lowered = text.lower()
        tokens = _words(lowered)
        for term in self.terms:
            if " " in term:
                if term in lowered:
                    return True
            elif term in tokens:
                return True
        return False

    def with_name(self, name: str) -> ResolvedTicker:
        """Return a copy with ``name`` set and its derived term added."""
        cleaned = name.strip()
        if not cleaned:
            return self
        terms = _dedupe_terms((*self.terms, cleaned.lower()))
        return ResolvedTicker(
            query=self.query,
            symbol=self.symbol,
            base=self.base,
            name=cleaned,
            terms=terms,
            aliases=self.aliases,
        )


def _dedupe_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


#: Strip a query down to the base symbol; kept in sync with :mod:`utils`.
def _extract_base(ticker: str) -> str:
    text = ticker.strip()
    for sep in _SEPARATORS:
        if sep in text:
            text = text.split(sep, 1)[0]
    return text.strip().upper()


def resolve_ticker(
    ticker: str, *, symbols: dict[str, tuple[str, tuple[str, ...]]] | None = None
) -> ResolvedTicker:
    """Resolve a user ticker into a :class:`ResolvedTicker`.

    ``ETH/USD``, ``eth-usd`` and ``ETH`` all resolve to the same base ``ETH``;
    a symbol in :data:`SYMBOLS` also gains its human name and search terms.
    """
    table = SYMBOLS if symbols is None else symbols
    base = _extract_base(ticker)
    symbol = normalize_ticker(ticker)
    entry = table.get(base)
    if entry is None:
        return ResolvedTicker(
            query=ticker,
            symbol=symbol,
            base=base,
            terms=_dedupe_terms((base.lower(),)),
        )
    name, aliases = entry
    terms = _dedupe_terms((base.lower(), name.lower(), *aliases))
    return ResolvedTicker(
        query=ticker,
        symbol=symbol,
        base=base,
        name=name,
        terms=terms,
        aliases=aliases,
    )


#: Cheap whole-word tokeniser; a frozenset per call is fine for headline volumes.
def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall(text))


_WORD_RE = re.compile(r"[a-z0-9]+")


__all__ = ["SYMBOLS", "ResolvedTicker", "resolve_ticker"]
