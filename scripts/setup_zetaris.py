"""Register Preflight's three live sources in a local Zetaris instance. Idempotent.

    python scripts/setup_zetaris.py            # dry run: show state and planned changes
    python scripts/setup_zetaris.py --apply    # make the changes

Steps (each checks current state first and skips what already exists):
  1. Postgres container `preflight-postgres` on the Zetaris Docker network (leave system)
  2. `leave` table seeded with the scenario A rows (Priya's leave is toggled separately)
  3. roster/demand/staff/wards CSVs copied into the Zetaris customer-data volume
  4. Zetaris objects: PREFLIGHT_FILES (file source), preflight_pg (JDBC), PREFLIGHT_API (REST)
  5. Read-only verification through the app's adapter, and the ZETARIS_TABLES line to use

Prerequisite for step 4: the skills service is running
    uvicorn services.skills_api:app --host 127.0.0.1 --port 8077
"""

from __future__ import annotations

import argparse
import os
import time

import httpx

from _infra import (
    API_DB,
    BASE_LEAVE,
    CUSTOMER_DATA_DIR,
    FILE_DB,
    FILE_TABLES,
    LEAVE_COLUMNS,
    LEAVE_DDL,
    PG_CONTAINER,
    PG_DATASOURCE,
    PG_DB,
    PG_HOST_PORT,
    PG_IMAGE,
    PG_USER,
    PG_VOLUME,
    SAMPLE_A,
    SKILLS_PORT,
    SKILLS_URL_FOR_ZETARIS,
    ZETARIS_DRIVER,
    ZETARIS_NETWORK,
    docker,
    pg_connect,
    pg_password,
)

MASK = "********"


def say(kind: str, text: str) -> None:
    print(f"  [{kind:^7}] {text}")


# --- statements -----------------------------------------------------------------


def zetaris_statements(password: str) -> list[tuple[str, str, str]]:
    """(object kind, object name, SQL). One statement each, no semicolons."""
    out = [
        ("database", FILE_DB, f'CREATE LIGHTNING DATABASE {FILE_DB} DESCRIBE BY "Preflight roster, demand, staff, wards (synthetic CSV)"'),
    ]
    for t in FILE_TABLES:
        out.append((
            f"table:{FILE_DB}", t,
            f"CREATE LIGHTNING FILESTORE TABLE {t} FROM {FILE_DB} FORMAT CSV OPTIONS ("
            f'path "{CUSTOMER_DATA_DIR}/{t}.csv", header "true", inferSchema "true")',
        ))
    out += [
        ("datasource", PG_DATASOURCE,
         f'CREATE DATASOURCE {PG_DATASOURCE} DESCRIBE BY "Preflight leave system (synthetic Postgres)" OPTIONS ('
         f'jdbcdriver "org.postgresql.Driver", jdbcurl "jdbc:postgresql://{PG_CONTAINER}:5432/{PG_DB}", '
         f'username "{PG_USER}", password "{password}", fetchsize "1000")'),
        ("datasource-table", "leave", f'REGISTER DATASOURCE TABLE "leave" FROM {PG_DATASOURCE}'),
        ("database", API_DB, f'CREATE LIGHTNING DATABASE {API_DB} DESCRIBE BY "Preflight staff skills service (synthetic REST)"'),
        (f"table:{API_DB}", "staff_skills",
         f"CREATE LIGHTNING REST TABLE staff_skills FROM {API_DB} REQUEST ("
         f'endpoint "{SKILLS_URL_FOR_ZETARIS}", method "GET", http_encoding "URLENCODED", '
         f'response_type "JSON", insecure_ssl "false") HEADER () BODY ()'),
    ]
    return out


# --- state ------------------------------------------------------------------------


def pg_container_state() -> str:
    res = docker("ps", "-a", "--filter", f"name=^{PG_CONTAINER}$", "--format", "{{.State}}", check=False)
    return res.stdout.strip() or "absent"


