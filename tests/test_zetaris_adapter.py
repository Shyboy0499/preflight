import json
import uuid
from datetime import date

import httpx
import pytest

from app.adapters.zetaris import (
    DataSourceUnavailable,
    Query,
    UnsafeQuery,
    ZetarisRestSource,
    render,
)
from app.config import load_config

Q = Query("staff_by_id", "SELECT s.staff_id AS staff_id, s.weekly_hour_cap AS cap FROM {p}.staff s WHERE s.staff_id = :sid", ("staff_id", "cap"))
SECRET = "test-key-do-not-log"


def make(handler):
    cfg = load_config(sample_mode=False, today=date(2026, 10, 26), zetaris_api_key=SECRET, zetaris_org_id="1", zetaris_table_prefix="hospital")
    return ZetarisRestSource(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_render_types_and_prefix():
    sql = render(Query("q", "SELECT 1 AS x FROM {p}.t WHERE d = :d AND n = :n", ("x",)), {"d": date(2026, 10, 27), "n": 4}, "src")
    assert sql == "SELECT 1 AS x FROM src.t WHERE d = DATE '2026-10-27' AND n = 4"


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1; DROP TABLE x", "DELETE FROM {p}.staff", "SELECT 1 AS x FROM {p}.t WHERE a IN (SELECT 1) UNION SELECT 1 FROM (CREATE TABLE y)"],
)
def test_render_rejects_non_read_only(sql):
    with pytest.raises(UnsafeQuery):
        render(Query("bad", sql, ("x",)), {}, "p")


def test_rest_request_follows_zetaris_contract_and_coerces_strings():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [["S001", "38"]], "headers": ["staff_id", "cap"], "total": 1, "timeUsed": 5})

    result = make(handler).run(Q, {"sid": "S001"})
    assert seen["url"] == "http://localhost:8888/api/v1.0/sql-editor/sqls/run"
    assert seen["headers"]["authorization"] == f"Bearer {SECRET}"
    assert seen["headers"]["x-org-id"] == "1"
    uuid.UUID(seen["headers"]["x-request-id"])
    uuid.UUID(seen["body"]["queryId"])
    assert seen["body"]["sql"] == "SELECT s.staff_id AS staff_id, s.weekly_hour_cap AS cap FROM hospital.staff s WHERE s.staff_id = 'S001'"
    assert result.rows == [{"staff_id": "S001", "cap": 38}]
    assert result.source == "zetaris"


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, text="unauthorised"),
        lambda r: httpx.Response(200, json={"oops": True}),
        lambda r: httpx.Response(200, json={"data": [["only-one-col"]]}),
    ],
)
def test_rest_errors_become_unavailable_without_leaking_key(handler):
    with pytest.raises(DataSourceUnavailable) as err:
        make(handler).run(Q, {"sid": "S001"})
    assert SECRET not in str(err.value)


def test_rest_timeout_becomes_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DataSourceUnavailable, match="timed out"):
        make(handler).run(Q, {"sid": "S001"})


def test_missing_credentials_is_reported_by_name():
    with pytest.raises(DataSourceUnavailable, match="ZETARIS_API_KEY"):
        ZetarisRestSource(load_config(sample_mode=False, today=date(2026, 10, 26), zetaris_api_key="", zetaris_org_id="1"))


def test_metadata_query_takes_column_names_from_headers():
    show = Query("show_tables", "SHOW LIGHTNING TABLES NY_TAXI", ())
    body = {"data": [["taxi_zones", None, "csv"]], "headers": ["name", "materialized_table", "format"]}
    result = make(lambda r: httpx.Response(200, json=body)).run(show)
    assert result.rows == [{"name": "taxi_zones", "materialized_table": None, "format": "csv"}]


def test_metadata_query_without_headers_gets_positional_names():
    show = Query("show_tables", "SHOW LIGHTNING TABLES NY_TAXI", ())
    result = make(lambda r: httpx.Response(200, json={"data": [["taxi_zones", "csv"]]})).run(show)
    assert result.rows == [{"col_0": "taxi_zones", "col_1": "csv"}]


def test_headers_that_disagree_with_declared_columns_fail_the_query():
    body = {"data": [["38", "S001"]], "headers": ["cap", "staff_id"]}
    with pytest.raises(DataSourceUnavailable, match="returned columns"):
        make(lambda r: httpx.Response(200, json=body)).run(Q, {"sid": "S001"})
