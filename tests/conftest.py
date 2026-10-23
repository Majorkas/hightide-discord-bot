import pytest

from src.db.models import Base
from src.db.session import get_engine, get_session_factory


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
