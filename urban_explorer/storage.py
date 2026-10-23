import json
from pathlib import Path

from sqlalchemy import Column, Integer, String, Text, UniqueConstraint, create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session

from urban_explorer.models import Place, SessionState


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(String(100), primary_key=True)
    state = Column(Text, nullable=False)


class SavedPlace(Base):
    __tablename__ = "saved_places"
    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    place_id = Column(String(100), nullable=False)
    payload = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("session_id", "place_id"),)


class Preference(Base):
    __tablename__ = "preferences"
    session_id = Column(String(100), primary_key=True)
    payload = Column(Text, nullable=False)


class Store:
    def __init__(self, url: str):
        database = make_url(url).database
        if url.startswith("sqlite") and database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        options = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=options)
        Base.metadata.create_all(self.engine)

    def get_session(self, session_id: str) -> SessionState:
        with Session(self.engine) as db:
            row = db.get(Conversation, session_id)
            return SessionState.model_validate_json(row.state) if row else SessionState()

    def save_session(self, session_id: str, state: SessionState) -> None:
        with Session(self.engine) as db:
            db.merge(Conversation(id=session_id, state=state.model_dump_json()))
            db.commit()

    def favorites(self, session_id: str) -> list[Place]:
        with Session(self.engine) as db:
            rows = db.scalars(select(SavedPlace).where(SavedPlace.session_id == session_id).order_by(SavedPlace.id.desc()))
            return [Place.model_validate_json(row.payload) for row in rows]

    def save_favorite(self, session_id: str, place: Place) -> None:
        with Session(self.engine) as db:
            row = db.scalar(select(SavedPlace).where(SavedPlace.session_id == session_id, SavedPlace.place_id == place.id))
            if row:
                row.payload = place.model_dump_json()
            else:
                db.add(SavedPlace(session_id=session_id, place_id=place.id, payload=place.model_dump_json()))
            db.commit()

    def remove_favorite(self, session_id: str, place_id: str) -> None:
        with Session(self.engine) as db:
            db.execute(delete(SavedPlace).where(SavedPlace.session_id == session_id, SavedPlace.place_id == place_id))
            db.commit()

    def preferences(self, session_id: str) -> list[str]:
        with Session(self.engine) as db:
            row = db.get(Preference, session_id)
            return json.loads(row.payload) if row else []

    def save_preferences(self, session_id: str, preferences: list[str]) -> None:
        with Session(self.engine) as db:
            db.merge(Preference(session_id=session_id, payload=json.dumps(preferences, ensure_ascii=False)))
            db.commit()
