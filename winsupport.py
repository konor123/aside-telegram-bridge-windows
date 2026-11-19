#!/usr/bin/env python3
"""Windows support: background services, process lookup, console setup.

On macOS both the bridge and the Mini App are launchd agents. Windows has
no per-user agent with launchd's keep-alive and log redirection, so a
service here is two pieces:

  * a Task Scheduler task, triggered at logon of the current user, that
    runs `pythonw winsupport.py run <name>`;
  * that supervisor, which starts the real process with stdout/stderr
    appended to log files, restarts it when it exits (at most once per
    `throttle` seconds, like launchd's ThrottleInterval), and holds it in
    a kill-on-close job object so stopping the supervisor takes the whole
    process tree with it -- `aside exec` turns and cloudflared included.

Nothing here parses `schtasks` text, which is localised. Whether a task
exists comes from its exit code; whether the service runs comes from the
state file the supervisor writes, checked against the live process table.

Usage (mostly for debugging; setup.py and bridgemon drive it):
  python winsupport.py status|start|stop|restart|uninstall <name>
  python winsupport.py run <name>        the supervisor itself
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape as _xml_escape

IS_WINDOWS = os.name == "nt"

HERE = os.path.dirname(os.path.realpath(__file__))

BRIDGE_SERVICE = "AsideTelegramBridge"
MINIAPP_SERVICE = "AsideMiniApp"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

DEFAULT_THROTTLE_S = 10
LOG_ROTATE_BYTES = 10 * 1024 * 1024


def service_home():
    """Where service specs and runtime state live, per user."""
    override = os.environ.get("ASIDE_BRIDGE_SERVICE_HOME")
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        os.path.join("~", "AppData", "Local"))
    return os.path.join(base, "aside-telegram-bridge", "services")


def spec_path(name):
    return os.path.join(service_home(), "%s.json" % name)


def state_path(name):
    return os.path.join(service_home(), "%s.state.json" % name)


def no_window():
    """`subprocess` kwargs that keep a console child from opening a window."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


def run_quiet(cmd, timeout=60, **kw):
    """Run a helper command with no console window, decoding leniently."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    if kw.get("text"):
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    if IS_WINDOWS:
        kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
    return subprocess.run(cmd, timeout=timeout, **kw)


# --- console -------------------------------------------------------------


def setup_console():
    """UTF-8 output that cannot raise, and ANSI colours in the console.

    A Korean or Japanese Windows console defaults to a legacy code page,
    and redirected output uses it too, so printing a check mark could
    raise UnicodeEncodeError. Colours need virtual terminal processing,
    which the classic console host only does when asked.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.DWORD)]
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        for std in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = k32.GetStdHandle(std)
            mode = wintypes.DWORD()
            if handle and k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except (OSError, AttributeError, ValueError):
        pass


# --- the Aside CLI -------------------------------------------------------


def aside_cli_candidates():
    """Where the Aside CLI installer puts aside.exe, in the order the Aside
    daemon itself looks. bridge.py keeps an identical copy so that it stays
    a single self-contained file."""
    home = os.path.expanduser("~")
    local = (os.environ.get("LOCALAPPDATA") or "").strip() \
        or os.path.join(home, "AppData", "Local")
    out = []
    custom = (os.environ.get("ASIDE_CLI_INSTALL_DIR") or "").strip()
    if custom:
        out.append(os.path.join(custom, "current", "aside.exe"))
    out += [
        os.path.join(local, "Aside", "CLI", "current", "aside.exe"),
        os.path.join(home, ".local", "bin", "aside.exe"),
        os.path.join(home, ".aside", "bin", "aside.exe"),
    ]
    return out


def find_aside_cli():
    """The installed aside.exe, or None."""
    for candidate in aside_cli_candidates():
        if os.path.isfile(candidate):
            return candidate
    import shutil
    return shutil.which("aside")


