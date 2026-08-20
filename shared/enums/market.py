from enum import StrEnum


class AssetType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    BOND = "bond"
    CASH = "cash"


class TransactionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    STOCK_SPLIT = "stock_split"


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
