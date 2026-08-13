import sublime

import os
import time
import base64
import logging
import tempfile
import threading
from queue import Queue, Empty

from .ptty import TerminalPtyProcess, TerminalScreen, TerminalStream
from .utils import responsive, intermission
from .view import get_panel_window, panel_is_visible, view_is_visible, view_size
from .key import get_key_code
from .image import get_image_info, image_resize


IMAGE = """
<style>
body {{
    margin: 1px;
}}
</style>
<img src="data:image/{what};base64,{data}" width="{width}" height="{height}"/>
"""

logger = logging.getLogger('Terminus')

# how often a rejected resize to the very same size is retried before it is given up on
MAX_RESIZE_ATTEMPTS = 5


class Terminal:
    _terminals = {}
    _detached_terminals = []

    def __init__(self, view=None):
        self.view = view
        self.process = None
        self._cached_cursor = [0, 0]
        self._size = sublime.load_settings('Terminus.sublime-settings').get('size', (None, None))
        self._cached_cursor_is_hidden = [True]
        self.image_count = 0
        self.images = {}
        # whether the child process runs under TERM=linux, see the unix_term setting
        self.linux_mode = False
        self._strings = Queue()
        self._pending_to_send_string = [False]
        self._pending_to_clear_scrollback = [False]
        self._pending_to_reset = [None]
        # the size the pty was last known to agree on, None until the process is spawned
        self._pty_size = None
        self._failed_size = None
        self._resize_failures = 0
        self.lock = threading.Lock()

    @classmethod
    def from_id(cls, vid):
        if vid not in cls._terminals:
            return None
        return cls._terminals[vid]

    @classmethod
    def from_tag(cls, tag, current_window_only=True):
        # restrict to only current window
        for terminal in cls._terminals.values():
            if terminal.tag == tag:
                if current_window_only:
                    active_window = sublime.active_window()
                    if terminal.window and active_window:
                        if terminal.window == active_window:
                            return terminal
                else:
                    return terminal
        return None

    @classmethod
    def cull_terminals(cls):
        terminals_to_kill = []
        for terminal in cls._terminals.values():
            if not terminal.is_hosted():
                terminals_to_kill.append(terminal)

        for terminal in terminals_to_kill:
            terminal.kill()

    @property
    def window(self):
        if self.detached:
            return None
        if self.show_in_panel:
            return get_panel_window(self.view)
        else:
            return self.view.window()

    def attach_view(self, view, offset=None):
        with self.lock:
            self.view = view
            self.detached = False
            Terminal._terminals[view.id()] = self
            if self in Terminal._detached_terminals:
                Terminal._detached_terminals.remove(self)
            # allow screen to be rerendered
            self.screen.dirty.update(range(self.screen.lines))
            self.set_offset(offset)

    def detach_view(self):
        with self.lock:
            self.detached = True
            Terminal._detached_terminals.append(self)
            if self.view.id() in Terminal._terminals:
                del Terminal._terminals[self.view.id()]
            self.view = None

    @responsive(period=1, default=True)
    def is_hosted(self):
        if self.detached:
            # irrelevant if terminal is detached
            return True
        return self.window is not None

    def can_be_resized(self):
        # an unmeasurable viewport, e.g. during a pane drag, a layout change or when the
        # window is minimized, would report a bogus size, better not to resize at all
        if self.detached or not self.view:
            return False
        if self._size and all(self._size):
            # the size is forced by the settings, it does not depend on the viewport
            return True
        if self.show_in_panel:
            if not panel_is_visible(self.view):
                return False
        elif not view_is_visible(self.view):
            return False
        return all(self.view.viewport_extent())

    def _need_to_render(self):
        flag = False
        if self.screen.dirty:
            flag = True
        elif self.screen.cursor.x != self._cached_cursor[0] or \
                self.screen.cursor.y != self._cached_cursor[1]:
            flag = True
        elif self.screen.cursor.hidden != self._cached_cursor_is_hidden[0]:
            flag = True

        if flag:
            self._cached_cursor[0] = self.screen.cursor.x
            self._cached_cursor[1] = self.screen.cursor.y
            self._cached_cursor_is_hidden[0] = self.screen.cursor.hidden
        return flag

    def _start_rendering(self):
        data = [""]
        done = [False]

        @responsive(period=1, default=False)
        def was_resized():
            # keep the current size if the viewport cannot be measured
            size = tuple(view_size(
                self.view, default=(self.screen.lines, self.screen.columns), force=self._size))
            if self._pty_size == size and \
                    self.screen.lines == size[0] and self.screen.columns == size[1]:
                # both the pty and the screen are already at the requested size
                return False
            if self._failed_size == size and self._resize_failures >= MAX_RESIZE_ATTEMPTS:
                # this size keeps being rejected, stop retrying it every second
                return False
            return self.can_be_resized()

        def reader():
            while True:
                try:
                    temp = self.process.read(1024)
                except EOFError:
                    break

                with self.lock:
                    data[0] += temp

                    if done[0] or not self.is_hosted():
                        logger.debug("reader breaks")
                        break

            done[0] = True

        threading.Thread(target=reader).start()

        def renderer():

            def feed_data():
                if len(data[0]) > 0:
                    logger.debug("receieved: %s", data[0])
                    try:
                        self.stream.feed(data[0])
                    except Exception as e:
                        # an exception escaping here would kill the renderer thread for good
                        logger.error("error feeding data: %s", e)
                    finally:
                        # always drop the data, otherwise a poisonous sequence would be
                        # fed again on every tick
                        data[0] = ""

            while True:
                with intermission(period=0.03), self.lock:
                    feed_data()
                    if not self.detached:
                        try:
                            if was_resized():
                                self.handle_resize()
                                self.view.run_command("terminus_show_cursor")

                            if self._need_to_render():
                                self.view.run_command("terminus_render")
                                self.screen.dirty.clear()
                        except Exception as e:
                            logger.error("error rendering: %s", e)

                    if done[0] or not self.is_hosted():
                        logger.debug("renderer breaks")
                        break

            feed_data()
            done[0] = True

            def _cleanup():
                if self.view:
                    self.view.run_command("terminus_cleanup")

            sublime.set_timeout(_cleanup)

        threading.Thread(target=renderer).start()

    def set_offset(self, offset=None):
        if offset is not None:
            self.offset = offset
        else:
            if self.view and self.view.size() > 0:
                view = self.view
                self.offset = view.rowcol(view.size())[0] + 1
            else:
                self.offset = 0
        logger.debug("activating with offset %s", self.offset)

    def start(
            self, cmd, cwd=None, env=None, default_title=None, title=None,
            show_in_panel=None, panel_name=None, tag=None, auto_close=True, cancellable=False,
            timeit=False):

        view = self.view
        self.detached = view is None

        self.show_in_panel = show_in_panel
        self.panel_name = panel_name
        self.tag = tag
        self.auto_close = auto_close
        self.cancellable = cancellable
        self.timeit = timeit
        if timeit:
            self.start_time = time.time()
        self.default_title = default_title
        self.title = title

        if view:
            self.set_offset()

        size = view_size(
            view or sublime.active_window().active_view(), default=(40, 80), force=self._size)
        logger.debug("view size: {}".format(str(size)))
        _env = os.environ.copy()
        _env.update(env)
        # the linux console home and end codes differ from the xterm ones, key.py stays
        # settings free so the flag is derived here from the TERM the process gets
        self.linux_mode = _env.get("TERM") == "linux"
        try:
            self.process = TerminalPtyProcess.spawn(cmd, cwd=cwd, env=_env, dimensions=size)
        except Exception as e:
            self.process = None
            logger.error("error spawning {}: {}".format(cmd, e))
            # a terminal without a process must not be left registered
            if view and Terminal._terminals.get(view.id()) is self:
                del Terminal._terminals[view.id()]
            if self in Terminal._detached_terminals:
                Terminal._detached_terminals.remove(self)
            if view:
                # the view is no longer backed by a terminal, mark it finished so that
                # on_activated does not keep reactivating a command which cannot spawn
                view.settings().set("terminus_view.finished", True)
            raise
        # the pty was spawned with these dimensions, so it already agrees with the screen
        self._pty_size = tuple(size)
        self.screen = TerminalScreen(
            size[1], size[0], process=self.process, history=10000,
            clear_callback=self.clear_callback, reset_callback=self.reset_callback)
        self.stream = TerminalStream(self.screen)

        self.screen.set_show_image_callback(self.show_image)

        # only publish the terminal once the process is running
        if view:
            Terminal._terminals[view.id()] = self
        else:
            Terminal._detached_terminals.append(self)

        self._start_rendering()

    def kill(self):
        logger.debug("kill")

        if self.process:
            self.process.terminate()
        view = self.view
        if view:
            # view is None if the terminal has been detached
            vid = view.id()
            if vid in self._terminals:
                del self._terminals[vid]

    def handle_resize(self):
        # keep the current size if the viewport cannot be measured
        size = tuple(view_size(
            self.view, default=(self.screen.lines, self.screen.columns), force=self._size))
        logger.debug("handle resize {} {} -> {} {}".format(
            self.screen.lines, self.screen.columns, size[0], size[1]))
        try:
            # pywinpty will rasie an runtime error, newer versions raise a WinptyError
            # which is not a RuntimeError, hence the broad except below
            if self.process:
                self.process.setwinsize(*size)
            # the screen is only resized once the pty agreed on the new geometry,
            # otherwise the child process and the screen would disagree on the number
            # of columns and every line would be wrapped at the wrong place
            self.screen.resize(*size)
        except Exception as e:
            # count the failures so that a size which is rejected over and over is
            # eventually given up on instead of being retried forever
            if self._failed_size == size:
                self._resize_failures += 1
            else:
                self._failed_size = size
                self._resize_failures = 1
            logger.error("error resizing to {}: {}".format(size, e))
            return
        self._pty_size = size
        self._failed_size = None
        self._resize_failures = 0

    def clear_callback(self):
        self._pending_to_clear_scrollback[0] = True

    def reset_callback(self):
        if self._pending_to_reset[0] is None:
            self._pending_to_reset[0] = False
        else:
            self._pending_to_reset[0] = True

    def send_key(self, *args, **kwargs):
        kwargs["application_mode"] = self.application_mode_enabled()
        kwargs["new_line_mode"] = self.new_line_mode_enabled()
        kwargs["linux_mode"] = self.linux_mode
        self.send_string(get_key_code(*args, **kwargs), normalized=False)

    def send_string(self, string, normalized=True):
        if normalized:
            # normalize CR and CRLF to CR (or CRLF if LNM)
            string = string.replace("\r\n", "\n")
            if self.new_line_mode_enabled():
                string = string.replace("\n", "\r\n")
            else:
                string = string.replace("\n", "\r")

        no_queue = not self._pending_to_send_string[0]
        if no_queue and len(string) <= 512:
            logger.debug("sent: {}".format(string[0:64] if len(string) > 64 else string))
            self.process.write(string)
        else:
            for i in range(0, len(string), 512):
                self._strings.put(string[i:i+512])
            if no_queue:
                self._pending_to_send_string[0] = True
                threading.Thread(target=self.process_send_string).start()

    def process_send_string(self):
        while True:
            try:
                string = self._strings.get(False)
                logger.debug("sent: {}".format(string[0:64] if len(string) > 64 else string))
                self.process.write(string)
            except Empty:
                self._pending_to_send_string[0] = False
                return
            else:
                time.sleep(0.1)

    def bracketed_paste_mode_enabled(self):
        return (2004 << 5) in self.screen.mode

    def new_line_mode_enabled(self):
        return (20 << 5) in self.screen.mode

    def application_mode_enabled(self):
        return (1 << 5) in self.screen.mode

    def find_image(self, pt):
        view = self.view
        for pid in self.images:
            region = view.query_phantom(pid)[0]
            if region.end() == pt:
                return pid
        return None

    def show_image(self, data, args, cr=None):
        view = self.view

        if "inline" not in args or not args["inline"]:
            return

        cursor = self.screen.cursor
        pt = view.text_point(self.offset + cursor.y, cursor.x)

        databytes = base64.decodebytes(data.encode())

        image_info = get_image_info(databytes)
        if not image_info:
            logger.error("cannot get image info")
            return

        what, width, height = image_info

        _, image_path = tempfile.mkstemp(suffix="." + what)
        with open(image_path, "wb") as f:
            f.write(databytes)

        width, height = image_resize(
            width,
            height,
            args["width"] if "width" in args else None,
            args["height"] if "height" in args else None,
            view.em_width(),
            view.viewport_extent()[0] - 3 * view.em_width(),
            args["preserveAspectRatio"] if "preserveAspectRatio" in args else 1
        )

        if self.find_image(pt):
            self.view.run_command("terminus_insert", {"point": pt, "character": " "})
            pt += 1

        self.image_count += 1
        p = view.add_phantom(
            "terminus_image#{}".format(self.image_count),
            sublime.Region(pt, pt),
            IMAGE.format(
                what=what,
                data=data,
                width=width,
                height=height,
                count=self.image_count),
            sublime.LAYOUT_INLINE,
        )
        self.images[p] = image_path

        if cr:
            self.screen.index()

    def clean_images(self):
        view = self.view
        for pid in list(self.images.keys()):
            region = view.query_phantom(pid)[0]
            if region.empty() and region.begin() == 0:
                view.erase_phantom_by_id(pid)
                if pid in self.images:
                    try:
                        os.remove(self.images[pid])
                    except Exception:
                        pass
                    del self.images[pid]

    def __del__(self):
        # make sure the process is terminated
        if self.process:
            self.process.terminate(force=True)

        # remove images
        for image_path in list(self.images.values()):
            try:
                os.remove(image_path)
            except Exception:
                pass

        if self.process and self.process.isalive():
            logger.debug("process becomes orphaned")
        else:
            logger.debug("process is terminated")
