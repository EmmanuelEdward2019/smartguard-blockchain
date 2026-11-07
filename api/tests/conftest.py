"""Test fixtures.

The suite runs against SQLite in a temporary file rather than PostgreSQL. The
models use portable types precisely so this works, and it keeps the suite
runnable with no service to start. The routes exercise the same SQLAlchemy code
either way; what SQLite cannot verify is Postgres-specific behaviour, and none
is relied on.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

# The chain layer is switched off for the suite, overriding .env rather than
# deferring to it. Left on, every test run would reach Sepolia: slow, dependent
# on a network and an RPC quota, and liable to spend testnet ether. The chain
# paths are exercised against the real network by
# tests/test_chain_integration.py, which is opt-in.
os.environ["CHAIN_ENABLED"] = "false"
os.environ["CHAIN_SYNC_ENABLED"] = "false"

_db_fd, _db_path = tempfile.mkstemp(suffix=".sqlite")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

from fastapi.testclient import TestClient  # noqa: E402

from smartguard_api.config import get_settings  # noqa: E402
from smartguard_api.db import SessionLocal, create_all, engine  # noqa: E402
from smartguard_api.main import app  # noqa: E402
from smartguard_api.models import Base  # noqa: E402
from smartguard_api.seed import ACCOUNTS, seed_users  # noqa: E402


class StubScorer:
    """Stands in for the trained model.

    The routes are being tested, not the classifier. The score is driven by the
    feature vector so a test can steer the gate deliberately, which is what
    makes the gate testable at all.
    """

    def __init__(self) -> None:
        self.model_path = Path("stub")
        self.model_version = "stub-v1"
        self.is_available = True
        self.load_error = None
        self.forced_score: float | None = None
        self.raise_on_score = False

    def load(self) -> None:
        pass

    def score(self, features):
        from smartguard_api.inference import InferenceUnavailable, RiskAssessment

        if self.raise_on_score:
            raise InferenceUnavailable("Stub scorer was told to fail.")

        if self.forced_score is not None:
            value = self.forced_score
        else:
            # High-risk geography plus a device mismatch is enough to clear the
            # gate, which mirrors how the real features behave.
            value = 0.10
            if features.get("geographic_risk"):
                value += 0.45
            if not features.get("device_consistency"):
                value += 0.35

        return RiskAssessment(
            risk_score=round(min(value, 1.0), 4),
            model_version="stub-v1",
            latency_ms=12,
            exceeded_budget=False,
            contributions=[
                {"feature": "counterparty_reputation", "value": 0.9, "importance": 0.19},
            ],
        )


@pytest.fixture(scope="session", autouse=True)
def _database():
    create_all()
    yield
    Base.metadata.drop_all(bind=engine)
    Path(_db_path).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Each test starts from an empty database with the four accounts seeded."""
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        seed_users(session)
    get_settings.cache_clear()
    yield


@pytest.fixture
def scorer() -> StubScorer:
    stub = StubScorer()
    app.state.scorer = stub
    return stub


@pytest.fixture
def client(scorer) -> TestClient:
    with TestClient(app) as test_client:
        # TestClient's lifespan replaces app.state.scorer with the real loader;
        # put the stub back so tests control scoring.
        app.state.scorer = scorer
        yield test_client


@pytest.fixture
def tokens(client) -> dict[str, str]:
    """A bearer token per role."""
    result = {}
    for account in ACCOUNTS:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": account["password"]},
        )
        assert response.status_code == 200, response.text
        result[account["role"].value] = response.json()["accessToken"]
    return result


@pytest.fixture
def auth():
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def seller_id(client, tokens, auth) -> str:
    from smartguard_api.models import Role, User

    with SessionLocal() as session:
        seller = session.query(User).filter(User.role == Role.SELLER).one()
        return seller.id
