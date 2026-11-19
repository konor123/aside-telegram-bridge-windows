"""A session Aside has deleted is replaced, not reported over and over.

Aside deletes a session the CLI started 15 minutes after it goes idle
unless `aside settings save-sessions` is on, and the next message then
failed with "Session not found" until the owner thought of /new. Also
covers how a session is created now that Aside refuses to raise a
session's permission from the repl.

Offline: Telegram, the aside CLI and the session transcripts are stubbed.

Run:  python tests/test_session_recovery.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(HERE, "config.json")

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


SAVED = None
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        SAVED = f.read()

try:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"token": "1234567890:TEST-ONLY-FAKE-BOT-TOKEN-not-real",
                   "chat_id": 8675309, "owner_name": "Fixture"}, f)
    spec = importlib.util.spec_from_file_location(
        "bridge_recovery", os.path.join(HERE, "bridge.py"))
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)

    TEXTS = []
    b.log = lambda msg: None
    b.tg = lambda method, params=None, timeout=65: {
        "ok": True, "result": {"message_id": 1}}
    b.send_text = TEXTS.append
    b.send_bubbles = TEXTS.append
    b.tg_send_status = lambda text: 1
    b.tg_edit = lambda *a, **k: True
    b.tg_delete = lambda *a, **k: None
    b.save_json = lambda *a, **k: None
    b.session_msg_file = lambda sid: None

    # ---- 1. the command line -------------------------------------------
    print("\ncreating a session")
    sent = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    real_run = b.subprocess.run
    b.subprocess.run = lambda cmd, **kw: sent.append(cmd) or Done()
    try:
        b.run_aside("hi", effort="low", permission="full-access")
        check("full access rides on the create, before the subcommand",
              sent[-1][1:] == ["--effort", "low", "--permission",
                               "full-access", "exec", "--", "hi"])
        b.run_aside("again", session_id="abc", effort="low")
        check("a follow-up does not repeat it",
              "--permission" not in sent[-1])
    finally:
        b.subprocess.run = real_run

    expr = b.new_session_settings_expression("S1")
    check("the repl call no longer asks to raise permission",
          "permissionMode" not in expr and "finalConfirm: false" in expr)

    # ---- 2. a deleted session is replaced and the message resent -------
    print("\nrecovering from a deleted session")
    calls = []
    replies = {
        "gone0001": (1, "", "continuing existing session: gone0001\n"
                            " - Error Session not found: gone0001"),
        "fresh002": (0, "hello back", ""),
    }

    def fake_run_aside(prompt, session_id=None, model=None, effort=None,
                       permission=None):
        calls.append((session_id, prompt))
        return replies[session_id]

    def fake_heavy_new():
        calls.append(("heavy_new", None))
        b.state["session_id"] = "fresh002"

    b.run_aside = fake_run_aside
    b.heavy_new = fake_heavy_new
    b.state["session_id"] = "gone0001"
    TEXTS.clear()
    b.handle_message("hello")

    check("a fresh session is started", ("heavy_new", None) in calls)
    check("the message is sent again, to the new session",
          [c[0] for c in calls] == ["gone0001", "heavy_new", "fresh002"]
          and calls[-1][1].startswith("hello"))
    check("the owner is told why", any("no longer exists" in t
                                       for t in TEXTS))
    check("the reply arrives", "hello back" in TEXTS)
    check("the raw error is not what they see",
          not any("hit an error" in t for t in TEXTS))

    # ---- 3. it gives up rather than looping ----------------------------
    print("\nno retry loop")
    calls.clear()
    TEXTS.clear()
    replies["fresh002"] = replies["gone0001"]

    def heavy_new_same():
        calls.append(("heavy_new", None))

    b.heavy_new = heavy_new_same
    b.state["session_id"] = "gone0001"
    b.handle_message("hello")
    check("no resend when /new could not produce a new session",
          [c[0] for c in calls] == ["gone0001", "heavy_new"])

    calls.clear()
    TEXTS.clear()
    b.heavy_new = fake_heavy_new
    b.state["session_id"] = "gone0001"
    b.handle_message("hello")
    check("a second 'not found' is reported, not retried again",
          [c[0] for c in calls] == ["gone0001", "heavy_new", "fresh002"]
          and any("hit an error" in t for t in TEXTS))

    # ---- 4. other failures are left alone ------------------------------
    print("\nother failures")
    calls.clear()
    TEXTS.clear()
    replies["gone0001"] = (1, "", "rate limited")
    b.state["session_id"] = "gone0001"
    b.handle_message("hello")
    check("an unrelated error does not start a new session",
          [c[0] for c in calls] == ["gone0001"]
          and any("rate limited" in t for t in TEXTS))
finally:
    if SAVED is None:
        if os.path.exists(CONFIG_PATH):
            os.unlink(CONFIG_PATH)
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(SAVED)

print("\n%d failed" % len(fails))
for name in fails:
    print("  - " + name)
sys.exit(1 if fails else 0)
