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
_redaction_warned = False


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
        # Only this cheap filter runs on the agent's thread. Every lookup (state.db, config, the store's files)
        # and the redaction can block for a while, so they all happen on the daemon thread.
        if kwargs.get("coalesced") or kwargs.get("surface") not in SURFACES:
            return
        request = dict(kwargs)
        self.spawn(lambda: self._work(request))

    def _work(self, kwargs: dict) -> None:
        try:
            self._prepare_and_send(kwargs)
        except Exception:
            log.exception("herald-push: couldn't send an approval push")

    def _prepare_and_send(self, kwargs: dict) -> None:
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
            command=self._safe_text(command, MAX_COMMAND), description=self._safe_text(description, MAX_DESCRIPTION),
            request_id=key[:12])
        sig = self.store.identity()[2]
        self._send(profile, devices, payload, sig)

    def _safe_text(self, text: str, limit: int) -> Optional[str]:
        """The text with secrets redacted, then clipped; None when it can't be redacted (it is then left out)."""
        global _redaction_warned
        if not text:
            return None
        try:
            return clip(self.redact(text), limit) or None
        except Exception as exc:
            if not _redaction_warned:
                _redaction_warned = True
                log.warning("herald-push: can't redact approval text (%s); only the bot's name is sent", type(exc).__name__)
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


def attach(ctx, store: Store, root: Path) -> Optional[ApprovalNotifier]:
    """Registers the approval observer for this plugin's profile. None when this Hermes can't (no hook support) or
    the profile's name is not one the plugin handles; the plugin then simply sends no approval pushes."""
    register = getattr(ctx, "register_hook", None)
    if register is None:
        return None
    try:
        home_path = getattr(getattr(ctx, "_manager", None), "home_path", None)
        if not home_path:
            # Guessing "default" here could push one bot's approvals to phones allowed only another's.
            log.warning("herald-push: can't tell which profile this plugin serves; approval pushes are off")
            return None
        home = Path(home_path)
        profile = "default" if home.resolve() == Path(root).resolve() else home.name
    except Exception:
        log.warning("herald-push: can't tell which profile this plugin serves; approval pushes are off", exc_info=True)
        return None
    if not PROFILE_RE.match(profile):
        log.warning("herald-push: approval pushes are off for a profile with an unusable name")
        return None
    notifier = ApprovalNotifier(store, profile, home, root)
    try:
        register(HOOK, notifier.on_request)
    except Exception:
        log.warning("herald-push: this Hermes can't register %s; approval pushes are off", HOOK, exc_info=True)
        return None
    return notifier
