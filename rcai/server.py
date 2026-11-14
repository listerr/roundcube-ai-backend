# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""roundcube-ai-backend: starts the API server with uvicorn."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load


def main() -> None:
    ap = argparse.ArgumentParser(description="Backend for the Roundcube ai_agent plugin.")
    ap.add_argument("-c", "--config", help="config file (default: $RCAI_CONFIG or /etc/roundcube-ai-backend/config.toml)")
    args = ap.parse_args()
    try:
        cfg = load(args.config)
        cfg.check_server()
    except ConfigError as e:
        sys.exit(f"roundcube-ai-backend: {e}")

    logging.basicConfig(level=cfg.server.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    for item in cfg.insecure_transport():
        logging.getLogger("rcai").warning("INSECURE: %s (allow_insecure_transport = true)", item)
    # One process only: the agent runs and the login rate limit live in memory.
    uvicorn.run(
        create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        proxy_headers=True,
        forwarded_allow_ips=cfg.server.forwarded_allow_ips,
        log_level=cfg.server.log_level,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
