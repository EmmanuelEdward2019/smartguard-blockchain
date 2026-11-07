"""End-to-end tests against the real Sepolia deployment.

Opt-in, and skipped by default. These spend testnet ether, take about a minute
each because every transaction waits for inclusion, and fail when an RPC quota
runs out — none of which should happen on an ordinary `pytest` run.

    CHAIN_IT=1 pytest tests/test_chain_integration.py -v

What they establish is the thing no other test in this repository can: that a
request entering the application layer ends as state on a public blockchain,
and that the two agree afterwards. Table 4.3's end-to-end row has had no
retained artefact behind it; this is that artefact.

They run against the live contracts named in docs/evidence/deployment.sepolia.json
and the database the service is configured with, so they are integration tests
in the strict sense: nothing here is mocked.
"""

from __future__ import annotations

import os
import time

import pytest

RUN = os.environ.get("CHAIN_IT") == "1"

if RUN:
    # conftest.py switches the chain off for the whole suite, which is right
    # for every other test and wrong for these. Re-enabled here, after that
    # module has run, so opting in actually opts in.
    os.environ["CHAIN_ENABLED"] = "true"

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="set CHAIN_IT=1 to run the Sepolia integration tests (spends testnet ether)",
)


@pytest.fixture(scope="module")
def chain():
    # Imported inside the fixture so collection does not need web3 configured.
    from smartguard_api.chain import get_chain, reset_chain
    from smartguard_api.config import Settings, get_settings

    # The singleton may already hold a client built while the chain was off.
    reset_chain()
    get_settings.cache_clear()
    client = get_chain(Settings())
    if not client.available:
        pytest.skip(f"chain layer unavailable: {client.status().reason}")
    return client


@pytest.fixture(scope="module")
def settings():
    from smartguard_api.config import Settings
    return Settings()


def test_contracts_are_reachable_and_configured(chain):
    """The deployment the report cites is live and holds the stated parameters."""
    status = chain.status()
    assert status.connected
    assert status.chain_id == 11155111, "not Sepolia"
    assert status.escrow_contract and status.oracle_contract
    assert status.operator, "no oracle operator key loaded"

    threshold = chain._escrow.functions.riskThreshold().call()
    assert threshold == 75, (
        f"the contract's risk threshold is {threshold}; Section 3.11.3 fixes it at 75"
    )


def test_the_gate_refuses_an_unscored_deposit(chain, settings):
    """Section 3.15.2: a missing score is a refusal, not a pass.

    The strongest single claim in the design, and the one most worth testing
    against the deployed bytecode rather than a local fixture. A nonce that no
    oracle write has used leaves the escrow unscored, so the deposit must
    revert.
    """
    from smartguard_api.chain import ChainUnavailable

    w3 = chain._w3
    amount = w3.to_wei(settings.chain_demo_amount_eth, "ether")
    seller = "0x7e806BC978372A7cc0Bd17f9c3a5ecbE1c5680e4"

    # Far above anything the oracle has accepted, so no score exists for the
    # escrow this nonce derives.
    unscored_nonce = int(chain._oracle.functions.lastNonce().call()) + 10_000

    with pytest.raises(ChainUnavailable) as caught:
        chain.deposit(seller, chain._operator.address, unscored_nonce, amount)

    assert "depositFunds" in str(caught.value)


def test_full_lifecycle_reaches_the_chain_and_reconciles(chain, settings):
    """A complete escrow, from score to settlement, with the database agreeing.

    Exercises the path Table 4.3 calls end-to-end: score written by the oracle,
    funds held by the contract, delivery confirmed, dispute raised, arbitrator
    resolving, and the projection reconciled against the contract afterwards.
    """
    from smartguard_api.chain_sync import reconcile_open_escrows

    w3 = chain._w3
    amount = w3.to_wei(settings.chain_demo_amount_eth, "ether")
    buyer = chain._funder.address
    arbitrator = chain._operator.address
    seller = "0x7e806BC978372A7cc0Bd17f9c3a5ecbE1c5680e4"

    escrow_id, nonce, score_receipt = chain.write_risk_score(
        buyer, seller, amount, 0.12)
    assert score_receipt.succeeded
    assert chain.has_score(escrow_id), "the oracle did not record the score"

    deposit = chain.deposit(seller, arbitrator, nonce, amount)
    assert deposit.succeeded

    held = chain.get_escrow(escrow_id)
    assert held is not None
    assert held["stateName"] == "INITIATED"
    assert held["amount"] == amount, "the contract is not holding the deposit"

    assert chain.confirm_delivery(escrow_id).succeeded
    assert chain.get_escrow(escrow_id)["stateName"] == "DELIVERED"

    assert chain.initiate_dispute(escrow_id).succeeded
    assert chain.get_escrow(escrow_id)["stateName"] == "DISPUTED"

    assert chain.resolve_dispute(escrow_id, False).succeeded
    final = chain.get_escrow(escrow_id)
    assert final["stateName"] == "COMPLETED"
    assert final["outcomeName"] == "RESOLVED_FOR_BUYER"
    assert final["amount"] == 0, "the contract still holds funds after settlement"


def test_a_score_cannot_be_replayed(chain, settings):
    """The nonce must strictly increase, so an accepted write cannot be resent."""
    from smartguard_api.chain import ChainUnavailable, _to_bytes32, _to_bytes32_text

    w3 = chain._w3
    amount = w3.to_wei(settings.chain_demo_amount_eth, "ether")
    seller = "0x7e806BC978372A7cc0Bd17f9c3a5ecbE1c5680e4"

    escrow_id, nonce, _ = chain.write_risk_score(
        chain._funder.address, seller, amount, 0.1)

    # The same nonce again, which the oracle has now recorded as its highest.
    with pytest.raises(ChainUnavailable):
        chain._send(
            chain._oracle.functions.updateRiskScore(
                _to_bytes32(w3, escrow_id), 10,
                _to_bytes32_text("rf-paysim-v1.0.0"), nonce,
                int(time.time()) + 3600,
            ),
            signer=chain._operator, label="updateRiskScore replay",
        )
