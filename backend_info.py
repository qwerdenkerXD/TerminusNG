# this module lives at the top of the package on purpose. sublime only scans the top
# level for plugins, and it must not be reached through main.py, whose import chain runs
# `from winpty import PtyProcess` in terminus/ptty.py. a winpty which cannot load is the
# very failure this diagnostic exists for, and it would take main.py, and with it every
# command registered there, down with it. nothing here imports from terminus/.
import sublime
import sublime_plugin

import os
import sys
import inspect
import logging
import importlib.util


logger = logging.getLogger('Terminus')


# ConPTY landed in Windows 10 1809, older hosts can only ever run the legacy agent
CONPTY_MIN_BUILD = 17763

# mirrors the list `terminus_open` validates TERM against in terminus/commands.py
SUPPORTED_TERMS = ["linux", "xterm", "xterm-16color", "xterm-256color"]

# the compiled artifacts worth naming in the winpty package directory: the ABI tag of
# the extension is the diagnosis when the import fails, and an agent/dll pair proves
# the installed backend is the legacy screen scraping one
INTERESTING_SUFFIXES = (".pyd", ".dll", ".exe", ".so")

WIDTH = 22


def field(label, value):
    return "  {:<{width}}: {}".format(label, value, width=WIDTH)


def describe_error(e):
    return "{}: {}".format(type(e).__name__, e)


def probe(title, func):
    """
    Run a single probe, turning any failure into a reported line instead of an
    exception. A diagnostic which crashes on the machine being diagnosed is worthless.
    """
    lines = [title]
    try:
        lines.extend(func())
    except Exception as e:
        lines.append(field("probe failed", describe_error(e)))
    lines.append("")
    return lines


def major_version(version):
    """
    Leading integer of a version string, None if it cannot be read.
    """
    if not isinstance(version, str):
        return None
    head = version.strip().split(".")[0]
    try:
        return int(head)
    except ValueError:
        return None


def module_lines(name):
    """
    Importability, location and version of a plain python module.
    """
    lines = []
    try:
        module = importlib.import_module(name)
    except ImportError as e:
        # kept distinct from other failures, a missing or unloadable binary reports here
        lines.append(field("import", "ImportError: {}".format(e)))
        return lines
    except Exception as e:
        lines.append(field("import", "failed: {}".format(describe_error(e))))
        return lines

    lines.append(field("import", "ok"))
    lines.append(field("__file__", getattr(module, "__file__", "<none>")))
    lines.append(field("__version__", getattr(module, "__version__", "<none>")))
    return lines


def host_lines():
    lines = []
    lines.append(field("sys.version", sys.version.replace("\n", " ")))
    lines.append(field("sys.platform", sys.platform))
    lines.append(field("sys.executable", sys.executable))
    try:
        lines.append(field("sublime build", sublime.version()))
        lines.append(field("sublime platform", sublime.platform()))
        lines.append(field("sublime arch", sublime.arch()))
        channel = getattr(sublime, "channel", None)
        if channel:
            lines.append(field("sublime channel", channel()))
    except Exception as e:
        lines.append(field("sublime", "failed: {}".format(describe_error(e))))
    return lines


def conpty_lines():
    """
    Whether the host could run ConPTY at all, independent of what pywinpty offers.
    """
    lines = []
    if not sys.platform.startswith("win"):
        lines.append(field("conpty capable", "n/a, not a Windows host"))
        return lines

    # sys.getwindowsversion is GetVersionEx, whose answer is gated by the manifest of the
    # host executable and by per exe compatibility shims, so it happily reports 6.2 build
    # 9200 on a windows 11 machine. it is a data point, the kernel32 export below is the
    # verdict, because that is the api ConPTY actually needs
    getwindowsversion = getattr(sys, "getwindowsversion", None)
    build = None
    if not getwindowsversion:
        lines.append(field("windows version", "unknown, sys.getwindowsversion missing"))
    else:
        version = getwindowsversion()
        build = getattr(version, "build", None)
        lines.append(field("windows version", "{}.{} build {}".format(
            getattr(version, "major", "?"), getattr(version, "minor", "?"), build)))

    exported = None
    try:
        import ctypes
        exported = hasattr(ctypes.WinDLL("kernel32"), "CreatePseudoConsole")
        lines.append(field("CreatePseudoConsole", "present" if exported else "absent"))
    except Exception as e:
        lines.append(field("CreatePseudoConsole", "unreadable: {}".format(
            describe_error(e))))

    if exported is not None:
        lines.append(field("conpty capable", "yes" if exported else
                           "no, kernel32 does not export CreatePseudoConsole"))
    elif isinstance(build, int):
        lines.append(field("conpty capable", "probably, build {} >= {}, the api probe "
                                             "failed".format(build, CONPTY_MIN_BUILD)
                           if build >= CONPTY_MIN_BUILD else
                           "no, build {} < {}".format(build, CONPTY_MIN_BUILD)))
    else:
        lines.append(field("conpty capable", "unknown, no api export and no build number"))
    return lines


