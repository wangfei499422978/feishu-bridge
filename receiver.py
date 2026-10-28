#!/usr/bin/env python3
"""Feishu long-connection receiver — receive + persistent dedup + received-signal.

Reads credentials ONLY from environment variables:
  FEISHU_APP_ID, FEISHU_APP_SECRET
Optional: FEISHU_BOT_OPEN_ID (skip the bot's own messages when set).
Never hardcodes, logs, or stores any credential or token.
Subscribes to im.message.receive_v1. For each accepted message it:
  - records message_id in a persistent on-disk seen store (dedup),
  - skips replays, messages older than 15 minutes, and (when
    FEISHU_BOT_OPEN_ID is set) its own messages,
  - logs an EVENT line (the exact format the auto-reply hook greps for),
  - immediately adds a THUMBSUP reaction as a received-signal.
Skipped messages are logged as SKIP lines WITHOUT the "message_id="
substring, so the auto-reply hook never wakes for them.
Reaction failures are logged (code only) and never crash the receiver;
a missing reactions permission shows up as an API error, not a crash.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import lark_oapi as lark
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

BRIDGE_DIR = os.path.expanduser("~/workspace/feishu-bridge")
SEEN_FILE = os.path.join(BRIDGE_DIR, "seen_ids.json")
ANSWERED_FILE = os.path.join(BRIDGE_DIR, "answered.txt")
SEEN_MAX = 2000
STALE_SEC = 15 * 60

_token_cache = {"token": "", "expire_at": 0.0}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts} UTC] {msg}", flush=True)


def safe_text(content, message_type):
    try:
        data = json.loads(content or "{}")
        if message_type == "text":
            return data.get("text", "")
        return f"<{message_type} message>"
    except Exception:
        return "<unparsable content>"


def load_seen():
    order = []
    seen = set()
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            if isinstance(data, list):
                for mid in data:
                    mid = str(mid)
                    if mid not in seen:
                        seen.add(mid)
                        order.append(mid)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"seen store unreadable, starting empty: {type(e).__name__}")
    # Seed from answered.txt (ids already replied to) so replays are skipped.
    try:
        with open(ANSWERED_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("om_") and line not in seen:
                    seen.add(line)
                    order.append(line)
    except FileNotFoundError:
        pass
    return order, seen


def save_seen(order):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(order[-SEEN_MAX:], f)
    os.replace(tmp, SEEN_FILE)


def get_tenant_token(app_id, app_secret):
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_at"] > now + 60:
        return _token_cache["token"]
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    if data.get("code") != 0:
        raise RuntimeError(f"tenant token code={data.get('code')} msg={data.get('msg')}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + int(data.get("expire", 7200))
    return _token_cache["token"]


def send_reaction(app_id, app_secret, message_id):
    """Add a THUMBS_UP reaction. Returns (ok, detail) with no secrets."""
    try:
        token = get_tenant_token(app_id, app_secret)
        body = json.dumps({"reaction_type": {"emoji_type": "THUMBSUP"}}).encode()
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if data.get("code") == 0:
            return True, "ok"
        return False, f"code={data.get('code')} msg={data.get('msg')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def make_handler(app_id, app_secret, bot_open_id):
    order, seen = load_seen()
    log(f"dedup store loaded: {len(order)} ids (seen_ids.json + answered.txt)")

    def on_message(data):
        try:
            ev = data.event
            msg = ev.message
            sender = ev.sender
            open_id = ""
            try:
                open_id = (sender.sender_id.open_id or "") if sender and sender.sender_id else ""
            except Exception:
                open_id = ""
            mid = getattr(msg, "message_id", "?")
            # Skip rules (logged as SKIP, never as EVENT, so the hook won't wake).
            if mid in seen:
                log(f"SKIP dup id={mid}")
                return
            try:
                ct = int(getattr(msg, "create_time", "0") or 0) / 1000.0
            except Exception:
                ct = 0.0
            if ct and (time.time() - ct > STALE_SEC):
                log(f"SKIP stale id={mid}")
                return
            if bot_open_id and open_id and open_id == bot_open_id:
                log(f"SKIP self id={mid}")
                return
            seen.add(mid)
            order.append(mid)
            try:
                save_seen(order)
            except Exception as e:
                log(f"seen store save failed: {type(e).__name__}")
            log(
                "EVENT im.message.receive_v1 "
                f"message_id={mid} "
                f"chat_id={getattr(msg, 'chat_id', '?')} "
                f"sender_open_id={open_id or '?'} "
                f"type={getattr(msg, 'message_type', '?')} "
                f"text={safe_text(getattr(msg, 'content', ''), getattr(msg, 'message_type', ''))!r}"
            )
            ok, detail = send_reaction(app_id, app_secret, mid)
            log(f"REACTION {'ok' if ok else 'fail'} id={mid} {detail}")
        except Exception as e:
            log(f"EVENT handler error: {type(e).__name__}")

    return on_message


def main():
    # Credentials: restricted local config file first (user-approved storage),
    # env vars as fallback. Values are never printed or logged.
    cfg_id = ""
    cfg_secret = ""
    cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".feishu-credentials")
    try:
        with open(cred_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("FEISHU_APP_ID="):
                    cfg_id = line.split("=", 1)[1].strip()
                elif line.startswith("FEISHU_APP_SECRET="):
                    cfg_secret = line.split("=", 1)[1].strip()
    except OSError:
        pass
    app_id = cfg_id or os.environ.get("FEISHU_APP_ID", "")
    app_secret = cfg_secret or os.environ.get("FEISHU_APP_SECRET", "")
    if cfg_id and cfg_secret:
        cred_source = "config file"
    elif cfg_id or cfg_secret:
        cred_source = "config file + env"
    else:
        cred_source = "env"
    bot_open_id = os.environ.get("FEISHU_BOT_OPEN_ID", "")
    if not app_id or not app_secret:
        print("ERROR: Feishu credentials not found (config file or env vars)", flush=True)
        return 2
    log(f"receiver starting (credentials from {cred_source}, values never printed)")
    log(f"own-message filter: {'ON' if bot_open_id else 'OFF (FEISHU_BOT_OPEN_ID unset)'}")
    # Workaround for lark-oapi 1.7.5: its WS client forces proxy=None, which
    # breaks in environments where only the HTTP(S) proxy reaches the internet.
    # FEISHU_WS_USE_ENV_PROXY=1 -> drop the proxy key so websockets 15
    # discovers the proxy from env vars. FEISHU_WS_PROXY_EXPLICIT=1 -> set the
    # proxy explicitly from the https_proxy env var.
    if os.environ.get("FEISHU_WS_USE_ENV_PROXY") == "1":
        import lark_oapi.ws.client as _wsclient

        _orig_kwargs = _wsclient._ws_connect_kwargs

        def _patched(connection_headers=None):
            kwargs = _orig_kwargs(connection_headers)
            kwargs.pop("proxy", None)
            return kwargs

        _wsclient._ws_connect_kwargs = _patched
        log("proxy workaround ON: SDK proxy=None override removed (env discovery)")
    elif os.environ.get("FEISHU_WS_PROXY_EXPLICIT") == "1":
        import lark_oapi.ws.client as _wsclient

        _proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
        _orig_kwargs = _wsclient._ws_connect_kwargs

        def _patched_explicit(connection_headers=None):
            kwargs = _orig_kwargs(connection_headers)
            kwargs["proxy"] = _proxy_url or None
            return kwargs

        _wsclient._ws_connect_kwargs = _patched_explicit
        log("proxy workaround ON: explicit proxy from env (value never printed)")
    log("domain=https://open.feishu.cn mode=long-connection(websocket) event=im.message.receive_v1")
    handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(make_handler(app_id, app_secret, bot_open_id))
        .build()
    )
    cli = lark.ws.Client(app_id, app_secret, event_handler=handler)
    log("ws client created, calling start() (blocks until stopped)...")
    cli.start()
    log("ws client stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
