"""Tells registered phones that a bot is waiting for the user to approve a command.

Hermes calls the ``pre_approval_request`` observer, through the plugin manager of the active profile, in the process
where the approval happens (the dashboard for an app chat, the messaging gateway for a Telegram one). The observer
must never slow or break the agent: it only filters, redacts and de-duplicates, then hands the sealed push to a
daemon thread. Nothing can be answered from the notification; the phone opens the chat, where the user approves as
usual. There is no outbox: this may run in a process that isn't the watcher's leader, and an approval is only
useful for minutes.

Only surfaces where a person is asked push: ``gateway`` and ``cli``. The smart-approval guardian (``smart``) fires
the same hook while an auxiliary model decides, and a ``coalesced`` follower repeats a request already pending.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import httpx

from . import push
from .phone import _profile_holding_session
from .store import PROFILE_RE, Store
from .watcher import bot_label, profile_enabled

log = logging.getLogger(__name__)

MAX_COMMAND = 160
MAX_DESCRIPTION = 240
DEDUPE_SECONDS = 60
SURFACES = ("gateway", "cli")
HOOK = "pre_approval_request"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# One map for the whole process, keyed without the profile: several plugin instances may see one request.
_recent: Dict[str, float] = {}
_recent_lock = threading.Lock()


def host_redact(text: str) -> str:
    """Hermes' own redaction of secrets in a command. ImportError when this Hermes doesn't have it."""
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(text, force=True)


def start_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="herald-push-approval", daemon=True).start()


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def seen_recently(key: str, now: float) -> bool:
    with _recent_lock:
        for old in [k for k, t in _recent.items() if now - t >= DEDUPE_SECONDS]:
            del _recent[old]
        if key in _recent:
            return True
        _recent[key] = now
    return False


class ApprovalNotifier:
    def __init__(self, store: Store, profile: str, home: Path, root: Path, *, now: Callable[[], float] = time.time,
                 post=push.post, seal=push.envelope_for, redact: Callable[[str], str] = host_redact,
                 enabled: Callable[[Path], bool] = profile_enabled,
                 spawn: Callable[[Callable[[], None]], None] = start_thread,
                 resolve: Optional[Callable[[str], str]] = None):
        self.store = store
        self.profile = profile
        self.home = home
        self.root = Path(root)
        self.now = now
        self.post = post
        self.seal = seal
        self.redact = redact
        self.enabled = enabled
        self.spawn = spawn
        self.resolve = resolve if resolve is not None else (lambda session_key: _profile_holding_session(session_key, self.root))

    def on_request(self, **kwargs) -> None:
        """The hook callback. Never raises: an exception here would land in the agent's approval path."""
        try:
            self._notify(kwargs)
        except Exception:
            log.exception("herald-push: couldn't prepare an approval push")

    def _profile_and_home(self, session: str):
        """The profile that owns the session (when exactly one does), else this plugin's own."""
        try:
            owner = self.resolve(session) if session else ""
        except Exception:
            owner = ""
        if owner and PROFILE_RE.match(owner) and owner != self.profile:
            return owner, (self.root if owner == "default" else self.root / "profiles" / owner)
        return self.profile, self.home

    def _notify(self, kwargs: dict) -> None:
        if kwargs.get("coalesced") or kwargs.get("surface") not in SURFACES:
            return
        command = str(kwargs.get("command") or "")
        description = str(kwargs.get("description") or "")
        session = str(kwargs.get("session_key") or "")
        profile, home = self._profile_and_home(session)
        if not self.enabled(home):
            return
        allow = self.store.allowlist()
        devices = [d for d in self.store.devices() if allow.allows(d.device_id, profile)]
        if not devices:
            return
        key = hashlib.sha256("\x1f".join((session, str(kwargs.get("pattern_key") or ""), command)).encode("utf-8")).hexdigest()
        if seen_recently(key, self.now()):
            return
        payload = push.message(
            "approval", bot=profile, label=bot_label(home, profile), session=session or None,
            command=self._safe_command(command), description=clip(description, MAX_DESCRIPTION) or None,
            request_id=key[:12])
        sig = self.store.identity()[2]
        self.spawn(lambda: self._send(profile, devices, payload, sig))

    def _safe_command(self, command: str) -> Optional[str]:
        """The command with secrets redacted and clipped, or None when it can't be redacted."""
        if not command:
            return None
        try:
            return clip(self.redact(command), MAX_COMMAND) or None
        except Exception:
            return None

    def _send(self, profile: str, devices, payload: dict, sig) -> None:
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
                for device in devices:
                    try:
                        body = self.seal(device, payload, sig)
                        outcome = self.post(device, body, client)
                    except Exception as exc:
                        log.warning("herald-push: an approval push for %s failed: %s", profile, type(exc).__name__)
                        continue
                    if outcome != push.OK:
                        log.warning("herald-push: an approval push for %s was not taken (%s)", profile, outcome)
        except Exception:
            log.exception("herald-push: sending approval pushes failed")
