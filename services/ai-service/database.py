from config import settings
from shared.database import (create_service_engine, create_session_factory,
                             make_get_db)

engine = create_service_engine(settings)
SessionLocal = create_session_factory(engine)
get_db = make_get_db(SessionLocal)


def register_all_models() -> None:
    import domains.analysis.model
    import domains.analytics.model
    import domains.backtest.model
    import domains.decision.model
    import domains.filings.model
    import domains.knowledge.model
    import domains.market.model
    import domains.memory.model
    import domains.rag.model  # noqa: F401