def winpty_location_lines():
    """
    Locate the winpty package without importing it, so that the compiled artifacts can
    be named even when loading them is exactly what fails.
    """
    lines = []
    spec = importlib.util.find_spec("winpty")
    if spec is None:
        lines.append(field("find_spec", "not found on sys.path"))
        return lines

    origin = getattr(spec, "origin", None)
    lines.append(field("find_spec origin", origin))

    if not origin or not os.path.isfile(origin):
        return lines

    pkg_dir = os.path.dirname(origin)
    try:
        names = sorted(os.listdir(pkg_dir))
    except Exception as e:
        lines.append(field("package dir", "unreadable: {}".format(describe_error(e))))
        return lines

    binaries = [name for name in names if name.lower().endswith(INTERESTING_SUFFIXES)]
    if binaries:
        lines.append(field("binaries", ", ".join(binaries)))
    else:
        lines.append(field("binaries", "none found in {}".format(pkg_dir)))
    return lines


def winpty_lines():
    """
    The interesting probe: which pywinpty is installed and which backend it would pick.
    """
    lines = []
    if not sys.platform.startswith("win"):
        lines.append(field("winpty", "n/a, not a Windows host"))
        return lines

    try:
        lines.extend(winpty_location_lines())
    except Exception as e:
        lines.append(field("find_spec", "failed: {}".format(describe_error(e))))

    try:
        import winpty
    except ImportError as e:
        if isinstance(e, ModuleNotFoundError) and getattr(e, "name", None) == "winpty":
            # nothing to load at all, which is a different finding from a broken load
            lines.append(field("import", "not installed: {}".format(e)))
            return lines
        # the single most likely finding: a cp33 extension built in 2019 cannot load in
        # the python 3.8 plugin host, and the loader message is the whole diagnosis
        lines.append(field("import", "ImportError: {}".format(e)))
        lines.append(field("diagnosis", "winpty is present but cannot load in this host, "
                                        "most likely an extension built for another "
                                        "python ABI, see upstream Terminus issue #368"))
        return lines
    except Exception as e:
        lines.append(field("import", "failed: {}".format(describe_error(e))))
        return lines

    lines.append(field("import", "ok"))
    lines.append(field("__file__", getattr(winpty, "__file__", "<none>")))
    version = getattr(winpty, "__version__", None)
    lines.append(field("__version__", version if version else "<none>"))
    forced = os.environ.get("PYWINPTY_BACKEND", None)
    lines.append(field("PYWINPTY_BACKEND", forced if forced else "<unset>"))

    backends = getattr(getattr(winpty, "enums", None), "Backend", None)
    if backends is not None:
        members = []
        try:
            # a plain python enum iterates, the rust backed class of pywinpty 2.x
            # does not, so fall back to reading the members we know it defines
            members = ["{}={}".format(m.name, m.value) for m in backends]
        except TypeError:
            for name in ["ConPTY", "WinPTY"]:
                value = getattr(backends, name, None)
                if value is not None:
                    members.append("{}={}".format(name, getattr(value, "value", value)))
        except Exception as e:
            members = ["unreadable: {}".format(describe_error(e))]
        lines.append(field("enums.Backend", ", ".join(members) if members else "<empty>"))
    else:
        lines.append(field("enums.Backend", "<none>"))

    spawn = getattr(getattr(winpty, "PtyProcess", None), "spawn", None)
    if spawn is None:
        lines.append(field("PtyProcess.spawn", "<none>"))
        return lines

    try:
        signature = inspect.signature(spawn)
    except Exception as e:
        lines.append(field("PtyProcess.spawn", "signature unreadable: {}".format(
            describe_error(e))))
        return lines

    lines.append(field("spawn signature", str(signature)))
    # the backend parameter is present from pywinpty 1.0 on, which is where ConPTY
    # support arrived, so it is a reliable probe for a modern pywinpty
    parameter = signature.parameters.get("backend", None)
    lines.append(field("spawn backend arg", "yes, default {}".format(parameter.default)
                       if parameter is not None else "no"))

    major = major_version(version)
    if parameter is None:
        selected = "legacy WinPTY only, pywinpty < 1.0 with the screen scraping agent"
    elif forced:
        # every pywinpty from 1.0 on honours it at spawn time, not only the 1.x line
        selected = "forced by PYWINPTY_BACKEND={}".format(forced)
    elif major is not None and major >= 2:
        selected = "auto, ConPTY when the host supports it, falling back to WinPTY"
    elif major == 1:
        selected = "auto, ConPTY when the host supports it, falling back to WinPTY"
    else:
        selected = "pywinpty >= 1.0 by the backend argument, version unreadable"
    lines.append(field("backend selected", selected))
    return lines


