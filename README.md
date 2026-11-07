# SmartGuard

**Blockchain-Enhanced AI-Powered Digital Escrow Transaction System for Fraud
Detection and Risk Analysis**

A prototype escrow platform in which a machine-learning risk score is a
precondition of blockchain-enforced fund custody, connected through an oracle.

Miva Open University Abuja · Department of Software Engineering

---

## Repository layout

```
contracts/   Solidity contracts, Hardhat tests, deployment scripts   [built]
web/         Next.js presentation layer and access control           [built]
ml/          Experiment pipeline: data, training, evaluation         [built]
api/         FastAPI service and oracle bridge                     [built]
docs/        Decision log and the evidence the report cites
```

## The four layers

| Layer | Implementation | Status |
| ----- | -------------- | ------ |
| Presentation | React 19 · Next.js App Router | Built |
| Application & intelligence | FastAPI · scikit-learn · PostgreSQL | Built |
| Oracle / integration | Web3.py bridge · event listener | Built |
| Blockchain | Solidity 0.8.20 · Ethereum Sepolia | Deployed and verified |

The contracts are live on Sepolia, verified, and deployed from a clean tree so
the bytecode corresponds to a commit:

| Contract | Address |
| -------- | ------- |
| `RiskOracleContract` | [`0x7E7Dc2C906ab520097c57B747f0d5b406E6a9aEe`](https://sepolia.etherscan.io/address/0x7E7Dc2C906ab520097c57B747f0d5b406E6a9aEe#code) |
| `EscrowContract` | [`0x02f7AF12B2Bcf967E9439acea560B60491B57CCD`](https://sepolia.etherscan.io/address/0x02f7AF12B2Bcf967E9439acea560B60491B57CCD#code) |

## Quick start

```bash
cp .env.example .env          # fill in before deploying or running production

# Contracts
cd contracts && npm install
npx hardhat test              # 62 tests
npx hardhat run scripts/deploy.js --network sepolia

# Presentation layer
cd web && npm install
npm run dev                   # http://localhost:3000

# Database. PostgreSQL 16; the seed creates the schema on first run.
brew install postgresql@16 && brew services start postgresql@16
psql -d postgres -c "CREATE ROLE smartguard LOGIN PASSWORD '<password>' CREATEDB;"
psql -d postgres -c "CREATE DATABASE smartguard OWNER smartguard;"

# Application and intelligence layer
cd api && python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m smartguard_api.seed
.venv/bin/uvicorn smartguard_api.main:app --reload --port 8000

# Load test — NFR-01 and NFR-05. Starts its own API, so stop the one above.
cd api && .venv/bin/python -m loadtest.run

# Interface evidence. Needs the web app and the API running.
cd web && node scripts/capture-screenshots.js

# Experiment pipeline
cd ml && make setup
python scripts/make_sample_paysim.py    # stand-in, until PaySim is downloaded
make all
```

Credentials for Sepolia and the PaySim download are covered step by step in
[docs/SETUP-KEYS.md](docs/SETUP-KEYS.md).

The password in `DATABASE_URL` must match the role's. The test suites do not
need PostgreSQL — `tests/conftest.py` points them at a temporary SQLite file —
but the load test does, because SQLite serialises writes and would report a
throughput figure that says nothing about the deployed stack.

## Evidence

`docs/evidence/` holds the artefacts the report cites, produced by tooling
rather than transcribed:

| File | Produced by | Closes |
| ---- | ----------- | ------ |
| `deployment.sepolia.json` | `scripts/deploy.js` | Appendix A — addresses, transaction hashes, block numbers, compiler settings, commit |
| `sepolia-measurements.json` | `scripts/measure-sepolia.js` | Appendix A — 15 live receipts with hashes, per-operation gas and submission-to-inclusion latency against a named provider |
| `state-cost-probe.json` | `scripts/probe-state-cost.js` | D-014 — the control showing the local/Sepolia gas divergence is confined to newly allocated storage |
| `gas-measurements.json` | `scripts/measure-gas.js` | Appendix A — 300 local receipts, so Table 4.2 has dispersion and the time-locked paths are measured |
| `gas-report.txt` | `REPORT_GAS=true npx hardhat test` | Correction 17 — per-call receipts with min, max and call counts |
| `contract-tests.txt` | `npx hardhat test` | Appendix A — the replay, nonce-ordering, expiry and operator-rotation controls listed as "not claimed as tested" |
| `slither-report.txt` | `slither .` | Appendix A — the full static-analysis output, not a description of it |
| `load-test.json` | `python -m loadtest.run` | Appendix A — NFR-01 and NFR-05 with the hardware, OS, worker configuration, concurrency schedule and run-to-run variability recorded |
| `screenshots/` | `node scripts/capture-screenshots.js` | Appendix A and Table A1 — interface evidence, including the server-side RBAC refusal |

The experiment's own artefacts live in `ml/outputs/` and `ml/artifacts/`, and
`ml/notebooks/SmartGuard_Implementation.ipynb` presents them executed, with
every number read from a written artefact and every code listing printed from
the module that ran it.

`docs/DECISIONS.md` records every deviation from Chapters One and Three, with
the reason and the report edit it requires. The verification review's central
finding was that the report and the artefact had drifted apart; that file exists
so it cannot happen silently again.

## Measured against reported

The gas figures in Table 4.2 were reported without execution receipts. The
measured values differ, and the measured ones are now the record:

| Function | Table 4.2 | Measured (avg) |
| -------- | --------: | -------------: |
| `depositFunds` | 187,432 | 154,875 |
| `updateRiskScore` | 47,215 | 116,277 |
| `resolveDispute` | 91,203 | 51,940 |
| `claimRefund` | 63,870 | 44,792 |
| `initiateDispute` | 74,556 | 36,920 |
| `confirmDelivery` | 68,210 | 36,860 |

`updateRiskScore` costs more than reported because this implementation stores
the model version, a nonce and a timestamp in order to satisfy the replay and
expiry controls of Section 3.15.2. That is a design cost, and it is explainable;
the remaining figures were estimates that measurement has now replaced.