def console_python(python=None):
    """The console interpreter for a service child: it gets a hidden
    console of its own, which the console programs it starts then share
    instead of each opening a window. pythonw3.13t.exe -> python3.13t.exe."""
    python = python or sys.executable
    folder, exe = os.path.split(python)
    if exe.lower().startswith("pythonw"):
        candidate = os.path.join(folder, "python" + exe[len("pythonw"):])
        if os.path.exists(candidate):
            return candidate
    return python


# --- processes -----------------------------------------------------------

STILL_ACTIVE = 259
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(ctypes.c_ulonglong)] * 4
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    return k32


def process_created(pid):
    """The process's creation time (FILETIME ticks), or None if gone.

    Recorded next to every pid this module stores, so a pid Windows has
    since handed to an unrelated process is never mistaken for ours.
    """
    if not IS_WINDOWS or not pid:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                             int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) \
                or code.value != STILL_ACTIVE:
            return None
        created, ended = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not k32.GetProcessTimes(handle, ctypes.byref(created),
                                   ctypes.byref(ended), ctypes.byref(kernel),
                                   ctypes.byref(user)):
            return None
        return int(created.value)
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid, created=None):
    """True if `pid` is running and, when given, was created at `created`."""
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False
    actual = process_created(pid)
    if actual is None:
        return False
    return created is None or int(created) == actual


def terminate(pid):
    """Hard-stop one process. True if it was running and is now told to go."""
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(int(pid), 15)
            return True
        except (OSError, ValueError):
            return False
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, 1))
    finally:
        k32.CloseHandle(handle)


def kill_tree(pid):
    """Stop a process and everything it started."""
    if not pid:
        return
    if IS_WINDOWS:
        try:
            run_quiet(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                      timeout=30)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    else:
        terminate(pid)


_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9 ._()-]+$")


