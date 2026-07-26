import json

from sqlalchemy.orm import Session

from app.models.entities import SecurityAuditRecord, UserAccount


class SecurityAuditService:
    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        actor: UserAccount,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        ip_address: str,
    ) -> None:
        self.db.add(
            SecurityAuditRecord(
                user_id=actor.id,
                actor=actor.username,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                ip_address=ip_address,
                details_json=json.dumps({"outcome": outcome}, ensure_ascii=False),
            )
        )
        self.db.commit()
