from src.db.models import Base, Event, GuildConfig
from src.db.session import get_engine, get_session_factory


def test_models_create_and_insert():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    # bind factory's engine for the in-memory case
    factory.configure(bind=engine)
    session = factory()
    gc = GuildConfig(guild_id=1)
    session.add(gc)
    ev = Event(guild_id=1, external_id="wp:1", title="Test", url="https://x")
    session.add(ev)
    session.commit()
    assert session.query(GuildConfig).count() == 1
    assert session.query(Event).first().external_id == "wp:1"
