from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from echoweave.config import Settings


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="echoweave")
    subcommands = parser.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="run the realtime gateway")
    serve.add_argument("--host", default=settings.host)
    serve.add_argument("--port", type=int, default=settings.port)
    serve.add_argument(
        "--ssl-certfile",
        type=Path,
        default=settings.tls_certfile,
    )
    serve.add_argument(
        "--ssl-keyfile",
        type=Path,
        default=settings.tls_keyfile,
    )
    serve.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if args.command not in {None, "serve"}:
        parser.error("unknown command")
    bind_host = getattr(args, "host", settings.host)
    bind_port = getattr(args, "port", settings.port)
    tls_certfile = getattr(args, "ssl_certfile", settings.tls_certfile)
    tls_keyfile = getattr(args, "ssl_keyfile", settings.tls_keyfile)
    if not 1 <= bind_port <= 65_535:
        parser.error("port must be between 1 and 65535")
    settings.validate_bind_host(
        bind_host,
        tls_certfile=tls_certfile,
        tls_keyfile=tls_keyfile,
    )
    trusted_proxy_ips = settings.normalized_trusted_proxy_ips
    os.environ["ECHOWEAVE_HOST"] = bind_host
    os.environ["ECHOWEAVE_PORT"] = str(bind_port)
    if tls_certfile is not None and tls_keyfile is not None:
        os.environ["ECHOWEAVE_TLS_CERTFILE"] = str(tls_certfile.resolve())
        os.environ["ECHOWEAVE_TLS_KEYFILE"] = str(tls_keyfile.resolve())
    uvicorn.run(
        "echoweave.app:app",
        host=bind_host,
        port=bind_port,
        reload=getattr(args, "reload", False),
        log_level=settings.log_level.lower(),
        ws_max_size=settings.max_ws_message_bytes,
        proxy_headers=bool(trusted_proxy_ips),
        forwarded_allow_ips=list(trusted_proxy_ips),
        ssl_certfile=(str(tls_certfile) if tls_certfile is not None else None),
        ssl_keyfile=(str(tls_keyfile) if tls_keyfile is not None else None),
    )


if __name__ == "__main__":
    main()
