"""Live check of the Windows background service, against Task Scheduler.

Registers a throwaway scheduled task (`AsideBridgeLiveTest`) whose payload
is a small script that starts a console grandchild, then walks the whole
lifecycle: start, crash and automatic restart, restart, stop, uninstall.
The task and its files are removed again whatever happens. It never
touches the bridge's or the Mini App's own services.

Opt-in, because it creates a real scheduled task:

    set ASIDE_BRIDGE_LIVE_SERVICE_TEST=1
    python tests\\test_windows_service_live.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

if os.name != "nt" or os.environ.get("ASIDE_BRIDGE_LIVE_SERVICE_TEST") != "1":
    print("skipped: Windows only, and set ASIDE_BRIDGE_LIVE_SERVICE_TEST=1 "
          "to run (it registers a temporary scheduled task)")
    sys.exit(0)

import winsupport as ws  # noqa: E402

NAME = "AsideBridgeLiveTest"
PAYLOAD = r'''
import os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
child = subprocess.Popen(["ping", "-n", "3000", "127.0.0.1"],
                         stdout=subprocess.DEVNULL)
with open(os.path.join(here, "grandchild.pid"), "w") as f:
    f.write(str(child.pid))
sys.stderr.write("payload up ✓ 한글\n")
sys.stderr.flush()
while True:
    print("tick", flush=True)
    time.sleep(2)
'''

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def wait_for(predicate, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def grandchild(work):
    try:
        with open(os.path.join(work, "grandchild.pid")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


work = tempfile.mkdtemp(prefix="aside-live-svc-")
try:
    script = os.path.join(work, "payload.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(PAYLOAD)
    err_log = os.path.join(work, "payload.err.log")

    print("\ninstall")
    ok, msg = ws.install(NAME, [ws.console_python(), script], work,
                         os.path.join(work, "payload.out.log"), err_log,
                         throttle=3, description="aside bridge live test")
    check("the scheduled task registers (%s)" % msg, ok)
    check("Task Scheduler knows it", ws.installed(NAME))

    print("\nstart")
    check("start brings the process up", ws.start(NAME))
    first = ws.status(NAME)
    check("status reports it running with a pid",
          first["running"] and first["pid"])
    check("its console grandchild is running",
          wait_for(lambda: ws.pid_alive(grandchild(work)), 10))

    def logged():
        try:
            with open(err_log, encoding="utf-8", errors="replace") as f:
                return "한글" in f.read()
        except OSError:
            return False
    check("stderr reaches the log as UTF-8", wait_for(logged, 10))

    print("\ncrash")
    ws.terminate(first["pid"])
    check("the supervisor restarts a process that dies",
          wait_for(lambda: ws.status(NAME)["running"]
                   and ws.status(NAME)["pid"] != first["pid"], 20))

    print("\nrestart")
    before = ws.status(NAME)
    old_gc = grandchild(work)
    check("restart succeeds", ws.restart(NAME))
    after = ws.status(NAME)
    check("restart replaces the process",
          after["running"] and after["pid"] != before["pid"])
    check("the old process tree is gone",
          not ws.pid_alive(before["pid"]) and not ws.pid_alive(old_gc))

    print("\nstop")
    gc = grandchild(work)
    check("stop succeeds", ws.stop(NAME))
    check("nothing of the service is left running",
          not ws.status(NAME)["running"] and not ws.pid_alive(gc))
finally:
    ok, msg = ws.uninstall(NAME)
    print("\nuninstall")
    check("the task is removed (%s)" % msg, ok and not ws.installed(NAME))
    check("its spec is removed", not os.path.exists(ws.spec_path(NAME)))
    shutil.rmtree(work, ignore_errors=True)

print("\n%d failed" % len(fails))
for name in fails:
    print("  - " + name)
sys.exit(1 if fails else 0)
