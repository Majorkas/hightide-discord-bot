from src.db.models import Base
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, has_ping,
    list_interest_roles, record_ping, remove_interest_role, remove_keyword,
)
from src.db.session import get_engine, get_session_factory


def _session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_add_list_remove_role():
    s = _session()
    r = add_interest_role(s, 1, 111, "Yu-Gi-Oh", emoji="🎴")
    assert r.id is not None
    roles = list_interest_roles(s, 1)
    assert [x.label for x in roles] == ["Yu-Gi-Oh"]
    assert roles[0].emoji == "🎴"
    assert remove_interest_role(s, 1, r.id) is True
    assert list_interest_roles(s, 1) == []
    assert remove_interest_role(s, 1, 999) is False


def test_keywords_and_ping_once():
    s = _session()
    r = add_interest_role(s, 1, 111, "MTG")
    add_keyword(s, r.id, "mtg")
    add_keyword(s, r.id, "magic")
    kw = get_keywords_by_role(s, 1)
    assert kw[r.id] == ["mtg", "magic"]
    assert remove_keyword(s, r.id, "magic") is True
    assert get_keywords_by_role(s, 1)[r.id] == ["mtg"]
    assert has_ping(s, 42, r.id) is False
    record_ping(s, 1, 42, r.id)
    assert has_ping(s, 42, r.id) is True


def test_guild_isolation():
    s = _session()
    add_interest_role(s, 1, 111, "A")
    add_interest_role(s, 2, 222, "B")
    assert [x.label for x in list_interest_roles(s, 1)] == ["A"]