def zetaris_state(src) -> dict[str, set[str]]:
    from app.adapters.zetaris import Query

    def names(sql: str) -> set[str]:
        return {str(r.get("name") or next(iter(r.values()))) for r in src.run(Query("setup_state", sql, ())).rows}

    dbs = names("SHOW LIGHTNING DATABASES")
    state = {"database": dbs, "datasource": names("SHOW DATASOURCES")}
    for db in (FILE_DB, API_DB):
        state[f"table:{db}"] = names(f"SHOW LIGHTNING TABLES {db}") if db in dbs else set()
    state["datasource-table"] = set()
    if PG_DATASOURCE in state["datasource"]:
        rows = src.run(Query("setup_state", f"SHOW DATASOURCE TABLES {PG_DATASOURCE}", ())).rows
        state["datasource-table"] = {str(v).split(".")[-1] for r in rows for v in r.values() if isinstance(v, str)}
    return state


def exists(state: dict[str, set[str]], kind: str, name: str) -> bool:
    return name.lower() in {n.lower() for n in state.get(kind, set())}


# --- steps ------------------------------------------------------------------------


def step_postgres(apply: bool) -> None:
    print("1. Postgres leave system")
    if apply and not pg_password(required=False):
        say("skip", "PREFLIGHT_PG_PASSWORD not set in .env.local; leave system not created")
        return
    st = pg_container_state()
    if st == "running":
        say("ok", f"container {PG_CONTAINER} running (127.0.0.1:{PG_HOST_PORT}, network {ZETARIS_NETWORK})")
        return
    if st != "absent":
        cmd = ["start", PG_CONTAINER]
    else:
        cmd = ["run", "-d", "--name", PG_CONTAINER, "--network", ZETARIS_NETWORK,
               "-p", f"127.0.0.1:{PG_HOST_PORT}:5432", "-v", f"{PG_VOLUME}:/var/lib/postgresql/data",
               "-e", f"POSTGRES_DB={PG_DB}", "-e", f"POSTGRES_USER={PG_USER}", "-e", "POSTGRES_PASSWORD", PG_IMAGE]
    say("plan", "docker " + " ".join(cmd) + ("   (POSTGRES_PASSWORD from PREFLIGHT_PG_PASSWORD)" if "run" in cmd else ""))
    if not apply:
        return
    docker(*cmd, env={**os.environ, "POSTGRES_PASSWORD": pg_password()})
    for _ in range(30):
        if docker("exec", PG_CONTAINER, "pg_isready", "-U", PG_USER, "-d", PG_DB, check=False).returncode == 0:
            say("done", "Postgres ready")
            return
        time.sleep(1)
    raise SystemExit("Postgres did not become ready in 30 s")


