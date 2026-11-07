"""Command line: ``bench serve | worker | ris-mcp | migrate | backup | check-providers``."""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

log = logging.getLogger("bench")


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from bench.web.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True)
    return 0


def _worker(args: argparse.Namespace) -> int:
    from bench import prompts
    from bench.db import make_engine, migrate
    from bench.llm.http import make_http_client
    from bench.settings import get_settings
    from bench.worker import Worker

    settings = get_settings()
    engine = make_engine(settings.db_url)
    migrate(engine)

    async def go() -> None:
        async with make_http_client() as client:
            worker = Worker(engine, settings, client, poll_seconds=args.interval)
            try:
                await worker.run_forever()
            finally:
                await worker.aclose()

    from sqlmodel import Session

    with Session(engine) as db:
        prompts.seed(db)
        db.commit()
    asyncio.run(go())
    return 0


def _ris_mcp(args: argparse.Namespace) -> int:
    import uvicorn

    from bench.rismcp.server import create_app
    from bench.settings import RIS_MCP_API_KEY, get_settings

    settings = get_settings()
    app = create_app(settings.secret(RIS_MCP_API_KEY), settings.ris_mcp_allowed_hosts)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _migrate(_args: argparse.Namespace) -> int:
    from bench.db import make_engine, migrate
    from bench.settings import get_settings

    migrate(make_engine(get_settings().db_url))
    print("database is up to date")
    return 0


def _backup(args: argparse.Namespace) -> int:
    from bench.backup import backup_sqlite
    from bench.settings import get_settings

    settings = get_settings()
    target = args.target or settings.data_dir / "backups"
    print(backup_sqlite(settings.db_url, target, args.label))
    return 0


def _all(args: argparse.Namespace) -> int:
    from bench.backup import store_from_settings
    from bench.flymachines import FlyMachines
    from bench.settings import FLY_API_TOKEN, get_settings
    from bench.supervisor import Supervisor, default_commands

    settings = get_settings()
    commands = default_commands(args.port, args.mcp_port)
    bridge = None
    if settings.fly_bridge_app:
        token = settings.secret(FLY_API_TOKEN)
        if token:
            bridge = FlyMachines(
                settings.fly_api_url,
                token,
                settings.fly_bridge_app,
                settings.fly_bridge_health_url,
            )
        else:
            logging.error("%s not set: %s is not started", FLY_API_TOKEN, settings.fly_bridge_app)
    store = store_from_settings(settings)
    return Supervisor(settings, commands, store, bridge=bridge).run()


def _restore(args: argparse.Namespace) -> int:
    from bench.backup import apply_pending_restore, stage_restore, store_from_settings
    from bench.settings import get_settings

    settings = get_settings()
    store = store_from_settings(settings)
    if args.list:
        if store is None:
            print("no backup bucket configured (BENCH_BACKUP_BUCKET)", file=sys.stderr)
            return 1
        for b in store.list():
            print(f"{b.key}  {b.size / 1e6:8.1f} MB")
        return 0
    if not args.source:
        print("give a backup (file or bucket key) or --list", file=sys.stderr)
        return 2
    staged = stage_restore(settings, args.source, store)
    if args.now:
        apply_pending_restore(settings.db_url)
        print(f"restored {args.source}; the old database is kept beside it")
    else:
        print(f"staged {args.source} as {staged}; it replaces the database at the next")
        print("start of `bench all` (fly.io: fly machine restart)")
    return 0


def _import_cases(args: argparse.Namespace) -> int:
    from sqlmodel import Session

    from bench import cases
    from bench.db import make_engine, migrate
    from bench.models import ImportRun
    from bench.ris.client import RisClient
    from bench.settings import get_settings

    engine = make_engine(get_settings().db_url)
    migrate(engine)
    with Session(engine) as db:
        run = ImportRun(params={"date_from": args.date_from.isoformat(), "cli": True})
        db.add(run)
        db.commit()
        assert run.id is not None
        run_id = run.id

    async def go() -> None:
        ris = RisClient()
        try:
            await cases.run_import(
                engine, ris, run_id, args.date_from, args.date_to, args.procedural, None
            )
        finally:
            await ris.aclose()

    asyncio.run(go())
    with Session(engine) as db:
        done = db.get(ImportRun, run_id)
        assert done is not None
        print("\n".join(done.log))
        print(f"{done.imported} imported, {done.skipped} skipped, {done.failed} failed")
        return 0 if done.status == "done" and not done.failed else 1


