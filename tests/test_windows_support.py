"""Windows support, checked offline on any OS.

Covers the pieces of the Windows port that are plain logic: the scheduled
task definition, interpreter selection, aside CLI discovery (and that
bridge.py's copy of it matches winsupport.py's), matching a stale
`aside exec` by its command line, handing an over-long prompt over as a
file, `file:///C:/...` image paths, the bridgemon.cmd shim, and the
service state files. Nothing here touches Task Scheduler, the aside CLI
or Telegram; tests/test_windows_service_live.py does the live service run.

Run:  python tests/test_windows_support.py
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(HERE, "config.json")
sys.path.insert(0, HERE)

fails = []
checks = 0


def check(name, cond):
    global checks
    checks += 1
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAVED = None
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        SAVED = f.read()
SCRATCH = tempfile.mkdtemp(prefix="winsupport-test-")
SAVED_ENV = {k: os.environ.get(k) for k in (
    "LOCALAPPDATA", "ASIDE_CLI_INSTALL_DIR", "ASIDE_BRIDGE_SERVICE_HOME",
    "BRIDGEMON_DIR")}

try:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"token": "1234567890:TEST-ONLY-FAKE-BOT-TOKEN-not-real",
                   "chat_id": 8675309, "owner_name": "Fixture"}, f)
    os.environ["ASIDE_BRIDGE_SERVICE_HOME"] = os.path.join(SCRATCH, "svc")
    import winsupport as ws
    b = load("bridge_win", os.path.join(HERE, "bridge.py"))
    b.log = lambda msg: None

    # ---- 1. the scheduled task -----------------------------------------
    print("\nscheduled task definition")
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    xml = ws.task_xml(
        "AsideTelegramBridge", r"C:\Py & Co\pythonw.exe",
        r'"C:\a<b>\winsupport.py" run AsideTelegramBridge', r"C:\a<b>",
        r"PC\owner", "Aside Telegram bridge")
    root = ET.fromstring(xml.encode("utf-16"))

    def text(path):
        node = root.find(path, ns)
        return node.text if node is not None else None

    check("no execution time limit (the default kills it after 72h)",
          text("t:Settings/t:ExecutionTimeLimit") == "PT0S")
    check("battery power neither blocks nor stops it",
          text("t:Settings/t:DisallowStartIfOnBatteries") == "false"
          and text("t:Settings/t:StopIfGoingOnBatteries") == "false")
    check("a second start is ignored, not queued or parallel",
          text("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew")
    check("normal priority, not the below-normal default",
          text("t:Settings/t:Priority") == "5")
    check("runs as the signed-in user, unelevated",
          text("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
          and text("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege")
    check("triggered at that same user's logon",
          text("t:Triggers/t:LogonTrigger/t:UserId") == r"PC\owner"
          and text("t:Principals/t:Principal/t:UserId") == r"PC\owner")
    check("paths with XML metacharacters survive the round trip",
          text("t:Actions/t:Exec/t:Command") == r"C:\Py & Co\pythonw.exe"
          and text("t:Actions/t:Exec/t:Arguments")
          == r'"C:\a<b>\winsupport.py" run AsideTelegramBridge'
          and text("t:Actions/t:Exec/t:WorkingDirectory") == r"C:\a<b>")

    off = ET.fromstring(ws.task_xml(
        "AsideTelegramBridge", "pythonw.exe", "run", "C:\\x", r"PC\owner",
        at_logon=False).encode("utf-16"))
    check("autostart off disables the sign-in trigger only",
          off.find("t:Triggers/t:LogonTrigger/t:Enabled", ns).text == "false"
          and off.find("t:Settings/t:Enabled", ns).text == "true")
    check("autostart is on by default",
          text("t:Triggers/t:LogonTrigger/t:Enabled") == "true")

    # ---- 1b. switching autostart re-registers from the spec ------------
    print("\nautostart switch")
    registered = []

    class Ran:
        returncode, stdout, stderr = 0, "", ""

    def fake_schtasks(*args, timeout=60):
        if args[0] == "/Create":
            with open(args[args.index("/XML") + 1], encoding="utf-16") as f:
                registered.append(f.read())
        return Ran()

    real_schtasks = ws._schtasks
    ws._schtasks = fake_schtasks
    try:
        ok, _ = ws.install("AsideBridgeAutostartProbe", ["x.exe"], SCRATCH,
                           "out.log", "err.log", description="probe")
        check("a new service starts at sign-in",
              ok and ws.autostart_enabled("AsideBridgeAutostartProbe") is True
              and "<Enabled>true</Enabled>" in registered[-1].split(
                  "</LogonTrigger>")[0])
        ok, msg = ws.set_autostart("AsideBridgeAutostartProbe", False)
        check("switching it off re-registers with the trigger disabled",
              ok and ws.autostart_enabled("AsideBridgeAutostartProbe") is False
              and "<Enabled>false</Enabled>" in registered[-1].split(
                  "</LogonTrigger>")[0])
        ws.install("AsideBridgeAutostartProbe", ["y.exe"], SCRATCH,
                   "out.log", "err.log", description="probe")
        check("re-installing keeps the choice",
              ws.autostart_enabled("AsideBridgeAutostartProbe") is False
              and ws.read_spec("AsideBridgeAutostartProbe")["argv"]
              == ["y.exe"])
        check("an unknown service has no autostart to switch",
              ws.autostart_enabled("AsideBridgeNoSuchService") is None
              and ws.set_autostart("AsideBridgeNoSuchService", True)[0]
              is False)
    finally:
        ws._schtasks = real_schtasks

    # ---- 1c. Aside itself --------------------------------------------
    print("\nAside at sign-in")
    local_app = os.path.join(SCRATCH, "LocalAside", "Aside", "Application",
                             "Aside.exe")
    os.makedirs(os.path.dirname(local_app))
    open(local_app, "w").close()
    saved_pf = os.environ.get("ProgramFiles")
    os.environ["LOCALAPPDATA"] = os.path.join(SCRATCH, "LocalAside")
    os.environ["ProgramFiles"] = os.path.join(SCRATCH, "NoSuchProgramFiles")
    check("Aside.exe is found where the Aside CLI looks",
          ws.aside_app_path() == local_app)
    os.environ["LOCALAPPDATA"] = os.path.join(SCRATCH, "Empty")
    check("no Aside.exe reads as not installed", ws.aside_app_path() is None)
    if saved_pf is None:
        os.environ.pop("ProgramFiles", None)
    else:
        os.environ["ProgramFiles"] = saved_pf
    saved_appdata = os.environ.get("APPDATA")
    os.environ["APPDATA"] = os.path.join(SCRATCH, "Roaming")
    check("Aside's sign-in shortcut lives in the Startup folder",
          ws.aside_shortcut_path() == os.path.join(
              SCRATCH, "Roaming", "Microsoft", "Windows", "Start Menu",
              "Programs", "Startup", "Aside.lnk"))
    if saved_appdata is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = saved_appdata
    check("a Run entry for Aside.exe is recognised",
          ws._starts_aside(r'"C:\Program Files\Aside\Application\Aside.exe"')
          and not ws._starts_aside(r'"C:\Tools\aside-cli.exe"'))

    # ---- 2. interpreters -----------------------------------------------
    print("\ninterpreters")
    pydir = os.path.join(SCRATCH, "py")
    os.makedirs(pydir)
    py, pyw = os.path.join(pydir, "python.exe"), os.path.join(pydir,
                                                               "pythonw.exe")
    for p in (py, pyw):
        open(p, "w").close()
    check("the task runs the windowless pythonw next to python",
          ws.pythonw_for(py) == pyw)
    check("the service child runs python, which owns a hidden console",
          ws.console_python(pyw) == py)
    os.remove(pyw)
    check("no pythonw means python, not a missing file",
          ws.pythonw_for(py) == py)
    # The py launcher can pick a free-threaded build.
    pyt = os.path.join(pydir, "python3.13t.exe")
    pywt = os.path.join(pydir, "pythonw3.13t.exe")
    for p in (pyt, pywt):
        open(p, "w").close()
    check("a free-threaded python maps to its own pythonw",
          ws.pythonw_for(pyt) == pywt)
    check("and back again for the service child",
          ws.console_python(pywt) == pyt)

    # ---- 3. aside CLI discovery ----------------------------------------
    print("\naside CLI discovery")
    os.environ["LOCALAPPDATA"] = os.path.join(SCRATCH, "Local")
    os.environ.pop("ASIDE_CLI_INSTALL_DIR", None)
    plain = ws.aside_cli_candidates()
    check("the installer's default location comes first",
          plain[0] == os.path.join(SCRATCH, "Local", "Aside", "CLI",
                                   "current", "aside.exe"))
    os.environ["ASIDE_CLI_INSTALL_DIR"] = os.path.join(SCRATCH, "custom")
    custom = ws.aside_cli_candidates()
    check("an ASIDE_CLI_INSTALL_DIR override outranks it",
          custom[0] == os.path.join(SCRATCH, "custom", "current",
                                    "aside.exe")
          and custom[1:] == plain)
    check("bridge.py's copy lists exactly the same places",
          b.windows_aside_cli_candidates() == custom)
    target = custom[2]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "w").close()
    found = ws.find_aside_cli()
    check("the first one that exists is the one used",
          found == target)
    saved_flag = b.IS_WINDOWS
    b.IS_WINDOWS = True
    check("bridge.py resolves the same file", b.default_aside_cli() == target)
    b.IS_WINDOWS = saved_flag
    os.remove(target)

    # ---- 4. stale exec matching ----------------------------------------
    print("\nstale exec matching")
    cli = r'"C:\Users\me\AppData\Local\Aside\CLI\current\aside.exe"'
    m = b.exec_cmdline_matches
    check("a resumed turn on the session matches",
          m(cli + ' --effort medium session resume abc123 -- "hi there"',
            "abc123"))
    check("a resumed turn for another session does not",
          not m(cli + ' session resume abc1234 -- "x"', "abc123"))
    check("an older CLI's exec --session turn matches too",
          m(cli + ' exec --session abc123 -m claude-code/x "hi there"',
            "abc123"))
    check("a quoted session id matches",
          m(cli + ' exec --session "abc123" --effort low "x"', "abc123"))
    check("an id at the very end matches", m(cli + " exec --session abc123",
                                             "abc123"))
    check("a longer id that starts the same does not",
          not m(cli + ' exec --session abc1234 "x"', "abc123"))
    check("a repl call does not", not m(cli + " repl abc123", "abc123"))
    check("an id that is only in the prompt does not",
          not m(cli + ' exec --session zzz "about --session abc123"',
                "abc123"))

    # ---- 5. over-long prompts ------------------------------------------
    print("\nover-long prompts")
    sent = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        sent.append(cmd)
        return Done()

    real_run, real_media = b.subprocess.run, b.MEDIA_DIR
    b.subprocess.run = fake_run
    b.MEDIA_DIR = os.path.join(SCRATCH, "media")
    b.IS_WINDOWS = True
    try:
        long_prompt = "x" * 40000 + "\n\u3131\u3134 end"
        b.run_aside(long_prompt, session_id="abc123")
        last = sent[-1][-1]
        spilled = [os.path.join(b.MEDIA_DIR, f)
                   for f in os.listdir(b.MEDIA_DIR)]
        check("the command line stays under the Windows limit",
              len(b.subprocess.list2cmdline(sent[-1]))
              < b.WINDOWS_CMDLINE_MAX)
        check("the prompt names the file it was saved to",
              len(spilled) == 1 and spilled[0] in last)
        with open(spilled[0], encoding="utf-8") as f:
            check("the file holds the whole prompt, as UTF-8",
                  f.read() == long_prompt)
        check("the question reminder still rides on the short prompt",
              b.QUESTION_REMINDER in last)
        b.run_aside("short and sweet", session_id="abc123", effort="low")
        check("an ordinary prompt is passed through untouched",
              sent[-1][-1] == "short and sweet")
        check("a follow-up resumes the session, options first",
              sent[-1][1:] == ["--effort", "low", "session", "resume",
                               "abc123", "--", "short and sweet"])
        b.run_aside("-starts with a dash", effort="low")
        check("a new session is an exec, and the prompt follows --",
              sent[-1][1:] == ["--effort", "low", "exec", "--",
                               "-starts with a dash"])
    finally:
        b.subprocess.run, b.MEDIA_DIR = real_run, real_media
        b.IS_WINDOWS = saved_flag

    # ---- 6. file:// image paths ----------------------------------------
    print("\nfile:// image paths")
    img = os.path.join(SCRATCH, "a shot.png")
    open(img, "wb").close()
    uri = pathlib.Path(img).as_uri()
    b.IS_WINDOWS = True
    try:
        got = b._resolve_local_path(uri)
        check("a percent-encoded file URL resolves to the file (%s)" % uri,
              got is not None and os.path.samefile(got, img))
    finally:
        b.IS_WINDOWS = saved_flag
    check("a plain path still resolves", b._resolve_local_path(img) == img)

    # ---- 7. bridgemon.cmd ----------------------------------------------
    print("\nbridgemon.cmd")
    setup = load("setup_win", os.path.join(HERE, "setup.py"))
    bin_dir = os.path.join(SCRATCH, "bin")
    os.makedirs(bin_dir)
    shim = setup.write_bridgemon_shim(bin_dir)
    with open(shim, "rb") as f:
        body = f.read()
    check("the shim forwards every argument", b"%*" in body)
    check("it calls this checkout's bridgemon.py",
          os.path.join(HERE, "bridgemon.py")
          in body.decode("utf-8", "replace"))
    check("batch lines end in CRLF", b"\r\n" in body and b"\n\n" not in body)

    # ---- 8. service state ----------------------------------------------
    print("\nservice state")
    st = ws.status("AsideBridgeNoSuchService")
    check("an unknown service reads as stopped, not as an error",
          st["running"] is False and st["pid"] is None)
    ws.write_json_atomic(ws.state_path("AsideBridgeProbe"), {
        "supervisor_pid": os.getpid(),
        "supervisor_created": ws.process_created(os.getpid()),
        "pid": os.getpid(),
        "pid_created": ws.process_created(os.getpid()),
        "started": 1.0,
    })
    st = ws.status("AsideBridgeProbe")
    check("a live recorded pid reads as running",
          st["running"] is True and st["pid"] == os.getpid())
    ws.write_json_atomic(ws.state_path("AsideBridgeProbe"), {
        "supervisor_pid": os.getpid(), "pid": os.getpid(),
        "pid_created": 12345, "supervisor_created": 12345,
    })
    if os.name == "nt":
        check("a recycled pid (wrong creation time) reads as stopped",
              ws.status("AsideBridgeProbe")["running"] is False)

    # ---- 9. platform wiring --------------------------------------------
    print("\nplatform wiring")
    os.environ["BRIDGEMON_DIR"] = os.path.join(SCRATCH, "deploy")
    bm = load("bridgemon_win", os.path.join(HERE, "bridgemon.py"))
    check("bridgemon reads tracebacks from this platform's service log",
          os.path.basename(bm.ERR_LOG_PATH)
          == ("service.err.log" if os.name == "nt" else "launchd.err.log"))
    check("the Mini App service has this platform's name",
          bm.MINIAPP_LABEL == ("AsideMiniApp" if os.name == "nt"
                               else "com.aside.miniapp"))
    mon = load("monitor_win", os.path.join(HERE, "monitor.py"))
    check("monitor.py imports on this platform", callable(mon.read_key))
    check("`bridgemon autostart` is a command", "autostart" in bm.COMMANDS)

finally:
    if SAVED is None:
        if os.path.exists(CONFIG_PATH):
            os.unlink(CONFIG_PATH)
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(SAVED)
    for key, value in SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(SCRATCH, ignore_errors=True)

print("\n%d checks, %d failed" % (checks, len(fails)))
for name in fails:
    print("  - " + name)
sys.exit(1 if fails else 0)
