"""Tests for the meetings DB model, DDL, and repositories."""
from sqlalchemy import text

from src.db.models import Meeting
from src.db.repositories import add_meeting, list_meetings
from src.db.session import ensure_meeting_tables, get_engine

GUILD = 4242


def test_meeting_table_created_and_idempotent(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'm.db'}")
    ensure_meeting_tables(engine)
    ensure_meeting_tables(engine)  # idempotent
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "meetings" in names
    engine.dispose()


def test_add_and_list_meetings(session):
    add_meeting(session, GUILD, 1, "meet1.m4a", "Key points A", "excerpt A")
    add_meeting(session, GUILD, 1, "meet2.m4a", "Key points B", "excerpt B")
    add_meeting(session, GUILD + 1, 2, "other.m4a", "Other guild", "x")
    rows = list_meetings(session, GUILD)
    assert [m.filename for m in rows] == ["meet2.m4a", "meet1.m4a"]  # newest first
    assert all(m.guild_id == GUILD for m in rows)


def test_meeting_fields_roundtrip(session):
    m = add_meeting(session, GUILD, 5, "staff-meeting.mp3", "Talked about drafts", "the new deckboxes...")
    assert m.id is not None
    assert (m.channel_id, m.filename) == (5, "staff-meeting.mp3")
    assert m.summary == "Talked about drafts"
    assert m.transcript_excerpt == "the new deckboxes..."
    assert m.created_at is not None


def test_list_meetings_respects_limit(session):
    for i in range(30):
        add_meeting(session, GUILD, 1, f"m{i}.m4a", f"s{i}", "e")
    assert len(list_meetings(session, GUILD, limit=25)) == 25
