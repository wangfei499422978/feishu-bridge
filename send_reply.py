#!/usr/bin/env python3
"""Feishu reply-path TEST tool — manual send only, NOT an auto-reply loop.

Purpose: prove the bot can send a reply to a specific received message
before any auto-reply wiring is promised. Run it once, by hand, with the
message_id of a message the user actually sent (see events.log).

Usage:
    FEISHU_APP_ID=... FEISHU_APP_SECRET=... python3 send_reply.py \
        --message-id om_... --text "回复通道测试：收到"

Reads credentials ONLY from environment variables. Never hardcodes,
prints, or logs any credential or token. Prints only the API result
code/message and the new message_id on success.

API: POST /open-apis/im/v1/messages/:message_id/reply
(body: msg_type="text", content='{"text": "..."}', reply_in_thread=false)
via lark-oapi 1.7.5 (same SDK version as receiver.py).
Requires the app to have bot capability + im:message / im:message:send_as_bot
scopes; without them the API returns an error (e.g. bot not activated)
instead of sending — that error is itself the diagnostic.

NOT solved by this tool: waking Muse from a Feishu message. The receiver
today only logs events; turning an incoming message into a Muse run and
routing Muse's answer back is a separate design decision (queue + worker,
or polling events.log). Do not promise automatic answering until that
piece is built and tested.
"""
import argparse
import json
import os
import sys
import uuid

import lark_oapi as lark
from lark_oapi.api.im.v1 import model


def main() -> int:
    ap = argparse.ArgumentParser(description="Send one test reply to a Feishu message.")
    ap.add_argument("--message-id", required=True, help="om_... id from events.log")
    ap.add_argument("--text", required=True, help="Reply text to send")
    args = ap.parse_args()

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        print("ERROR: FEISHU_APP_ID / FEISHU_APP_SECRET env vars are required", flush=True)
        return 2

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    body = (
        model.ReplyMessageRequestBody.builder()
        .msg_type("text")
        .content(json.dumps({"text": args.text}, ensure_ascii=False))
        .reply_in_thread(False)
        .uuid(str(uuid.uuid4()))
        .build()
    )
    req = (
        model.ReplyMessageRequest.builder()
        .message_id(args.message_id)
        .request_body(body)
        .build()
    )
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        print(f"REPLY FAILED code={resp.code} msg={resp.msg}", flush=True)
        return 1
    data = resp.data
    new_id = getattr(data, "message_id", "?") if data else "?"
    print(f"REPLY OK new_message_id={new_id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
