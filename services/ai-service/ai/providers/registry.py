from ai.providers.base import (FundamentalDataProvider, MacroDataProvider,
                               MarketDataProvider, NewsProvider,
                               WebSearchProvider)
from ai.providers.fred import FredProvider
from ai.providers.newsapi import NewsApiProvider
from ai.providers.sec_edgar import SecEdgarProvider
from ai.providers.tavily import TavilyProvider
from ai.providers.yahoo_finance import YahooFinanceProvider

_yahoo = YahooFinanceProvider()
_sec_edgar = SecEdgarProvider()
_fred = FredProvider()
_newsapi = NewsApiProvider()
_tavily = TavilyProvider()


def get_market_data_provider() -> MarketDataProvider:
    return _yahoo


def get_fundamental_data_providers() -> list[FundamentalDataProvider]:
    return [_sec_edgar, _yahoo]


def get_news_provider() -> NewsProvider:
    return _newsapi


def get_macro_data_provider() -> MacroDataProvider:
    return _fred


def get_web_search_provider() -> WebSearchProvider:
    return _tavily
