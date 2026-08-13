import logging

import sublime

from .const import EXEC_PANEL
from .terminal import Terminal
from .view import get_panel_window

logger = logging.getLogger('Terminus')


class RecencyManager:
    _instances = {}

    @classmethod
    def _evict_instances(cls):
        # forget the windows which have been closed, otherwise the dict would grow for
        # the whole session and every entry would keep a Window and the recent View
        # alive. sublime also reuses window ids within a session, so a stale entry would
        # hand a newly opened window the recent terminal of the closed window whose id it
        # inherited
        live_ids = set(w.id() for w in sublime.windows())
        # a snapshot, this is reached from both the async listener thread and the main
        # thread and a dict which changes size during iteration raises, hence also the
        # pop instead of a del
        for window_id in list(cls._instances.keys()):
            if window_id not in live_ids:
                cls._instances.pop(window_id, None)

    @classmethod
    def from_window(cls, window):
        if not window:
            return None
        # drop the managers of the closed windows before the lookup, so a new window
        # which reuses a closed window's id cannot inherit its recent terminal
        cls._evict_instances()
        # one lookup, not a check and a get: the sweep runs on both threads and could
        # drop the entry in between
        instance = cls._instances.get(window.id())
        if instance is not None:
            return instance
        instance = cls(window)
        cls._instances[window.id()] = instance
        return instance

    @classmethod
    def from_view(cls, view):
        window = get_panel_window(view)
        if not window:
            window = view.window()
        return cls.from_window(window)

    def __init__(self, window):
        self.window = window
        self.cycling_panels = False
        self._recent_panel = None
        self._recent_view = None

    def set_recent_terminal(self, view):
        window = self.window
        if not window:
            return
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        logger.debug("set recent view: {}".format(view.id()))
        if terminal.show_in_panel and terminal.panel_name != EXEC_PANEL:
            self._recent_panel = terminal.panel_name
            self._recent_view = view
        else:
            self._recent_view = view

    def recent_panel(self):
        window = self.window
        if not window:
            return
        panel_name = self._recent_panel
        if panel_name:
            view = window.find_output_panel(panel_name)
            if view and Terminal.from_id(view.id()):
                return panel_name

    def recent_view(self):
        window = self.window
        if not window:
            return
        view = self._recent_view
        if view:
            terminal = Terminal.from_id(view.id())
            if terminal:
                return view