def step_leave_table(apply: bool) -> None:
    print("2. leave table (scenario A rows)")
    if pg_container_state() != "running" or not pg_password(required=False):
        say("plan", f"create table and insert {len(BASE_LEAVE)} rows once Postgres is up")
        return
    with pg_connect() as conn:
        present = conn.execute("SELECT to_regclass('public.leave') IS NOT NULL").fetchone()[0]
        count = conn.execute("SELECT count(*) FROM leave").fetchone()[0] if present else 0
        if present and count >= len(BASE_LEAVE):
            say("ok", f"leave table has {count} rows")
            return
        say("plan", f"CREATE TABLE IF NOT EXISTS leave; insert {len(BASE_LEAVE)} base rows (ON CONFLICT DO NOTHING)")
        if apply:
            conn.execute(LEAVE_DDL)
            cols = ", ".join(LEAVE_COLUMNS)
            for r in BASE_LEAVE:
                conn.execute(f"INSERT INTO leave ({cols}) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                             [r[c] for c in LEAVE_COLUMNS])
            say("done", "leave table ready")


def step_files(apply: bool) -> None:
    print(f"3. CSV files in Zetaris customer-data volume ({ZETARIS_DRIVER}:{CUSTOMER_DATA_DIR})")
    listing = docker("exec", ZETARIS_DRIVER, "ls", CUSTOMER_DATA_DIR, check=False).stdout.split()
    for t in FILE_TABLES:
        local = SAMPLE_A / f"{t}.csv"
        if f"{t}.csv" in listing:
            remote = docker("exec", ZETARIS_DRIVER, "cat", f"{CUSTOMER_DATA_DIR}/{t}.csv", check=False).stdout
            if remote == local.read_text(encoding="utf-8"):
                say("ok", f"{t}.csv up to date")
                continue
        say("plan", f"docker cp {local.relative_to(local.parents[3])} {ZETARIS_DRIVER}:{CUSTOMER_DATA_DIR}/{t}.csv")
        if apply:
            docker("exec", ZETARIS_DRIVER, "mkdir", "-p", CUSTOMER_DATA_DIR)
            docker("cp", str(local), f"{ZETARIS_DRIVER}:{CUSTOMER_DATA_DIR}/{t}.csv")
            say("done", f"{t}.csv copied")


def step_skills_service() -> bool:
    print(f"4a. skills service on 127.0.0.1:{SKILLS_PORT}")
    try:
        rows = httpx.get(f"http://127.0.0.1:{SKILLS_PORT}/health", timeout=3).json()["rows"]
        say("ok", f"running ({rows} skill rows); Zetaris will call {SKILLS_URL_FOR_ZETARIS}")
        return True
    except Exception:
        say("missing", f"start it: uvicorn services.skills_api:app --host 127.0.0.1 --port {SKILLS_PORT}")
        return False


def step_zetaris(src, apply: bool, skills_up: bool) -> None:
    print("4b. Zetaris objects")
    state = zetaris_state(src)
    for (kind, name, sql), (_, _, shown) in zip(zetaris_statements(pg_password(required=False)), zetaris_statements(MASK)):
        if exists(state, kind, name):
            say("ok", f"{kind} {name} exists")
            continue
        say("plan", shown)
        if not apply:
            continue
        if name == "staff_skills" and not skills_up:
            say("skip", "skills service not running")
            continue
        if kind.startswith("datasource") and (not pg_password(required=False) or pg_container_state() != "running"):
            say("skip", "Postgres leave system not set up yet")
            continue
        try:
            src.execute_admin(sql)
            say("done", f"{kind} {name}")
        except Exception as exc:  # report and continue; re-running picks up where it stopped
            if "already exists" in str(exc).lower():
                say("ok", f"{kind} {name} already exists")
            else:
                say("FAILED", f"{kind} {name}: {str(exc).replace(pg_password(required=False) or MASK, MASK)[:300]}")


def step_verify(src) -> None:
    from app.adapters.zetaris import DataSourceUnavailable, Query

    print("5. Verify (read-only, through the app adapter)")
    candidates = {t: [f"{FILE_DB}.{t}"] for t in FILE_TABLES}
    candidates["leave"] = [f"{PG_DATASOURCE}.leave", f"{PG_DATASOURCE}.public.leave"]
    candidates["staff_skills"] = [f"{API_DB}.staff_skills"]
    resolved = {}
    for table, names in candidates.items():
        for qualified in names:
            try:
                n = src.run(Query("verify_count", f"SELECT COUNT(*) AS n FROM {qualified}", ("n",))).rows[0]["n"]
                say("ok", f"{qualified}: {n} rows")
                resolved[table] = qualified
                break
            except DataSourceUnavailable as exc:
                last = str(exc)[:160]
        else:
            say("missing", f"{table}: not queryable yet ({last})")
    if len(resolved) == 6:
        print("\nSet in .env.local:\n  SAMPLE_MODE=false\n  DEMO_TODAY=2026-10-26\n  ZETARIS_TABLES=" +
              ",".join(f"{t}={q}" for t, q in resolved.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args()

    from app.adapters.zetaris import ZetarisRestSource
    from app.config import SAMPLE_TODAY, load_config

    src = ZetarisRestSource(load_config(sample_mode=False, today=SAMPLE_TODAY))
    print("DRY RUN (nothing changes; add --apply)\n" if not args.apply else "APPLYING\n")
    step_postgres(args.apply)
    step_leave_table(args.apply)
    step_files(args.apply)
    skills_up = step_skills_service()
    step_zetaris(src, args.apply, skills_up)
    step_verify(src)


if __name__ == "__main__":
    main()
