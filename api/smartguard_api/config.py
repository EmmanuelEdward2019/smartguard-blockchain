"""Service configuration.

Everything is read from the environment, which is the only place secrets belong
(Section 3.15.2: the oracle signing key "is stored outside the application
source code and is not written to application logs").
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `model_path` and `model_version` refer to the machine-learning model,
        # not to pydantic's BaseModel. Narrowing the reserved prefix stops
        # pydantic warning about a collision that does not exist.
        protected_namespaces=("settings_",),
    )

    # --- identity -------------------------------------------------------
    # Shared with the presentation layer. The Next.js proxy verifies tokens
    # this service signs, so the two must hold the same key and algorithm.
    session_secret: str = ""
    jwt_algorithm: str = "HS256"
    session_ttl_seconds: int = 60 * 60 * 8

    # --- storage --------------------------------------------------------
    database_url: str = "postgresql+psycopg://smartguard:smartguard@localhost:5432/smartguard"

    # --- intelligence ---------------------------------------------------
    # Produced by ml/. When absent the service refuses to score rather than
    # inventing a number — Section 3.15.2 requires the system to fail closed.
    model_path: Path = REPO_ROOT / "ml" / "artifacts" / "model_random_forest.joblib"
    model_version: str = "rf-paysim-v1.0.0"

    # NFR-01. Exceeding it is recorded on the prediction, not silently ignored.
    inference_budget_ms: int = 2000

    # --- escrow parameters ----------------------------------------------
    # Mirrored to the contract's owner-only setters. Section 3.11.3, 3.11.1.
    risk_threshold: float = 0.75
    time_lock_hours: int = 48
    holding_period_hours: int = 168

    fail_closed_on_inference_error: bool = True

    # --- blockchain ------------------------------------------------------
    # Off by default. The service must start, and its tests must run, on a
    # machine with no RPC endpoint and no keys.
    chain_enabled: bool = False

    sepolia_rpc_url: str = ""
    oracle_private_key: str = ""

    # Left empty, addresses are read from docs/evidence/deployment.sepolia.json,
    # which the deploy script writes and the report cites. One source of truth
    # beats asking an operator to copy two addresses in correctly.
    escrow_contract_address: str = ""
    risk_oracle_contract_address: str = ""

    # The demonstration custody account. depositFunds is payable and must be
    # signed by whoever owns the money; this service signs for the buyer.
    # That is a demonstration arrangement, not a custody model — see the note
    # at the top of chain.py and Section 3.15.
    chain_funder_private_key: str = ""

    # What is actually sent on chain, regardless of the notional amount shown
    # in the interface, so a demonstration does not drain the faucet.
    chain_demo_amount_eth: float = 0.0002

    # Sepolia inclusion takes about eleven seconds; this bounds the wait when
    # the network is congested rather than hanging a worker indefinitely.
    chain_tx_timeout_seconds: int = 180

    # The event listener polls rather than subscribes, because the HTTP
    # providers this project uses do not offer websockets on the free tier.
    chain_poll_seconds: int = 15
    chain_sync_enabled: bool = False

    # --- service --------------------------------------------------------
    cors_origins: str = "http://localhost:3000"
    environment: str = "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def resolved_secret(self) -> str:
        """The signing key, with the same production guard the web layer uses.

        A development fallback keeps local runs working. Signing production
        sessions with a key committed to the source tree would not.
        """
        if self.session_secret:
            return self.session_secret
        if self.is_production:
            raise RuntimeError(
                "SESSION_SECRET is not set. Refusing to sign sessions with the "
                "development key.",
            )
        return "smartguard-development-key-do-not-use-in-production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