def find_processes(image_name):
    """[(pid, command line)] for every process with this image name.

    Windows has no pgrep; CIM is the supported way to read another
    process's command line. The output is forced to UTF-8 because a
    command line can carry a prompt in any language.
    """
    if not IS_WINDOWS or not _IMAGE_NAME_RE.match(image_name or ""):
        return []
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$p = @(Get-CimInstance Win32_Process -Filter \"Name='%s'\" "
        "| Select-Object ProcessId, CommandLine); "
        "ConvertTo-Json -InputObject $p -Compress" % image_name)
    try:
        r = run_quiet(["powershell.exe", "-NoProfile", "-NonInteractive",
                       "-Command", script], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        rows = json.loads(r.stdout.strip() or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            out.append((int(row.get("ProcessId")),
                        str(row.get("CommandLine") or "")))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


# --- state files ---------------------------------------------------------


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, data, attempts=20):
    """tmp + rename, retried: Windows refuses to replace an open file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def read_spec(name):
    return _read_json(spec_path(name))


def read_state(name):
    return _read_json(state_path(name))


# --- Task Scheduler ------------------------------------------------------


def pythonw_for(python=None):
    """The windowless interpreter next to `python`, or `python` itself.

    Covers every build's naming, not just python.exe: the py launcher can
    hand back python3.13t.exe (free-threaded), whose windowless twin is
    pythonw3.13t.exe.
    """
    python = python or sys.executable
    folder, exe = os.path.split(python)
    low = exe.lower()
    if low.startswith("python") and not low.startswith("pythonw"):
        candidate = os.path.join(folder, "pythonw" + exe[len("python"):])
        if os.path.exists(candidate):
            return candidate
    return python


def _drop_console():
    """Detach the supervisor from any console it was started with.

    It should run under pythonw and have none, but if the task ended up
    pointing at a console interpreter, Task Scheduler gives it a visible
    window -- and closing that window kills the supervisor and, through
    the job object, the service with it.
    """
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if k32.GetConsoleWindow():
            k32.FreeConsole()
    except (OSError, AttributeError):
        pass


def current_user():
    """DOMAIN\\user for the task's principal and logon trigger."""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            secur32 = ctypes.WinDLL("secur32")
            secur32.GetUserNameExW.argtypes = [
                ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
            size = wintypes.ULONG(512)
            buf = ctypes.create_unicode_buffer(size.value)
            if secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
                return buf.value
        except (OSError, AttributeError):
            pass
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN")
    return "%s\\%s" % (domain, user) if domain else user


def quote_arg(arg):
    return subprocess.list2cmdline([arg])


def task_xml(name, command, arguments, workdir, user, description="",
             at_logon=True):
    """Task definition: per-user logon trigger, no time limit, no battery
    rules, one instance, normal priority (the default of 7 is below
    normal, and children would inherit it).

    With `at_logon` off the trigger is kept but disabled: the task still
    starts on demand (bridgemon, setup), just not at sign-in.
    """
    e = _xml_escape
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>{logon}</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".format(desc=e(description or name), name=e(name), user=e(user),
           command=e(command), arguments=e(arguments), workdir=e(workdir),
           logon="true" if at_logon else "false")


def _schtasks(*args, timeout=60):
    return run_quiet(["schtasks.exe", *args], timeout=timeout,
                     encoding=None, errors="replace", text=True)


def _schtasks_message(result):
    return ((result.stderr or "") + (result.stdout or "")).strip()


def installed(name):
    """True if Task Scheduler has a task by this name."""
    if not IS_WINDOWS:
        return False
    try:
        return _schtasks("/Query", "/TN", name).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install(name, argv, cwd, stdout_log, stderr_log, env=None,
            throttle=DEFAULT_THROTTLE_S, description="", python=None,
            at_logon=None):
    """Register (or re-register) a service. Returns (ok, message).

    The spec is written first and the task only points at it, so changing
    what a service runs never needs the task recreated. `at_logon=None`
    keeps whatever the existing registration chose (on for a new one).
    """
    if at_logon is None:
        at_logon = read_spec(name).get("at_logon", True)
    spec = {
        "name": name,
        "argv": [str(a) for a in argv],
        "cwd": cwd,
        "stdout": stdout_log,
        "stderr": stderr_log,
        "env": {str(k): str(v) for k, v in (env or {}).items()},
        "throttle": throttle,
        "description": description,
        "at_logon": bool(at_logon),
        "task_command": pythonw_for(python),
    }
    write_json_atomic(spec_path(name), spec)
    return _register_task(name, spec)


def _register_task(name, spec):
    """(Re)create the scheduled task for a spec. Returns (ok, message).

    Replacing the definition leaves a running instance running.
    """
    script = os.path.join(HERE, "winsupport.py")
    command = spec.get("task_command") or pythonw_for()
    arguments = "%s run %s" % (quote_arg(script), quote_arg(name))
    xml = task_xml(name, command, arguments, HERE, current_user(),
                   spec.get("description") or "",
                   spec.get("at_logon", True))
    fd, xml_path = tempfile.mkstemp(prefix="aside-task-", suffix=".xml")
    os.close(fd)
    try:
        with open(xml_path, "w", encoding="utf-16") as fh:
            fh.write(xml)
        r = _schtasks("/Create", "/TN", name, "/XML", xml_path, "/F")
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass
    if r.returncode != 0:
        return False, _schtasks_message(r) or "schtasks exited %d" % (
            r.returncode)
    return True, "registered"


def autostart_enabled(name):
    """Whether a service starts at sign-in; None if it is not installed."""
    spec = read_spec(name)
    if not spec:
        return None
    return bool(spec.get("at_logon", True))


def set_autostart(name, enabled):
    """Turn a service's start-at-sign-in on or off. Returns (ok, message).

    Re-registers the task from its saved spec; whether the service is
    running right now is not affected.
    """
    spec = read_spec(name)
    if not spec:
        return False, "not installed"
    spec["at_logon"] = bool(enabled)
    spec.setdefault("task_command", pythonw_for())
    write_json_atomic(spec_path(name), spec)
    ok, msg = _register_task(name, spec)
    return ok, ("starts at sign-in" if enabled else "manual start only") \
        if ok else msg


# --- Aside at sign-in ----------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_RUN_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                    r"\StartupApproved\Run")
APPROVED_FOLDER_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                       r"\Explorer\StartupApproved\StartupFolder")
ASIDE_RUN_VALUE = "Aside"
ASIDE_SHORTCUT = "Aside.lnk"
SW_SHOWMINNOACTIVE = 7


