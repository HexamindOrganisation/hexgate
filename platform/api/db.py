from pathlib import Path
from sqlmodel import Session, SQLModel, create_engine

DB_PATH = Path(__file__).parent / "fortify.db"
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
