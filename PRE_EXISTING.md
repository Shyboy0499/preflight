# Pre-existing work and third-party material

All code in this repository was written for this hackathon entry.

## No third-party code copied

No source code was copied from other repositories. The following public documentation was read and
its **interfaces** (endpoint paths, headers, env var names, record shapes, service contracts) were
implemented from scratch:

| Source | Licence | What was used | Where |
|---|---|---|---|
| [zetaris/getting-started](https://github.com/zetaris/getting-started): `scripts/zetaris_api.py`, `scripts/zetaris_sql.py`, `docs/connections/client-setup.md`, `AGENTS.md` | Apache-2.0 | REST contract (`/sql-editor/sqls/run`, bearer key, `X-Org-ID`, UUID `X-Request-ID`, `queryId`/`sql`/`source`/`limit` body, `data` rows as strings), env var names, the read-only / one-statement-per-call rules | `app/adapters/zetaris.py`, `.env.example` |
| [Meterless/Meterless](https://github.com/Meterless/Meterless): `engines/hmem/HOW_TO_USE_H-MEM.md` | Apache-2.0 | Memory record shape, store/retrieval service contracts, eight-signal ranker weights, feedback deltas, reinjection format, ledger contract | `app/meterless/hmem.py` |
| [Meterless/Meterless](https://github.com/Meterless/Meterless): `engines/world-model/HOW_TO_USE_WORLD_MODEL.md` | Apache-2.0 | Entity/edge model, stable hash ID scheme, per-observation ingest stages, read-surface `build_prompt_context` contract | `app/meterless/world_model.py` |

Note: the Zetaris getting-started repository is licensed Apache-2.0 (not MIT).

## Libraries

Runtime dependencies are installed from PyPI and listed in `requirements.txt`: duckdb, httpx,
openai, python-dotenv, streamlit, pytest.