def startup_folder():
    appdata = os.environ.get("APPDATA") or os.path.expanduser(
        os.path.join("~", "AppData", "Roaming"))
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def aside_shortcut_path():
    return os.path.join(startup_folder(), ASIDE_SHORTCUT)


def aside_app_path():
    """Aside.exe, where the Aside CLI itself looks for it, or None."""
    for base in (os.environ.get("LOCALAPPDATA"),
                 os.environ.get("ProgramFiles")):
        if base:
            candidate = os.path.join(base, "Aside", "Application", "Aside.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def _run_values():
    """{name: command} under HKCU\\...\\Run."""
    import winreg
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                out[name] = str(value)
                i += 1
    except OSError:
        pass
    return out


def _starts_aside(command):
    return "\\aside\\application\\aside.exe" in (command or "").lower()


def _startup_disabled(approved_key, name):
    """True if the entry was switched off in Task Manager > Startup apps."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_key) as key:
            data, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return False
    return bool(data) and bool(data[0] & 1)


def _clear_startup_disabled(approved_key, name):
    """Undo a Task Manager switch-off, which outlives the entry itself."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_key, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def aside_autostart_enabled():
    """Whether Aside opens at sign-in: our startup shortcut, or any Run
    entry for Aside.exe, that Task Manager has not switched off."""
    if not IS_WINDOWS:
        return False
    if os.path.isfile(aside_shortcut_path()) and not _startup_disabled(
            APPROVED_FOLDER_KEY, ASIDE_SHORTCUT):
        return True
    return any(_starts_aside(cmd) and not _startup_disabled(
        APPROVED_RUN_KEY, name) for name, cmd in _run_values().items())


def _remove_aside_run_entries():
    import winreg
    stale = [name for name, cmd in _run_values().items()
             if name == ASIDE_RUN_VALUE or _starts_aside(cmd)]
    if not stale:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        for name in stale:
            winreg.DeleteValue(key, name)


def set_aside_autostart(enabled):
    """Add or remove Aside from the programs Windows opens at sign-in.

    Aside has no windowless mode -- `--no-startup-window` makes the browser
    exit at once, and the daemon with it -- so it opens minimized instead:
    a shortcut in the Startup folder with "Run: Minimized", which is how
    Explorer starts it at sign-in. Aside.exe gets no arguments, as when the
    Aside CLI opens it. Returns (ok, message).
    """
    shortcut = aside_shortcut_path()
    if not enabled:
        try:
            os.remove(shortcut)
        except FileNotFoundError:
            pass
        _remove_aside_run_entries()
        return True, "does not open at sign-in"
    app = aside_app_path()
    if not app:
        return False, "Aside.exe not found -- is Aside installed?"
    os.makedirs(os.path.dirname(shortcut), exist_ok=True)
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "$env:ASIDE_LNK); $s.TargetPath = $env:ASIDE_EXE; "
        "$s.WorkingDirectory = Split-Path -Parent $env:ASIDE_EXE; "
        "$s.WindowStyle = %d; $s.Description = 'Aside (minimized)'; "
        "$s.Save()" % SW_SHOWMINNOACTIVE)
    env = dict(os.environ, ASIDE_LNK=shortcut, ASIDE_EXE=app)
    try:
        r = run_quiet(["powershell.exe", "-NoProfile", "-NonInteractive",
                       "-Command", script], timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "could not create the startup shortcut: %s" % e
    if r.returncode != 0 or not os.path.isfile(shortcut):
        return False, "could not create the startup shortcut: %s" % (
            (r.stderr or r.stdout).strip()[:200])
    _clear_startup_disabled(APPROVED_FOLDER_KEY, ASIDE_SHORTCUT)
    # An earlier version used a Run entry; two entries would open Aside twice.
    _remove_aside_run_entries()
    return True, "opens minimized at sign-in"


def status(name):
    """{installed, running, pid, supervisor_pid, last_exit} for a service."""
    st = read_state(name)
    sup = st.get("supervisor_pid")
    sup_alive = pid_alive(sup, st.get("supervisor_created"))
    child = st.get("pid")
    child_alive = sup_alive and pid_alive(child, st.get("pid_created"))
    return {
        "installed": installed(name),
        "running": bool(child_alive),
        "pid": child if child_alive else None,
        "supervisor_pid": sup if sup_alive else None,
        "last_exit": st.get("last_exit"),
        "started": st.get("started") if child_alive else None,
    }


def _spawn_detached_supervisor(name, python=None):
    """Start the supervisor without Task Scheduler (task missing/refused)."""
    argv = [pythonw_for(python), os.path.join(HERE, "winsupport.py"), "run",
            name]
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            subprocess.Popen(argv, cwd=HERE, creationflags=flags,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            return True
        except OSError:
            continue
    return False


def _wait(predicate, timeout, step=0.25):
    deadline = time.time() + timeout
    while True:
        if predicate():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(step)


def start(name, wait=15):
    """Start a service if it is not running. True once its process is up."""
    if status(name)["running"]:
        return True
    before = read_state(name).get("started")

    def up():
        s = read_state(name)
        return (s.get("started") != before
                and pid_alive(s.get("pid"), s.get("pid_created")))

    if installed(name) and _schtasks("/Run", "/TN", name).returncode == 0 \
            and _wait(up, wait):
        return True
    # No task, a refused /Run, or a task that never came up (an
    # interactive-only task with nobody logged on at the console): run
    # the supervisor directly. Its mutex keeps this from doubling up.
    if not read_spec(name) or not _spawn_detached_supervisor(name):
        return False
    return _wait(up, wait)


def stop(name, wait=15):
    """Stop the supervisor and, through its job object, the whole tree."""
    st = read_state(name)
    sup, sup_created = st.get("supervisor_pid"), st.get("supervisor_created")
    child, child_created = st.get("pid"), st.get("pid_created")
    if pid_alive(sup, sup_created):
        terminate(sup)
    if installed(name):
        try:
            _schtasks("/End", "/TN", name)
        except (OSError, subprocess.SubprocessError):
            pass

    def gone():
        return not pid_alive(sup, sup_created) and not pid_alive(
            child, child_created)
    if not _wait(gone, wait) and pid_alive(child, child_created):
        kill_tree(child)
        _wait(gone, 5)
    return gone()


def restart(name, wait=15):
    stop(name, wait)
    return start(name, wait)


def uninstall(name):
    """Stop and unregister a service. Returns (ok, message)."""
    stop(name)
    msg = "not registered"
    ok = True
    if installed(name):
        r = _schtasks("/Delete", "/TN", name, "/F")
        ok = r.returncode == 0
        msg = "removed" if ok else _schtasks_message(r)
    for path in (spec_path(name), state_path(name)):
        try:
            os.remove(path)
        except OSError:
            pass
    return ok, msg


# --- user PATH -----------------------------------------------------------


def _user_path_value():
    """(value, registry type) of the user's PATH, or ("", None)."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except OSError:
        return "", None
    return str(value), kind


def user_path_entries():
    if not IS_WINDOWS:
        return []
    value, _ = _user_path_value()
    return [p for p in value.split(";") if p]


def add_to_user_path(folder):
    """Append `folder` to the user's PATH. Returns (changed, message).

    Written to HKCU\\Environment, keeping the value's registry type (a new
    value is REG_EXPAND_SZ, as Windows creates it), then broadcast so new
    terminals see it without logging out.
    """
    import winreg
    norm = os.path.normcase(os.path.normpath(folder))
    value, kind = _user_path_value()
    entries = [p for p in value.split(";") if p]
    for entry in entries:
        expanded = os.path.expandvars(entry)
        if os.path.normcase(os.path.normpath(expanded)) == norm:
            return False, "already on PATH"
    entries.append(folder)
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        kind = winreg.REG_EXPAND_SZ
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(entries))
    try:
        import ctypes
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000,
            ctypes.byref(result))
    except (OSError, AttributeError):
        pass
    return True, "added"


# --- the supervisor ------------------------------------------------------


class _Job:
    """A job object that kills every process in it when the handle closes,
    which happens when the supervisor exits for any reason."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.k32 = _kernel32()
        self.handle = self.k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObject failed (%d)"
                          % ctypes.get_last_error())
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.k32.SetInformationJobObject(
                self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject failed (%d)"
                          % ctypes.get_last_error())

    def add(self, pid):
        handle = self.k32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(self.k32.AssignProcessToJobObject(self.handle,
                                                          handle))
        finally:
            self.k32.CloseHandle(handle)


