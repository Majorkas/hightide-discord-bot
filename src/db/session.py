from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def get_engine(url: str):
    return create_engine(url, future=True)


def get_session_factory(url: str) -> sessionmaker:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)
