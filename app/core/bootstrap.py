from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Base, engine
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.rag_ingestion.schema import AccessClass
from app.rag_ingestion.service import IngestionSubmission, KnowledgeIngestionService


def create_schema() -> None:
    """仅供测试 Harness 显式建表；生产启动改由迁移管理。"""
    Base.metadata.create_all(bind=engine)


def ensure_database_current(settings=None) -> None:
    """在开始提供服务前确认 Alembic 已处于 head。"""
    from app.cli.migrate import check

    check((settings or get_settings()).database_url)


def seed_data(db: Session, settings: Settings | None = None) -> None:
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



def submit_builtin_knowledge(
    db: Session,
    *,
    settings: Settings | None = None,
    knowledge_root: Path | None = None,
    task_dispatcher=None,
    include_pdfs: bool = True,
) -> list[IngestionSubmission]:
    runtime_settings = settings or get_settings()
    root = knowledge_root or Path(__file__).resolve().parents[1] / "knowledge"
    service = KnowledgeIngestionService(
        db,
        runtime_settings,
        task_dispatcher=task_dispatcher,
    )
    files = [*root.glob("*.md"), *root.glob("*.markdown"), *root.glob("*.txt")]
    if include_pdfs and (root / "pdf").is_dir():
        files.extend((root / "pdf").glob("*.pdf"))
    submissions = []
    mime_by_suffix = {
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
    }
    for file in sorted(files, key=lambda item: item.as_posix().casefold()):
        relative = file.relative_to(root).as_posix()
        submissions.append(
            service.submit_file(
                filename=file.name,
                data=file.read_bytes(),
                mime_type=mime_by_suffix[file.suffix.casefold()],
                actor="bootstrap",
                access_class=AccessClass.BUILTIN_PUBLIC,
                cloud_vision_allowed=True,
                source_key=f"builtin:{relative.casefold()}",
            )
        )
    return submissions
