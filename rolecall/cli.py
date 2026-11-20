"""Command line: `rolecall serve`, `rolecall demo`, `rolecall ingest`, `rolecall process`, `rolecall tick`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="rolecall")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="dashboard + background worker")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    sub.add_parser("init", help="create the database")
    d = sub.add_parser("demo", help="generate + ingest the labeled demo source")
    d.add_argument("--process", action="store_true", help="also transcribe, rank and render now")
    i = sub.add_parser("ingest", help="ingest a local file or authorized URL for a creator")
    i.add_argument("creator_slug")
    i.add_argument("path_or_url")
    p = sub.add_parser("process", help="process a source now")
    p.add_argument("source_id", type=int)
    sub.add_parser("tick", help="run one worker cycle (ingest/process/publish)")
    sub.add_parser("status", help="publishing status per platform")
    st = sub.add_parser("set", help="change a setting, e.g. rolecall set packages '[{...}]'")
    st.add_argument("key")
    st.add_argument("value", help="JSON value")
    args = ap.parse_args(argv)

    db.init_db()
    if args.cmd == "init":
        print("database ready")
    elif args.cmd == "serve":
        import uvicorn

        from .web.app import app

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    elif args.cmd == "demo":
        from . import demo, pipeline
        from .config import get_config

        cid = demo.ensure_demo_creator()
        out = demo.make_demo(get_config().dir("tmp") / "rolecall_demo.mp4")
        sid = pipeline.ingest_file(cid, out, kind="demo", title="DEMO: traffic stop + court (synthetic)",
                                   is_demo=True, move=True)
        print(f"demo source {sid}")
        if args.process:
            print("clips:", pipeline.process_source(sid))
    elif args.cmd == "ingest":
        from . import pipeline

        c = db.q1("SELECT id FROM creators WHERE slug=?", (args.creator_slug,))
        if not c:
            print(f"no creator '{args.creator_slug}'", file=sys.stderr)
            return 2
        if args.path_or_url.startswith(("http://", "https://")):
            sid = pipeline.ingest_url(c["id"], args.path_or_url)
        else:
            sid = pipeline.ingest_file(c["id"], args.path_or_url)
        print(f"source {sid}")
    elif args.cmd == "process":
        from . import pipeline

        print("clips:", pipeline.process_source(args.source_id))
    elif args.cmd == "tick":
        from . import scheduler

        scheduler.tick()
        print(scheduler.debug_dump())
    elif args.cmd == "set":
        db.set_setting(args.key, json.loads(args.value))
        print(f"{args.key} = {db.get_setting(args.key)!r}")
    elif args.cmd == "status":
        from . import scheduler

        print(json.dumps(scheduler.status_snapshot(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
