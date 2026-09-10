"""Backup/restore only the fixed local test database, never business storage."""

import json
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import uuid4
from datetime import datetime, timezone

CONTAINER = "photo-agent-p4-test-postgres-1"
SOURCE = "photo_agent_batch1_test"


def run(*args):
    return subprocess.check_output(
        ["docker", "exec", CONTAINER, *args], text=True
    ).strip()


def query(database, sql):
    return run(
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "batch1",
        "-d",
        database,
        "-At",
        "-c",
        sql,
    )


def fingerprint(database):
    tables = query(
        database,
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename",
    ).splitlines()
    result = {}
    for table in tables:
        identifier = '"' + table.replace('"', '""') + '"'
        count, checksum = query(
            database,
            f"SELECT count(*),coalesce(md5(string_agg(md5(t::text),'' ORDER BY md5(t::text))),'empty') FROM public.{identifier} t",
        ).split("|")
        result[table] = {"rows": int(count), "content_checksum": checksum}
    return result


def main():
    suffix = uuid4().hex
    target = "photo_agent_p7_restore_" + suffix
    backup = "/tmp/photo-agent-p7-" + suffix + ".dump"
    evidence = (
        Path(__file__).resolve().parents[1]
        / ".project-to-act/tasks/S6-P7-002/evidence/restore.json"
    )
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE,
        "target": target,
        "scope": "isolated_postgresql_only",
        "production_restore": False,
        "passed": False,
    }
    started = perf_counter()
    created = False
    try:
        assert query(SOURCE, "SELECT current_database()") == SOURCE
        before = fingerprint(SOURCE)
        run(
            "pg_dump",
            "-U",
            "batch1",
            "-d",
            SOURCE,
            "-Fc",
            "--no-owner",
            "--no-acl",
            "-f",
            backup,
        )
        report["backup_ms"] = (perf_counter() - started) * 1000
        report["dump_sha256"] = run("sha256sum", backup).split()[0]
        run("createdb", "-U", "batch1", target)
        created = True
        restored_at = perf_counter()
        run(
            "pg_restore",
            "-U",
            "batch1",
            "-d",
            target,
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            backup,
        )
        report["restore_ms"] = (perf_counter() - restored_at) * 1000
        restored = fingerprint(target)
        after = fingerprint(SOURCE)
        report.update(
            tables=restored,
            source_unchanged=before == after,
            content_match=before == restored,
            migration_head=query(target, "SELECT version_num FROM alembic_version"),
        )
        report["passed"] = before == after == restored
    finally:
        if created:
            run("dropdb", "-U", "batch1", target)
        # Exact generated Linux container path, never a host path or source database.
        run("rm", "-f", backup)
        report["cleanup_complete"] = True
        report["total_ms"] = (perf_counter() - started) * 1000
        evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "tables": len(report.get("tables", {})),
                "head": report.get("migration_head"),
            }
        )
    )
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