def _export(args: argparse.Namespace) -> int:
    from sqlmodel import Session

    from bench import export
    from bench.db import make_engine
    from bench.providers import load_config, resolve
    from bench.settings import get_settings

    settings = get_settings()
    config = resolve(load_config(settings.config_path), settings)
    with Session(make_engine(settings.db_url)) as db:
        content = export.build(db, config, raw=args.raw)
    args.out.write_bytes(content)
    print(f"{args.out}: {len(content)} bytes")
    return 0


def _check_providers(args: argparse.Namespace) -> int:
    from bench.llm.http import make_http_client
    from bench.llm.ping import ping
    from bench.providers import load_config, resolve
    from bench.settings import get_settings

    settings = get_settings()
    models = resolve(load_config(settings.config_path), settings)
    if args.model:
        models = {k: v for k, v in models.items() if k in args.model}

    async def run() -> int:
        failed = 0
        async with make_http_client() as client:
            for rm in models.values():
                if not rm.available:
                    print(f"{rm.key:24} SKIP  secret {rm.provider.api_key_secret} missing")
                    continue
                res = await ping(rm, settings, client)
                failed += not res.ok
                detail = res.answer if res.ok else res.error
                extra = f" model={res.model_reported}" if res.model_reported else ""
                extra += f" provider={res.provider_reported}" if res.provider_reported else ""
                extra += " MODEL-MISMATCH" if res.model_mismatch else ""
                state = "OK  " if res.ok else "FAIL"
                print(f"{rm.key:24} {state} {res.duration_ms:>6} ms  {detail!r}{extra}")
        return 1 if failed else 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the web application")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_serve)

    p = sub.add_parser("worker", help="run the job worker")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    p.set_defaults(func=_worker)

    p = sub.add_parser("ris-mcp", help="run the RIS legislation MCP server (condition C)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_ris_mcp)

    p = sub.add_parser(
        "all", help="web, worker and ris-mcp in one machine, idle stop, backups (fly.io)"
    )
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--mcp-port", type=int, default=8001)
    p.set_defaults(func=_all)

    p = sub.add_parser("migrate", help="bring the database schema up to date")
    p.set_defaults(func=_migrate)

    p = sub.add_parser("backup", help="online backup of the SQLite database")
    p.add_argument("--target", type=Path)
    p.add_argument("--label")
    p.set_defaults(func=_backup)

    p = sub.add_parser("restore", help="restore the database from a backup")
    p.add_argument("source", nargs="?", help="backup file or key in the backup bucket")
    p.add_argument("--list", action="store_true", help="list the backups in the bucket")
    p.add_argument(
        "--now", action="store_true", help="replace at once (web and worker must be stopped)"
    )
    p.set_defaults(func=_restore)

    p = sub.add_parser("import-cases", help="import VfGH regulation reviews from the RIS")
    p.add_argument("--from", dest="date_from", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="date_to", type=date.fromisoformat)
    p.add_argument("--procedural", action="store_true", help="also procedural rulings")
    p.set_defaults(func=_import_cases)

    p = sub.add_parser("export", help="data, metrics and dashboard as one ZIP")
    p.add_argument("out", type=Path)
    p.add_argument("--raw", action="store_true", help="include the raw HTTP exchanges")
    p.set_defaults(func=_export)

    p = sub.add_parser("check-providers", help="send a short test prompt to each model")
    p.add_argument("model", nargs="*", help="model keys (default: all)")
    p.set_defaults(func=_check_providers)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
