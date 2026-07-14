from shared.database import (create_service_engine, create_session_factory,
                             make_get_db)
from app.config import settings

engine = create_service_engine(settings)
SessionLocal = create_session_factory(engine)
get_db = make_get_db(SessionLocal)
