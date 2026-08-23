"""Connection registry: persistent, user-inspectable server/credential map.

Stored under plugin-data (via ctx.state key "session.connections") —
never raw passwords, only pointers: user, auth method, where the
credential lives on disk.
"""

import json
import logging
import time

from . import state

logger = logging.getLogger(__name__)


def record(host, user=None, auth_method=None, cred_source=None, source="manual"):
    """Upsert a connection entry."""
    def _rec(st):
        conns = st.setdefault("connections", {})
        existing = conns.get(host, {})
        conns[host] = {
            "user": user or existing.get("user"),
            "auth_method": auth_method or existing.get("auth_method", "password"),
            "cred_source": cred_source or existing.get("cred_source"),
            "last_used": time.time(),
            "source": source,
        }
    state.update_session(_rec)


def forget(host):
    def _rm(st):
        st.get("connections", {}).pop(host, None)
    state.update_session(_rm)


def report():
    st = state.session()
    conns = st.get("connections") or {}
    if not conns:
        return "No connections tracked. They are learned from ssh commands and /continuum conn add."
    lines = ["Tracked connections:"]
    for host, info in sorted(conns.items()):
        lines.append(
            f"  {host}: user={info.get('user')}, auth={info.get('auth_method')}, "
            f"cred_source={info.get('cred_source')}, last_used="
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(info['last_used'])) if info.get('last_used') else 'n/a'}"
        )
    fails = st.get("ssh_auth_failed") or {}
    for host, n in fails.items():
        lines.append(f"  [AUTH FAILURES] {host}: {n}")
    return "\n".join(lines)
