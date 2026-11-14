# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""Configuration: one TOML file, by default /etc/roundcube-ai-backend/config.toml.

The environment variable RCAI_CONFIG or the --config option selects
another file. Every key has a default except the OpenAI model and key and
the IMAP master user, which the server needs to start.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field, fields
from urllib.parse import urlsplit

CONFIG_ENV = "RCAI_CONFIG"
DEFAULT_PATH = "/etc/roundcube-ai-backend/config.toml"


class ConfigError(Exception):
    pass


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8085
    # Sub-URL of the reverse proxy, e.g. "/rcapi". Requests work with and
    # without the prefix, so the proxy can keep it or strip it.
    base_path: str = "/rcapi"
    # Browser origins that can call the API. Empty = same origin only, which
    # is correct when the API is a sub-URL of the webmail host.
    cors_origins: list[str] = field(default_factory=list)
    forwarded_allow_ips: str = "127.0.0.1"
    log_level: str = "info"
    max_body_bytes: int = 2_000_000     # larger request bodies get 413
    # Allow IMAP without TLS or certificate check, and http:// for the model
    # server, to hosts that are not loopback addresses.
    allow_insecure_transport: bool = False


@dataclass
class DatabaseConfig:
    path: str = "/var/lib/roundcube-ai-backend/rcai.db"


@dataclass
class ImapConfig:
    host: str = "localhost"
    port: int = 993
    security: str = "ssl"          # ssl, starttls or none
    tls_verify: bool = True
    timeout: float = 30.0
    master_user: str = ""
    master_password: str = ""
    master_separator: str = "*"


@dataclass
class AuthConfig:
    session_ttl: int = 8 * 3600     # seconds; the plugin logs in again on 401
    max_failures: int = 5           # failed logins per login name and address ...
    max_failures_ip: int = 20       # ... or per client address ...
    failure_window: int = 300       # ... in this many seconds, then 429
    max_concurrent_logins: int = 4  # password checks at the same time, then 429
    default_domain: str = ""        # address = login@default_domain when login has no @


@dataclass
class AccessConfig:
    # AI is on by default for every mailbox, or for the mailboxes at these
    # domains. A setting made with roundcube-ai-admin overrides the default.
    enable_all: bool = False
    enable_domains: list[str] = field(default_factory=list)


@dataclass
class OpenAIConfig:
    api_key: str = ""               # empty = the environment variable OPENAI_API_KEY
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    timeout: float = 300.0
    # Extra parameters for every request, e.g. {reasoning_effort = "low"}.
    params: dict = field(default_factory=dict)


