import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["PORTAL_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["PORTAL_SECRET_KEY"] = "test-secret-key-that-is-long-enough-for-hmac-0001"
os.environ["PORTAL_ALLOWED_ORIGINS"] = "http://127.0.0.1:18080"
os.environ["PORTAL_ENVIRONMENT"] = "test"

from h100_portal_api.database import Base, get_db
from h100_portal_api.main import app
from h100_portal_api.models import PortalRole
from h100_portal_api.rbac import PERMISSIONS

TEST_ENGINE = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autoflush=False, expire_on_commit=False)


def override_db() -> Generator[Session]:
    with TestingSession() as session:
        yield session


app.dependency_overrides[get_db] = override_db


@pytest.fixture(autouse=True)
def database() -> Generator[Session]:
    Base.metadata.drop_all(TEST_ENGINE)
    Base.metadata.create_all(TEST_ENGINE)
    with TestingSession() as session:
        for name, permissions in PERMISSIONS.items():
            session.add(PortalRole(name=name, description=name, permissions=sorted(permissions)))
        session.commit()
        yield session


@pytest.fixture
def client() -> Generator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1:18080") as value:
        yield value


@pytest.fixture
def origin_headers() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1:18080"}
