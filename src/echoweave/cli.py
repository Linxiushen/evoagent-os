from __future__ import annotations

import argparse
import os

import uvicorn

from echoweave.config import Settings


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="echoweave")
    subcommands = parser.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="run the realtime gateway")
    serve.add_argument("--host", default=settings.host)
    serve.add_argument("--port", type=int, default=settings.port)
    serve.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if args.command not in {None, "serve"}:
        parser.error("unknown command")
    bind_host = getattr(args, "host", settings.host)
    bind_port = getattr(args, "port", settings.port)
    if not 1 <= bind_port <= 65_535:
        parser.error("port must be between 1 and 65535")
    settings.validate_bind_host(bind_host)
    os.environ["ECHOWEAVE_HOST"] = bind_host
    os.environ["ECHOWEAVE_PORT"] = str(bind_port)
    uvicorn.run(
        "echoweave.app:app",
        host=bind_host,
        port=bind_port,
        reload=getattr(args, "reload", False),
        log_level=settings.log_level.lower(),
        ws_max_size=settings.max_ws_message_bytes,
    )


if __name__ == "__main__":
    main()
