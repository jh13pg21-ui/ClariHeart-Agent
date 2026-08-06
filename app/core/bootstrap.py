from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def create_schema() -> None:
    """仅供测试 Harness 显式建表；生产启动改由迁移管理。"""
    Base.metadata.create_all(bind=engine)


def ensure_database_current(settings=None) -> None:
    """在开始提供服务前确认 Alembic 已处于 head。"""
    from app.cli.migrate import check

    check((settings or get_settings()).database_url)


def seed_data(db: Session) -> None:
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password("admin123"),
            password_algorithm="argon2id",
            must_reset_password=False,
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password("student123"),
            password_algorithm="argon2id",
            must_reset_password=False,
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()

    service = KnowledgeService(db, get_settings())
    root = Path(__file__).resolve().parents[1]
    for file in sorted((root / "knowledge").glob("*.md")):
        service.ensure_source(file.name, file.read_text(encoding="utf-8"))
