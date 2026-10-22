"""Zetaris adapter: every fact Preflight reasons about comes through here.

Two interchangeable sources run the *same rendered SQL text*:

* ZetarisRestSource  - live mode. Direct REST API of a Zetaris instance
  (POST {ZETARIS_REST_URL}/sql-editor/sqls/run, bearer API key, X-Org-ID,
  UUID X-Request-ID), as documented in github.com/zetaris/getting-started
  (docs/connections/client-setup.md). Written for this project; no code copied.
* DuckDBSampleSource - SAMPLE_MODE. DuckDB views over data/sample/<scenario>/*.csv
  exposed under the same `<prefix>.<table>` names.

Rules from the Zetaris AGENTS.md that this adapter enforces for every call:
read-only statements only (SELECT / WITH / SHOW / DESCRIBE), one statement per
call, no trailing semicolon, qualified table names. The API key is never logged.

Failure policy: any transport error, timeout or bad response raises
DataSourceUnavailable. Callers that gate actions treat that as a failed check
(fail closed). There is deliberately no silent fallback from live Zetaris to the
sample CSVs: acting on stale demo data would be worse than not acting.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import Config


class DataSourceUnavailable(RuntimeError):
    """The data source could not answer. Gated actions must not fire."""


class UnsafeQuery(ValueError):
    """The SQL is not a single read-only statement, or a parameter is unsafe."""


@dataclass(frozen=True)
class Query:
    """A named, parameterised SQL statement.

    `sql` uses `{table}` placeholders (e.g. `{leave}`) resolved per table to a
    qualified name, and `:name` for parameters. Every
    selected column must be aliased in the same order as `columns`, because the
    Zetaris REST API returns rows as positional lists (checked against the
    response's `headers`). Empty `columns` (metadata commands such as SHOW)
    takes the names from `headers`.
    """

    name: str
    sql: str
    columns: tuple[str, ...]


@dataclass
class QueryResult:
    query: str
    source: str
    sql: str
    rows: list[dict[str, Any]]
    query_id: str
    elapsed_ms: int
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query": self.query,
            "query_id": self.query_id,
            "sql": self.sql,
            "row_count": len(self.rows),
            "fetched_at": self.fetched_at,
        }


class SqlSource(Protocol):
    name: str
    tables: dict[str, str]

    def run(self, query: Query, params: dict[str, Any] | None = None) -> QueryResult: ...


# --- rendering and guards ----------------------------------------------------

_READ_ONLY_START = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE)\b", re.I)
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|REPLACE|COPY|ATTACH|CALL|EXEC)\b",
    re.I,
)
_PARAM = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)\b")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9 _\-.:/]{0,120}$")
_SAFE_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def sql_literal(value: Any) -> str:
    """Render one parameter as a typed SQL literal. Rejects anything unusual."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        if not _SAFE_TEXT.match(value):
            raise UnsafeQuery(f"parameter value not allowed: {value!r}")
        return "'" + value.replace("'", "''") + "'"
    raise UnsafeQuery(f"unsupported parameter type: {type(value).__name__}")


def assert_read_only(sql: str) -> None:
    if ";" in sql:
        raise UnsafeQuery("one statement per call, no semicolons")
    if not _READ_ONLY_START.match(sql):
        raise UnsafeQuery("only SELECT / WITH / SHOW / DESCRIBE are allowed")
    if _WRITE_WORDS.search(sql):
        raise UnsafeQuery("statement contains a write keyword")


_TABLE = re.compile(r"\{([a-z_]+)\}")


def render(query: Query, params: dict[str, Any] | None, tables: dict[str, str]) -> str:
    params = params or {}

    def table(match: re.Match[str]) -> str:
        qualified = tables.get(match.group(1))
        if qualified is None:
            raise UnsafeQuery(f"unknown table {{{match.group(1)}}} in {query.name}")
        if not _SAFE_PREFIX.match(qualified):
            raise UnsafeQuery(f"bad qualified table name: {qualified!r}")
        return qualified

    def param(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise UnsafeQuery(f"missing parameter :{key} for {query.name}")
        return sql_literal(params[key])

    sql = _TABLE.sub(table, query.sql)
    sql = " ".join(_PARAM.sub(param, sql).split())
    assert_read_only(sql)
    return sql


def _normalise(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def _coerce_rest_value(value: Any) -> Any:
    """Zetaris REST returns values as strings; decimals may use exponent form."""
    if isinstance(value, str) and _NUMBER.match(value):
        number = float(value)
        return int(number) if number.is_integer() and "." not in value and "e" not in value.lower() else number
    return value


# --- sources -----------------------------------------------------------------


class DuckDBSampleSource:
    """In-process stand-in for Zetaris over the synthetic CSVs."""

    name = "duckdb-sample"

    SCHEMA = "preflight"

    def __init__(self, csv_dir: Path):
        import duckdb

        self.csv_dir = csv_dir
        self._con = duckdb.connect(":memory:")
        self._con.execute(f"CREATE SCHEMA {self.SCHEMA}")
        csvs = sorted(csv_dir.glob("*.csv"))
        if not csvs:
            raise DataSourceUnavailable(f"no CSV files in {csv_dir}")
        for path in csvs:
            self._con.execute(
                f"CREATE VIEW {self.SCHEMA}.{path.stem} AS "
                f"SELECT * FROM read_csv('{path.as_posix()}', header = true, auto_detect = true)"
            )
        self.tables = {path.stem: f"{self.SCHEMA}.{path.stem}" for path in csvs}

    def run(self, query: Query, params: dict[str, Any] | None = None) -> QueryResult:
        sql = render(query, params, self.tables)
        started = time.perf_counter()
        try:
            cursor = self._con.execute(sql)
            raw = cursor.fetchall()
        except Exception as exc:  # duckdb raises several error types
            raise DataSourceUnavailable(f"{self.name}: {query.name} failed: {exc}") from exc
        rows = [dict(zip(query.columns, (_normalise(v) for v in row))) for row in raw]
        return QueryResult(
            query=query.name,
            source=f"{self.name}:{self.csv_dir.name}",
            sql=sql,
            rows=rows,
            query_id=str(uuid.uuid4()),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class ZetarisRestSource:
    """Live Zetaris over its direct REST API."""

    name = "zetaris"
    RUN_PATH = "/sql-editor/sqls/run"

    def __init__(self, cfg: Config, client: httpx.Client | None = None):
        missing = [n for n, v in (("ZETARIS_API_KEY", cfg.zetaris_api_key), ("ZETARIS_ORG_ID", cfg.zetaris_org_id)) if not v]
        if missing:
            raise DataSourceUnavailable("Zetaris not configured, set in .env.local: " + ", ".join(missing))
        if not cfg.zetaris_org_id.isdigit():
            raise DataSourceUnavailable("ZETARIS_ORG_ID must be the numeric organisation ID")
        self.tables = dict(cfg.zetaris_tables)
        self.base_url = cfg.zetaris_rest_url
        self.limit = cfg.zetaris_query_limit
        self._org = cfg.zetaris_org_id
        self._key = cfg.zetaris_api_key
        self._client = client or httpx.Client(timeout=cfg.zetaris_timeout_s)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "X-Org-ID": self._org,
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _post(self, sql: str, name: str, query_id: str) -> Any:
        body = {"queryId": query_id, "sql": sql, "source": "SqlEditor", "limit": self.limit}
        try:
            response = self._client.post(self.base_url + self.RUN_PATH, json=body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise DataSourceUnavailable(f"zetaris: {name} timed out") from exc
        except httpx.HTTPError as exc:
            raise DataSourceUnavailable(f"zetaris: cannot reach {self.base_url}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            hint = {401: "bad or expired API key", 400: "check ZETARIS_ORG_ID / request IDs / SQL"}.get(response.status_code, "")
            raise DataSourceUnavailable(f"zetaris: {name} HTTP {response.status_code} {hint}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise DataSourceUnavailable("zetaris: response was not JSON") from exc

    def execute_admin(self, sql: str) -> list[dict[str, Any]]:
        """Run one setup statement (CREATE / REGISTER ...) without the read-only guard.

        For scripts/setup_zetaris.py only. The agent never calls this; a test
        enforces that nothing under app/ does.
        """
        if ";" in sql.strip().rstrip(";"):
            raise UnsafeQuery("one statement per call")
        statement = " ".join(sql.strip().rstrip(";").split())
        payload = self._post(statement, "admin", str(uuid.uuid4()))
        return self._rows(payload, Query("admin", statement, ())) if isinstance(payload, dict) and "data" in payload else []

    def run(self, query: Query, params: dict[str, Any] | None = None) -> QueryResult:
        sql = render(query, params, self.tables)
        query_id = str(uuid.uuid4())
        started = time.perf_counter()
        payload = self._post(sql, query.name, query_id)
        rows = self._rows(payload, query)
        return QueryResult(
            query=query.name,
            source=self.name,
            sql=sql,
            rows=rows,
            query_id=query_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _rows(payload: Any, query: Query) -> list[dict[str, Any]]:
        """Map a run response to dicts.

        Confirmed against a local instance (2026-10-22): the body is
        {"data": [[...], ...], "headers": [col, ...], "total": n, "timeUsed": ms}
        with values as strings. When `headers` is present it must match the
        declared columns (case-insensitive), otherwise evidence could be
        misattributed, so the query fails instead.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if data is None:
            raise DataSourceUnavailable(f"zetaris: {query.name} response has no 'data'")
        headers = payload.get("headers")
        if query.columns:
            names = list(query.columns)
            if isinstance(headers, list) and [str(h).lower() for h in headers] != [c.lower() for c in names]:
                raise DataSourceUnavailable(f"zetaris: {query.name} returned columns {headers}, expected {names}")
        elif isinstance(headers, list):
            names = [str(h) for h in headers]
        else:
            names = []
        rows: list[dict[str, Any]] = []
        for raw in data:
            if isinstance(raw, dict):
                values = [raw.get(c) for c in names] if query.columns else list(raw.values())
            elif isinstance(raw, (list, tuple)):
                values = list(raw)
            else:
                raise DataSourceUnavailable(f"zetaris: {query.name} unexpected row type {type(raw).__name__}")
            keys = names or [f"col_{i}" for i in range(len(values))]
            if len(values) != len(keys):
                raise DataSourceUnavailable(f"zetaris: {query.name} row shape does not match {keys}")
            rows.append({k: _coerce_rest_value(v) for k, v in zip(keys, values)})
        return rows


HEALTH = Query("health_select_1", "SELECT 1 AS ok", ("ok",))


def make_source(cfg: Config) -> SqlSource:
    if cfg.sample_mode:
        return DuckDBSampleSource(cfg.data_dir / cfg.sample_scenario)
    return ZetarisRestSource(cfg)