def ptyprocess_lines():
    if sys.platform.startswith("win"):
        return [field("ptyprocess", "n/a, Windows uses winpty")]
    return module_lines("ptyprocess")


def term_lines():
    """
    What TERM a process spawned by terminus_open would actually receive, following the
    same resolution terminus/commands.py performs.
    """
    lines = []
    settings = sublime.load_settings("Terminus.sublime-settings")
    unix_term = settings.get("unix_term", "linux")
    lines.append(field("unix_term setting", unix_term))
    lines.append(field("os.environ TERM", os.environ.get("TERM", "<unset>")))

    if sys.platform.startswith("win"):
        # unix_term is not applied on Windows, a plain windows command only sees the
        # inherited TERM, while a wsl.exe command gets one shared into the distribution
        # by share_env_with_wsl, see terminus/commands.py
        lines.append(field("spawned TERM", os.environ.get("TERM", "<unset>")))
        lines.append(field("wsl.exe TERM", "xterm-256color, shared through WSLENV"))
        lines.append(field("note", "the config or the caller may pass TERM in env, "
                                   "which wins on both paths"))
        return lines

    # commands.py only falls back to unix_term when TERM is absent from the merged
    # config and caller env, an explicit TERM there wins and is validated the same way
    lines.append(field("spawned TERM", "{}, unless the config or the caller passes TERM "
                                       "in env".format(unix_term)))
    if unix_term not in SUPPORTED_TERMS:
        lines.append(field("supported", "no, terminus_open raises for this value"))
    else:
        lines.append(field("supported", "yes"))
    # key.py emits the linux console home and end codes when the child runs TERM=linux
    lines.append(field("linux_mode", "yes" if unix_term == "linux" else "no"))
    return lines


def collect_report():
    lines = ["", "Terminus backend info", "=====================", ""]
    lines.extend(probe("host", host_lines))
    lines.extend(probe("conpty", conpty_lines))
    lines.extend(probe("winpty (windows pty backend)", winpty_lines))
    lines.extend(probe("ptyprocess (unix pty backend)", ptyprocess_lines))
    lines.extend(probe("pyte", lambda: module_lines("pyte")))
    lines.extend(probe("wcwidth", lambda: module_lines("wcwidth")))
    lines.extend(probe("term", term_lines))
    return lines


class TerminusBackendInfoCommand(sublime_plugin.WindowCommand):
    """
    Print what the pty backend actually is on this machine, rather than what it is
    assumed to be. Never raises, every probe reports its own failure as data.
    """

    def run(self):
        sublime.set_timeout_async(self.run_async)

    def run_async(self):
        try:
            report = "\n".join(collect_report())
        except Exception as e:
            logger.error("error collecting backend info: {}".format(e))
            return

        print(report)
        sublime.status_message("Terminus backend info printed to the console")