@dataclass
class AgentConfig:
    max_steps: int = 15             # model calls per question
    max_concurrent: int = 4         # agent runs at the same time, then 429
    max_per_mailbox: int = 1        # agent runs at the same time for one mailbox
    run_timeout: float = 600.0      # seconds for one agent run, all steps
    daily_tokens: int = 0           # tokens per mailbox per day; 0 = no limit
    max_turns: int = 400            # stored turns in one conversation
    max_tools: int = 32             # client tools in one request
    max_tool_bytes: int = 32768     # all client tool definitions, as JSON
    ping_interval: float = 10.0     # seconds between ": ping" lines in the stream
    read_max_chars: int = 30000     # text of one message or attachment for the model
    search_limit: int = 50          # maximum messages in one search result
    timezone: str = ""              # e.g. "Europe/London"; empty = the server time zone


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    imap: ImapConfig = field(default_factory=ImapConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    def insecure_transport(self) -> list[str]:
        """Settings that send secrets without verified TLS to a non-loopback host."""
        out = []
        imap_local = _loopback(self.imap.host)
        if self.imap.security == "none" and not imap_local:
            out.append("[imap] security = none")
        if not self.imap.tls_verify and not imap_local:
            out.append("[imap] tls_verify = false")
        url = urlsplit(self.openai.base_url)
        if url.scheme != "https" and not _loopback(url.hostname or ""):
            out.append("[openai] base_url does not use https")
        return out

    def check_server(self) -> None:
        """Settings that the API server needs and the admin CLI does not."""
        if not self.openai.model:
            raise ConfigError("[openai] model is not set")
        if not self.openai.api_key:
            raise ConfigError("[openai] api_key is not set (or set OPENAI_API_KEY)")
        if not self.imap.master_user or not self.imap.master_password:
            raise ConfigError("[imap] master_user and master_password are not set")


SECTIONS = {f.name: f.type for f in fields(Config)}

# (section, key): (lowest, highest). Values outside the range stop the start.
RANGES = {
    ("server", "port"): (1, 65535),
    ("server", "max_body_bytes"): (10_000, 50_000_000),
    ("imap", "port"): (1, 65535),
    ("imap", "timeout"): (1, 600),
    ("auth", "session_ttl"): (300, 30 * 86400),
    ("auth", "max_failures"): (1, 100),
    ("auth", "max_failures_ip"): (1, 10_000),
    ("auth", "failure_window"): (10, 86400),
    ("auth", "max_concurrent_logins"): (1, 100),
    ("openai", "timeout"): (5, 3600),
    ("agent", "max_steps"): (1, 100),
    ("agent", "max_concurrent"): (1, 1000),
    ("agent", "max_per_mailbox"): (1, 100),
    ("agent", "run_timeout"): (10, 7200),
    ("agent", "ping_interval"): (0.01, 60),
    ("agent", "read_max_chars"): (1000, 1_000_000),
    ("agent", "search_limit"): (1, 1000),
    ("agent", "daily_tokens"): (0, 10**10),
    ("agent", "max_turns"): (10, 100_000),
    ("agent", "max_tools"): (0, 128),
    ("agent", "max_tool_bytes"): (1000, 1_000_000),
}


def _loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_ranges(cfg: "Config") -> None:
    for (section, key), (lo, hi) in RANGES.items():
        v = getattr(getattr(cfg, section), key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"[{section}] {key} must be a number")
        if not lo <= v <= hi:
            raise ConfigError(f"[{section}] {key} must be between {lo} and {hi}")


def _section(cls, data: dict, name: str):
    if not isinstance(data, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"[{name}] unknown keys: {', '.join(unknown)}")
    return cls(**data)


def from_dict(data: dict) -> Config:
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(unknown)}")
    cfg = Config(
        server=_section(ServerConfig, data.get("server", {}), "server"),
        database=_section(DatabaseConfig, data.get("database", {}), "database"),
        imap=_section(ImapConfig, data.get("imap", {}), "imap"),
        auth=_section(AuthConfig, data.get("auth", {}), "auth"),
        access=_section(AccessConfig, data.get("access", {}), "access"),
        openai=_section(OpenAIConfig, data.get("openai", {}), "openai"),
        agent=_section(AgentConfig, data.get("agent", {}), "agent"),
    )

    bp = cfg.server.base_path.strip().rstrip("/")
    if bp and not bp.startswith("/"):
        bp = "/" + bp
    cfg.server.base_path = bp
    if cfg.imap.security not in ("ssl", "starttls", "none"):
        raise ConfigError("[imap] security must be ssl, starttls or none")
    if not isinstance(cfg.access.enable_all, bool):
        raise ConfigError("[access] enable_all must be true or false")
    doms = cfg.access.enable_domains
    if not isinstance(doms, list) or not all(isinstance(d, str) for d in doms):
        raise ConfigError("[access] enable_domains must be a list of domain names")
    cfg.access.enable_domains = sorted({d.strip().lower() for d in doms if d.strip()})
    if not isinstance(cfg.server.cors_origins, list):
        raise ConfigError("[server] cors_origins must be a list")
    _check_ranges(cfg)
    insecure = cfg.insecure_transport()
    if insecure and not cfg.server.allow_insecure_transport:
        raise ConfigError(f"{', '.join(insecure)}: this sends secrets without verified TLS. "
                          "Fix it, or set [server] allow_insecure_transport = true")
    if not cfg.openai.api_key:
        cfg.openai.api_key = os.environ.get("OPENAI_API_KEY", "")
    return cfg


def load(path: str | None = None) -> Config:
    path = path or os.environ.get(CONFIG_ENV) or DEFAULT_PATH
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return from_dict(data)
