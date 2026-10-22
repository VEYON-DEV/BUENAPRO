"""Peruvian egress for SEACE, configured only through the environment.

SEACE blocks the VM's IP, so SEACE hosts may go through an HTTP proxy that only
tunnels ``*.seace.gob.pe:443``. Every other host (LLM, Telegram, Redis...) stays
direct. This module reads ``os.environ`` instead of ``Settings`` because the
PROD2 discovery transport avoids Settings and the PROD6 image is an overlay.

``SEACE_EGRESS_PROXY``   proxy URL, e.g. ``http://host:8888`` (optional).
``SEACE_EGRESS_REQUIRED`` ``1``/``true``: fail closed when the proxy is missing.
"""
from __future__ import annotations

import logging
import os
import socket
from urllib.parse import urlsplit

import httpx

LOGGER = logging.getLogger(__name__)
SEACE_DOMAIN = "seace.gob.pe"
# Child exit code meaning "the egress path failed", never a SEACE denial (403/429).
EGRESS_EXIT_CODE = 75


class EgressUnavailable(RuntimeError):
    """The SEACE egress path is down. Distinct from SEACE answering 403."""


class EgressNotConfigured(EgressUnavailable):
    """SEACE_EGRESS_REQUIRED is set but there is no usable SEACE_EGRESS_PROXY."""


def _proxy_url() -> str | None:
    proxy = os.environ.get("SEACE_EGRESS_PROXY", "").strip()
    required = os.environ.get("SEACE_EGRESS_REQUIRED", "").strip().lower() in {"1", "true"}
    if not proxy:
        if required:
            raise EgressNotConfigured("SEACE_EGRESS_REQUIRED is set without SEACE_EGRESS_PROXY")
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EgressNotConfigured("SEACE_EGRESS_PROXY must be an http(s) URL with a host")
    return proxy


def _proxy_transport(proxy: str, verify) -> httpx.HTTPTransport:
    return httpx.HTTPTransport(proxy=proxy, verify=verify)


def seace_http_client(**kwargs) -> httpx.Client:
    """Build the httpx.Client for SEACE. Environment proxies are never trusted."""
    proxy = _proxy_url()
    kwargs["trust_env"] = False
    if proxy:
        verify = kwargs.get("verify", True)
        kwargs["mounts"] = {**kwargs.get("mounts", {}),
                            f"all://{SEACE_DOMAIN}": _proxy_transport(proxy, verify),
                            f"all://*.{SEACE_DOMAIN}": _proxy_transport(proxy, verify)}
    return httpx.Client(**kwargs)


def check_egress(timeout: float = 5) -> None:
    """Open one TCP connection to the proxy; nothing is sent to SEACE."""
    proxy = _proxy_url()
    if proxy is None:
        return
    parsed = urlsplit(proxy)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
    except OSError as error:
        raise EgressUnavailable(f"SEACE egress proxy unreachable: {type(error).__name__}") from None


def is_egress_failure(error: BaseException) -> bool:
    """True when the proxy or the path to it failed, not when SEACE answered."""
    if isinstance(error, httpx.ProxyError):
        return True
    if not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return False
    # Through the proxy, a failed TCP connect can only be the connection to the proxy.
    try:
        host = (error.request.url.host or "").lower()
        return (host == SEACE_DOMAIN or host.endswith("." + SEACE_DOMAIN)) and _proxy_url() is not None
    except (RuntimeError, EgressNotConfigured):
        return False


def notify_ops(text: str) -> bool:
    """Best-effort Telegram alert to operations; never raises, never uses the proxy."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("OPS_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        from buenapro_worker.notifications.channels import send_telegram
        send_telegram(None, chat_id, text, bot_token=token)
    except Exception as error:
        # The exception text may carry the bot token in the request URL.
        LOGGER.warning("Ops alert not delivered: %s", type(error).__name__)
        return False
    return True
