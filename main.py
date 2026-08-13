import sublime

import sys
import logging

# Clear module cache to force reloading all modules of this package.
prefix = __package__ + "."  # don't clear the base package
for module_name in [
    module_name
    for module_name in sys.modules
    # subpackages and their contents, but never a top level sibling module such as
    # backend_info, which sublime scans and reloads by itself, pulling that one out of
    # sys.modules here would leave sublime holding a module it can no longer reload
    if module_name.startswith(prefix) and module_name != __name__ and
    ("." in module_name[len(prefix):] or
     hasattr(sys.modules[module_name], "__path__"))
]:
    del sys.modules[module_name]
del prefix

from .terminus.clipboard import TerminusClipboardHistoryUpdater  # noqa: E402
from .terminus.commands import (  # noqa: E402
    TerminusActivateCommand,
    TerminusCancelBuildCommand,
    TerminusClearUndoStackCommand,
    TerminusCloseAllCommand,
    TerminusCloseCommand,
    TerminusCopyCommand,
    TerminusCopyCommandOutputCommand,
    TerminusDeleteWordCommand,
    TerminusExecCommand,
    TerminusInitializeViewCommand,
    TerminusJumpToPromptCommand,
    TerminusKeypressCommand,
    TerminusMaximizeCommand,
    TerminusMinimizeCommand,
    TerminusOpenCommand,
    TerminusOpenShellFolderCommand,
    TerminusPasteCommand,
    TerminusPasteFromHistoryCommand,
    TerminusPasteTextCommand,
    TerminusRenameTitleCommand,
    TerminusResetCommand,
    TerminusSelectCommandOutputCommand,
    TerminusSendStringCommand,
    ToggleTerminusPanelCommand
)
from .terminus.event_listeners import (  # noqa: E402
    TerminusCoreEventListener
)
from .terminus.mouse import (  # noqa: E402
    TerminusClickCommand,
    TerminusMouseEventListener,
    TerminusOpenContextUrlCommand,
    TerminusOpenImageCommand
)
from .terminus.query import TerminusQueryContextListener  # noqa: E402
from .terminus.render import (  # noqa: E402
    TerminusCleanupCommand,
    TerminusRenderCommand,
    TerminusShowCursorCommand
)
from .terminus.terminal import Terminal  # noqa: E402
from .terminus.theme import (  # noqa: E402
    TerminusGenerateThemeCommand,
    TerminusSelectThemeCommand,
    plugin_loaded as theme_plugin_loaded,
    plugin_unloaded as theme_plugin_unloaded
)
from .terminus.utils import set_settings_on_change  # noqa: E402
from .terminus.view import (  # noqa: E402
    TerminusInsertCommand,
    TerminusNukeCommand,
    TerminusTrimTrailingLinesCommand
)


__all__ = [
    "TerminusActivateCommand",
    "TerminusCancelBuildCommand",
    "TerminusCleanupCommand",
    "TerminusClearUndoStackCommand",
    "TerminusClickCommand",
    "TerminusClipboardHistoryUpdater",
    "TerminusCloseAllCommand",
    "TerminusCloseCommand",
    "TerminusCopyCommand",
    "TerminusCopyCommandOutputCommand",
    "TerminusCoreEventListener",
    "TerminusDeleteWordCommand",
    "TerminusExecCommand",
    "TerminusGenerateThemeCommand",
    "TerminusInitializeViewCommand",
    "TerminusInsertCommand",
    "TerminusJumpToPromptCommand",
    "TerminusKeypressCommand",
    "TerminusMaximizeCommand",
    "TerminusMinimizeCommand",
    "TerminusMouseEventListener",
    "TerminusNukeCommand",
    "TerminusOpenCommand",
    "TerminusOpenContextUrlCommand",
    "TerminusOpenImageCommand",
    "TerminusOpenShellFolderCommand",
    "TerminusPasteCommand",
    "TerminusPasteFromHistoryCommand",
    "TerminusPasteTextCommand",
    "TerminusQueryContextListener",
    "TerminusRenameTitleCommand",
    "TerminusRenderCommand",
    "TerminusResetCommand",
    "TerminusSelectCommandOutputCommand",
    "TerminusSelectThemeCommand",
    "TerminusSendStringCommand",
    "TerminusShowCursorCommand",
    "TerminusTrimTrailingLinesCommand",
    "ToggleTerminusPanelCommand"
]


logger = logging.getLogger('Terminus')


def plugin_loaded():
    theme_plugin_loaded()

    if not logger.hasHandlers():
        ch = logging.StreamHandler(sys.stdout)
        logger.addHandler(ch)

    settings = sublime.load_settings("Terminus.sublime-settings")

    def on_change(debug):
        if debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.WARNING)

    on_change(settings.get("debug", False))
    set_settings_on_change(settings, "debug", on_change)


def plugin_unloaded():
    # close all terminals
    for w in sublime.windows():
        w.run_command("terminus_close_all")

    # a detached terminal, e.g. one which is being maximized or minimized while the
    # package is reloaded, is hosted by no view and is missed by the loop above
    for terminal in list(Terminal._detached_terminals):
        try:
            terminal.kill()
        except Exception as e:
            logger.error("error killing detached terminal: {}".format(e))
    Terminal._detached_terminals.clear()

    theme_plugin_unloaded()
    settings = sublime.load_settings("Terminus.sublime-settings")
    set_settings_on_change(settings, "debug", None)