def _single_instance(name):
    """Hold a named mutex for this service; False if another holds it."""
    import ctypes
    k32 = _kernel32()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    handle = k32.CreateMutexW(None, False, "Local\\aside-bridge-svc-" + safe)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return None
    return handle


def _rotate(path):
    try:
        if os.path.getsize(path) > LOG_ROTATE_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def _note(path, text):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s [service] %s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def run(name):
    """Supervise one service until terminated. Never returns normally."""
    spec = read_spec(name)
    if not spec.get("argv"):
        sys.stderr.write("no service spec at %s\n" % spec_path(name))
        return 2
    if IS_WINDOWS:
        _drop_console()
    mutex = _single_instance(name)
    if mutex is None:
        return 0
    err_log = spec.get("stderr") or os.path.join(service_home(),
                                                  name + ".err.log")
    out_log = spec.get("stdout") or os.path.join(service_home(),
                                                 name + ".out.log")
    for path in (err_log, out_log):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        job = _Job()
    except OSError as e:
        job = None
        _note(err_log, "no job object (%s); children may outlive a stop" % e)

    me = os.getpid()
    env = dict(os.environ)
    env.update(spec.get("env") or {})
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    throttle = max(1, int(spec.get("throttle") or DEFAULT_THROTTLE_S))
    last_exit = read_state(name).get("last_exit")

    while True:
        _rotate(out_log)
        _rotate(err_log)
        launched_at = time.time()
        try:
            with open(out_log, "ab") as out, open(err_log, "ab") as err:
                child = subprocess.Popen(
                    spec["argv"], cwd=spec.get("cwd") or None, env=env,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
        except OSError as e:
            _note(err_log, "could not start %s: %s" % (spec["argv"][0], e))
            child = None
        if child is not None:
            if job is not None:
                job.add(child.pid)
            write_json_atomic(state_path(name), {
                "supervisor_pid": me,
                "supervisor_created": process_created(me),
                "pid": child.pid,
                "pid_created": process_created(child.pid),
                "started": launched_at,
                "last_exit": last_exit,
            })
            last_exit = child.wait()
            _note(err_log, "process %d exited with code %s; restarting"
                  % (child.pid, last_exit))
            write_json_atomic(state_path(name), {
                "supervisor_pid": me,
                "supervisor_created": process_created(me),
                "pid": None,
                "started": None,
                "last_exit": last_exit,
            })
        time.sleep(max(1.0, throttle - (time.time() - launched_at)))


def main(argv):
    if len(argv) != 2 or argv[0] not in (
            "run", "status", "start", "stop", "restart", "uninstall"):
        print(__doc__)
        return 2
    cmd, name = argv
    if cmd == "run":
        try:
            return run(name)
        except Exception:  # noqa: BLE001 -- pythonw has nowhere to print
            import traceback
            _note(read_spec(name).get("stderr")
                  or os.path.join(service_home(), name + ".err.log"),
                  "supervisor crashed:\n" + traceback.format_exc())
            raise
    setup_console()
    if cmd == "status":
        print(json.dumps(status(name), indent=1))
        return 0
    if cmd == "uninstall":
        ok, msg = uninstall(name)
        print(msg)
        return 0 if ok else 1
    ok = {"start": start, "stop": stop, "restart": restart}[cmd](name)
    print("%s: %s" % (cmd, "ok" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
