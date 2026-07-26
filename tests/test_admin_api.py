import base64
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import UserAccount


class AdminReadApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            admin = UserAccount(
                username="admin",
                display_name="Counselor Admin",
                password_hash=hash_password("admin-password"),
            )
            admin.roles = {"ROLE_ADMIN"}
            db.add(admin)
            db.commit()

        self.app = create_app()

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app, raise_server_exceptions=False)
        token = base64.b64encode(b"admin:admin-password").decode("ascii")
        self.admin_auth = {"Authorization": f"Basic {token}"}

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_admin_read_endpoints_return_success_for_admin(self):
        self.assertEqual(self.client.get("/api/admin/reports", headers=self.admin_auth).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/agent-traces", headers=self.admin_auth).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/tool-audits", headers=self.admin_auth).status_code, 200)


if __name__ == "__main__":
    unittest.main()
