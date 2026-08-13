import re
import sys
import logging
import unicodedata
from copy import copy
from collections import defaultdict, deque, namedtuple
from wcwidth import wcwidth, wcswidth
from functools import lru_cache
from urllib.parse import unquote, urlparse

import pyte
from pyte.screens import StaticDefaultDict, Margins
from pyte import modes as mo
from pyte import graphics as g
from pyte import control as ctrl


if sys.platform.startswith("win"):
    from winpty import PtyProcess
    is_windows = True
else:
    from ptyprocess import PtyProcess
    is_windows = False


logger = logging.getLogger('Terminus')


ANSI_COLORS = [
    "black",
    "red",
    "green",
    "brown",
    "blue",
    "magenta",
    "cyan",
    "white",
    "light_black",
    "light_red",
    "light_green",
    "light_brown",
    "light_blue",
    "light_magenta",
    "light_cyan",
    "light_white"
]

FG_AIXTERM = {
    90: "light_black",
    91: "light_red",
    92: "light_green",
    93: "light_brown",
    94: "light_blue",
    95: "light_magenta",
    96: "light_cyan",
    97: "light_white"
}

BG_AIXTERM = {
    100: "light_black",
    101: "light_red",
    102: "light_green",
    103: "light_brown",
    104: "light_blue",
    105: "light_magenta",
    106: "light_cyan",
    107: "light_white"
}

XTERM_256_COLORS = ANSI_COLORS + pyte.graphics.FG_BG_256


def uri_to_path(uri):
    """
    Turn the file:// uri of an OSC 7 report into a path. The hostname is dropped,
    a shell in a container or over ssh reports its own host and we have no way to
    reach it anyway. Returns None if this is not a usable file uri.
    """
    if not uri:
        return None
    try:
        parts = urlparse(uri)
    except Exception:
        return None
    if parts.scheme and parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if not path:
        return None
    # a windows shell reports file:///C:/Users/..., the slash in front of the drive
    # letter belongs to the uri, not to the path
    if re.match(r"^/[a-zA-Z]:", path):
        path = path[1:]
    if re.match(r"^[a-zA-Z]:", path):
        return path.replace("/", "\\")
    if path.startswith("/"):
        # a posix path, and on windows that means a wsl shell reporting a path from
        # inside the distribution. it is left as it is rather than turned into
        # something windows shaped, the caller decides whether it can reach it
        return path
    # OSC 7 always reports an absolute path, anything else is not a report we can
    # make sense of
    return None


MARK_PROMPT = "A"    # a fresh line, the prompt begins
MARK_INPUT = "B"     # the prompt ends, the user input begins
MARK_OUTPUT = "C"    # the user input ends, the command output begins
MARK_END = "D"       # the command finished, exit_code may be None
MARK_KINDS = (MARK_PROMPT, MARK_INPUT, MARK_OUTPUT, MARK_END)
# the shared empty result of line_marks, it is never mutated
EMPTY_MARKS = ()


def line_marks(buffer_line):
    """
    the OSC 133 marks a buffer line carries, this is the one place the attribute
    is read. the result is a tuple and must be treated as immutable, a line in the
    scrollback may be sharing it with the line it was copied from
    """
    return getattr(buffer_line, "semantic_marks", EMPTY_MARKS)


class SemanticMark(object):
    """
    one OSC 133 boundary the shell reported, it belongs to a buffer line and not to
    a view row, render.py turns it into a row when it writes that line out
    """
    __slots__ = ("kind", "exit_code")

    def __init__(self, kind, exit_code=None):
        self.kind = kind
        self.exit_code = exit_code

    def __repr__(self):
        return "SemanticMark({}{})".format(
            self.kind, "" if self.exit_code is None else ", " + str(self.exit_code))


# the only schemes a terminal has any business handing to the OS, everything else
# an OSC 8 sequence may name is refused rather than repaired
LINK_SCHEMES = ("http", "https", "file")
# a target longer than this is not a link anybody is meant to click, and a hostile
# sequence must not be able to grow the state of the screen without bound
MAX_LINK_URI_LENGTH = 2048
# the id= of a link only ties two runs of text together, a long one buys nothing
MAX_LINK_ID_LENGTH = 64
# a shell which opens a link and never closes it must not swallow the rest of the
# session, the link is dropped once it covered this many cells. that is several
# screens worth of text and far more than any label, so a link which really is
# that long is a broken emitter and not something worth keeping
MAX_LINK_CELLS = 8192
# how many distinct targets are kept around so that equal ones share one object. a
# build log with more links than this starts the sharing over instead of
# remembering every url it ever printed
MAX_LINK_CACHE = 256
# a control character, a raw space or a c1 byte inside a uri, a well formed one
# percent encodes every one of them
UNSAFE_IN_URI = re.compile(r"[\x00-\x20\x7f-\x9f]")
# the shared empty result of line_links, it is never mutated
EMPTY_LINKS = ()


def safe_link_uri(uri):
    """
    Check the target of an OSC 8 hyperlink and return the uri to store, or None
    when it must not be stored at all. Everything reaching this came out of the
    pty, so it may be a hostile filename, a log line or a git branch name, and the
    only answer to a scheme we do not know is to refuse the whole link.
    """
    if not uri:
        return None
    uri = uri.strip()
    if not uri or len(uri) > MAX_LINK_URI_LENGTH:
        return None
    if UNSAFE_IN_URI.search(uri):
        return None
    scheme, sep, _ = uri.partition(":")
    if not sep:
        # a relative reference names nothing we could ever open on our own
        return None
    # the scheme is what stands in front of the first colon and nothing else. a "%"
    # in there could be decoded into a different scheme further on, e.g.
    # "%6aavascript:", and a "/", "?" or "#" means the colon belongs to the path
    # and the reference has no scheme at all
    if any(c in scheme for c in ("%", "/", "?", "#")):
        return None
    if scheme.lower() not in LINK_SCHEMES:
        return None
    return uri


def line_links(buffer_line):
    """
    the OSC 8 hyperlink spans a buffer line carries, this is the one place the
    attribute is read. the result is a tuple of HyperlinkSpan ordered by column and
    must be treated as immutable, a line in the scrollback may be sharing it with
    the line it was copied from
    """
    return getattr(buffer_line, "hyperlinks", EMPTY_LINKS)


def link_at(buffer_line, column):
    """
    the Hyperlink covering a column of a buffer line, or None
    """
    for span in line_links(buffer_line):
        if span.start <= column < span.end:
            return span.link
    return None


class Hyperlink(object):
    """
    the target of an OSC 8 hyperlink. one instance is shared by every span pointing
    at it, so a build log full of links keeps the url once and a reference per run
    of text. link_id is the id= of the sequence, it is "" when the shell did not
    send one, and two spans of the same non empty id are one logical link even when
    a wrap or an unrelated line sits between them
    """
    __slots__ = ("uri", "link_id")

    def __init__(self, uri, link_id=""):
        self.uri = uri
        self.link_id = link_id

    def __repr__(self):
        return "Hyperlink({}{})".format(
            self.uri, "" if not self.link_id else ", id=" + self.link_id)


class HyperlinkSpan(object):
    """
    a run of cells of one buffer line which is inside a hyperlink. start is
    inclusive, end is exclusive and both are buffer columns, the same columns
    segment_buffer_line reports. spans belong to terminal content and not to a view
    row, render.py turns them into a row when it writes that line out.

    a span is never modified once it is built, a line copied into the scrollback
    shares its spans with the line it came from
    """
    __slots__ = ("start", "end", "link")

    def __init__(self, start, end, link):
        self.start = start
        self.end = end
        self.link = link

    @property
    def uri(self):
        return self.link.uri

    @property
    def link_id(self):
        return self.link.link_id

    def __repr__(self):
        return "HyperlinkSpan({}, {}, {})".format(self.start, self.end, self.link)


FILE_PARAM_PATTERN = re.compile(
    r"^File=(?P<arguments>[^:]*?):(?P<data>[a-zA-Z0-9\+/=]*)(?P<cr>\r?)$"
)


class Char(namedtuple("Char", [
    "data",
    "fg",
    "bg",
    "bold",
    "italics",
    "underscore",
    "strikethrough",
    "reverse",
    "linefeed"
])):

    __slots__ = ()

    def __new__(cls, data, fg="default", bg="default", bold=False,
                italics=False, underscore=False,
                strikethrough=False, reverse=False, linefeed=False):
        return super(Char, cls).__new__(cls, data, fg, bg, bold, italics,
                                        underscore, strikethrough, reverse, linefeed)


class Cursor(object):
    __slots__ = ("x", "y", "attrs", "hidden")

    def __init__(self, x, y, attrs=Char(" ")):
        self.x = x
        self.y = y
        self.attrs = attrs
        self.hidden = False


if is_windows:

    class TerminalPtyProcess(PtyProcess):

        pass

else:

    class TerminalPtyProcess(PtyProcess):

        def read(self, size):
            b = super().read(size)
            return b.decode("utf-8", "ignore")

        def write(self, s):
            b = s.encode("utf-8", "backslashreplace")
            return super().write(b)


class TerminalScreen(pyte.Screen):

    @property
    def default_char(self):
        reverse = mo.DECSCNM in self.mode
        return Char(data=" ", fg="default", bg="default", reverse=reverse)

    def __init__(self, *args, **kwargs):
        if "process" in kwargs:
            self._process = kwargs["process"]
            del kwargs["process"]
        else:
            raise Exception("missing process")

        if "clear_callback" in kwargs:
            self._clear_callback = kwargs["clear_callback"]
            del kwargs["clear_callback"]
        else:
            raise Exception("missing clear_callback")

        if "reset_callback" in kwargs:
            self._reset_callback = kwargs["reset_callback"]
            del kwargs["reset_callback"]
        else:
            raise Exception("missing reset_callback")

        if "history" in kwargs:
            history = kwargs["history"]
            del kwargs["history"]
        else:
            history = 100

        self.primary_buffer = {}
        self.history = deque(maxlen=history)
        self._alternate_buffer_mode = False
        # the working directory the shell last reported through OSC 7, it is the
        # shell's own view of it and may name a path which does not exist on this
        # side of the pty, a wsl shell reports wsl paths
        self.cwd = None
        # the OSC 8 hyperlink which is open, every cell drawn from now on is inside
        # it, and the number of cells it covered so far. these have to exist before
        # super().__init__() because it calls reset()
        self._link = None
        self._link_cells = 0
        # whether a link was ever put on a line of this screen. draw() consults it
        # to keep the terminal of somebody whose shell emits no OSC 8 at all exactly
        # as fast as it was, and it is only ever cleared by reset() because leaving
        # it set costs a lookup and clearing it too eagerly would lose a span
        self._link_seen = False
        # the targets seen so far, so that equal ones share one Hyperlink instead of
        # one copy of the url per run of text. sys.intern is deliberately not used,
        # interned strings are never freed and the urls come from the pty
        self._link_cache = {}
        super().__init__(*args, **kwargs)

    # @property
    # def display(self):
    #     pass

    def reset(self):
        if self._alternate_buffer_mode:
            # a RIS received from inside the alternate buffer has to drop the
            # alternate buffer state, otherwise push_lines_into_history short
            # circuits for the rest of the session and a later ESC[?1049l would
            # restore the stale primary buffer over the live screen
            self._alternate_buffer_mode = False
            if "history" in self.primary_buffer:
                # the alternate buffer's history has maxlen 0 and would silently
                # swallow every line pushed into it
                self.history = self.primary_buffer["history"]
        # the buffer which is live now becomes the primary one, super().reset()
        # clears it below
        self.primary_buffer = {}
        super().reset()
        self.cursor = Cursor(0, 0)
        self.history.clear()
        # nothing of the old session is on the screen any more, an open link would
        # otherwise keep swallowing whatever the fresh one draws
        self.close_hyperlink()
        self._link_cache = {}
        self._link_seen = False
        self._reset_callback()

    def clamp_cursor(self):
        # the buffer is a defaultdict so an out of range cursor would materialize
        # a bogus line and hand it to the renderer
        self.cursor.y = min(max(self.cursor.y, 0), self.lines - 1)
        # x is clamped to columns and not to columns - 1, because x == columns is the
        # pending wrap sentinel draw() parks the cursor on when the right margin is
        # full; pulling it back onto a live cell would overwrite the last character
        self.cursor.x = min(max(self.cursor.x, 0), self.columns)

    def resize(self, lines=None, columns=None):
        lines = lines or self.lines
        columns = columns or self.columns

        if lines == self.lines and columns == self.columns:
            return  # No changes.

        self.dirty.update(range(lines))

        line_diff = self.lines - lines
        if line_diff > 0:
            bottom = self.first_non_empty_line_from_bottom()
            num_empty_lines = self.lines - 1 - bottom
            if line_diff > num_empty_lines:
                line_diff = line_diff - num_empty_lines
                self.push_lines_into_history(line_diff)
                self.scroll_up(line_diff)
                self.cursor.y -= line_diff

        if columns < self.columns:
            # drop the cells beyond the new width, render.py derives the length
            # of a line from the largest key of the buffer line
            for line in self.buffer.values():
                for x in range(columns, self.columns):
                    line.pop(x, None)
                # and a link span may not name a column which no longer exists
                self.clip_links(line, columns)

        self.lines, self.columns = lines, columns

        # the arithmetic above may leave the cursor outside of the new geometry
        self.clamp_cursor()

        self.set_margins()
        self.tabstops = set(range(8, self.columns, 8))

    def set_margins(self, top=None, bottom=None):
        if (top is None or top == 0) and bottom is None:
            # https://github.com/selectel/pyte/commit/676610b43954b644c05823371df6daf87caafdad
            self.margins = None
        else:
            super().set_margins(top, bottom)

    def set_mode(self, *modes, **kwargs):
        super().set_mode(*modes, **kwargs)
        if 1049 << 5 in self.mode and not self.alternate_buffer_mode:
            self.alternate_buffer_mode = True
            self.switch_to_screen(alt=True)

    def reset_mode(self, *modes, **kwargs):
        super().reset_mode(*modes, **kwargs)
        if 1049 << 5 not in self.mode and self.alternate_buffer_mode:
            self.alternate_buffer_mode = False
            self.switch_to_screen(alt=False)

    # def define_charset(self, code, mode):
    #     pass

    # def shift_in(self):
    #     pass

    # def shift_out(self):
    #     pass

    def draw(self, data):
        """
        Terminus alters the logic to better support double width chars and linefeed marker
        """
        data = data.translate(
            self.g1_charset if self.charset else self.g0_charset)

        for char in data:
            char_width = wcwidth(char)
            if (self.cursor.x == self.columns and char_width >= 1)  \
                    or (self.cursor.x == self.columns - 1 and char_width >= 2):
                if mo.DECAWM in self.mode:
                    last = self.buffer[self.cursor.y][self.columns - 1]
                    self.buffer[self.cursor.y][self.columns - 1] = \
                        last._replace(linefeed=True)
                    self.dirty.add(self.cursor.y)
                    self.carriage_return()
                    self.linefeed()
                elif char_width > 0:
                    self.cursor.x -= char_width

            if mo.IRM in self.mode and char_width > 0:
                self.insert_characters(char_width)

            line = self.buffer[self.cursor.y]
            if char_width == 1:
                if is_windows and self.cursor.x == self.columns - 1:
                    # always put a linefeed marker when cursor is at the last column
                    line[self.cursor.x] = self.cursor.attrs._replace(data=char, linefeed=True)
                else:
                    line[self.cursor.x] = self.cursor.attrs._replace(data=char)

            elif char_width == 2:
                line[self.cursor.x] = self.cursor.attrs._replace(data=char)
                if is_windows and self.cursor.x == self.columns - 2:
                    # always put a linefeed marker when the next char is at the last column
                    line[self.cursor.x + 1] = self.cursor.attrs._replace(data="", linefeed=True)
                elif self.cursor.x + 1 < self.columns:
                    line[self.cursor.x + 1] = self.cursor.attrs._replace(data="")

            elif char_width == 0 and unicodedata.combining(char):
                # unfornately, sublime text doesn't render decomposed double char correctly
                pos = None
                for (row, col) in [
                        (self.cursor.y, self.cursor.x),
                        (self.cursor.y - 1, self.columns)]:
                    if row < 0:
                        continue
                    # the base char sits on the previous row once the cursor
                    # wrapped, it has to be read from the very row it is written
                    # back to, otherwise it is replaced by the char of the
                    # current row along with that char's colors
                    base_line = self.buffer[row]
                    if col >= 2:
                        last = base_line[col - 2]
                        if wcswidth(last.data) >= 2:
                            pos = (row, col - 2)
                            break
                    if col >= 1:
                        last = base_line[col - 1]
                        pos = (row, col - 1)
                        break

                if pos:
                    normalized = unicodedata.normalize("NFC", last.data + char)
                    self.buffer[pos[0]][pos[1]] = last._replace(data=normalized)
                    self.dirty.add(pos[0])
            else:
                # a char of unknown width, e.g. a variation selector, a zero
                # width joiner or a zero width space. feed() passes whole runs
                # of plain text, so the rest of the run must still be drawn.
                # the char occupies no cell, hence the cursor does not advance
                continue

            if char_width > 0:
                if self._link is not None or (self._link_seen and line_links(line)):
                    # the cells which were just written join the link which is open,
                    # or leave the one they used to be part of. this guard sits in
                    # the hottest loop of the terminal, so a session which never saw
                    # a hyperlink at all pays two attribute reads for it
                    self.link_cells(
                        self.cursor.y,
                        self.cursor.x,
                        min(self.cursor.x + char_width, self.columns),
                        self._link)
                self.cursor.x = min(self.cursor.x + char_width, self.columns)

        self.dirty.add(self.cursor.y)

    # def set_title(self, param):
    #     pass

    # def set_icon_name(self, param):
    #     pass

    # def carriage_return(self):
    #     pass

    def index(self):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        # a line only belongs in the scrollback when it scrolls off the top of the
        # screen, that is when the scrolling region starts at the first line. a
        # pager which pinned a status line above the region discards its lines
        # instead of archiving them, otherwise the status line would be copied
        # into the scrollback on every scroll
        if not self.alternate_buffer_mode and self.cursor.y == bottom and top == 0:
            self.push_lines_into_history(1, top)
        super().index()

    # def reverse_index(self):
    #     pass

    # def linefeed(self):
    #     pass

    # def tab(self):
    #     pass

    # def backspace(self):
    #    pass

    # def save_cursor(self):
    #     pass

    # def restore_cursor(self):
    #     pass

    # def insert_lines(self, count=None):
    #     pass

    # def delete_lines(self, count=None):
    #     pass

    def insert_characters(self, count=None):
        # the cells at the cursor and to the right of it move that far right, and the
        # spans have to travel with the very cells they name. dropping them instead
        # would be safe but wrong in the common case: draw() routes every single
        # character through here while IRM is set, so a link typed in insert mode
        # would collapse to its last cell
        self.shift_links(self.cursor.y, self.cursor.x, count or 1)
        super().insert_characters(count)

    def delete_characters(self, count=None):
        self.shift_links(self.cursor.y, self.cursor.x, -(count or 1))
        super().delete_characters(count)

    def shift_links(self, y, at, count):
        """
        move the spans of a line sideways with the cells they name. count > 0 is an
        insert at column `at`, which pushes everything from there rightwards off the
        end of the line, count < 0 is a delete, which pulls it leftwards. the cells
        the insert opens up are blank and belong to no link, and the cells a delete
        takes away take their part of a span with them
        """
        line = self.buffer.get(y)
        if line is None:
            return
        spans = line_links(line)
        if not spans:
            return

        kept = []
        for span in spans:
            # whatever sat left of the cursor did not move
            if span.start < at:
                kept.append(HyperlinkSpan(span.start, min(span.end, at), span.link))
            if count > 0:
                start = max(span.start, at) + count
                end = min(span.end + count, self.columns)
            else:
                # the cells between at and at - count are gone with the delete
                start = max(span.start, at - count) + count
                end = span.end + count
            if start < end:
                kept.append(HyperlinkSpan(start, end, span.link))

        merged = []
        for span in sorted(kept, key=lambda s: s.start):
            # a delete closes the gap between the two halves of what used to be one
            # span, and they are one span again
            if merged and merged[-1].end == span.start and merged[-1].link is span.link:
                merged[-1] = HyperlinkSpan(merged[-1].start, span.end, span.link)
            else:
                merged.append(span)

        self.store_links(line, merged)

    def erase_characters(self, count=None):
        count = count or 1
        # the erased cells are blanked in place, they are no longer inside whatever
        # link used to cover them
        self.link_cells(
            self.cursor.y,
            self.cursor.x,
            min(self.cursor.x + count, self.columns),
            None)
        super().erase_characters(count)

    def erase_in_line(self, how=0, private=False):
        if how == 0:
            self.link_cells(self.cursor.y, self.cursor.x, self.columns, None)
            super().erase_in_line(how, private)
        elif how == 1:
            self.link_cells(self.cursor.y, 0, self.cursor.x + 1, None)
            super().erase_in_line(how, private)
        elif how == 2:
            self.drop_links(self.cursor.y)
            super().erase_in_line(how, private)
        else:
            # EL is defined for 0, 1 and 2 and xterm ignores every other parameter.
            # pyte instead leaves its interval unbound and raises out of the middle
            # of feed(), which used to take the rest of that chunk of output with it
            # and, since the spans were dropped first, left this row's text on screen
            # without its links. erasing on an undefined parameter would invent the
            # data loss, so nothing happens here, exactly as on a real terminal
            logger.debug("ignoring erase in line with the parameter {}".format(how))

    def erase_in_display(self, how=0, *args, **kwargs):
        # dump the screen to history
        # check also https://github.com/selectel/pyte/pull/108

        if not self.alternate_buffer_mode and \
                (how == 2 or (how == 0 and self.cursor.x == 0 and self.cursor.y == 0)):
            self.push_lines_into_history()

        if how == 0:
            interval = range(self.cursor.y + 1, self.lines)
        elif how == 1:
            interval = range(self.cursor.y)
        elif how == 2 or how == 3:
            interval = range(self.lines)

        self.dirty.update(interval)
        for y in interval:
            line = self.buffer[y]
            # the cells are blanked in place, so the line object survives and would
            # keep carrying the marks and the links of the text which was just erased
            self.drop_marks(y)
            self.drop_links(y)
            for i, x in list(enumerate(line)):
                if i < self.columns:
                    line[x] = self.cursor.attrs
                else:
                    line.pop(x, None)

        if how == 0 or how == 1:
            # the marks of the cursor line are deliberately kept, even when all of
            # it is blanked. a prompt redrawn in place is CR, some sgr, then an
            # erase, and the shell does not report its boundaries a second time,
            # zle and fish repaint that way on every keystroke
            self.erase_in_line(how)

        if how == 3:
            self.history.clear()
            # the whole terminal is being emptied, an open link must not carry over
            # into the text which is written after it
            self.close_hyperlink()
            self._clear_callback()

    # def set_tab_stop(self):
    #     pass

    # def clear_tab_stop(self, how=0):
    #     pass

    # def ensure_hbounds(self):
    #     pass

    # def ensure_vbounds(self, use_margins=None):
    #     pass

    # def cursor_up(self, count=None):
    #     pass

    # def cursor_up1(self, count=None):
    #     pass

    # def cursor_down(self, count=None):
    #     pass

    # def cursor_down1(self, count=None):
    #     pass

    # def cursor_back(self, count=None):
    #     pass

    # def cursor_forward(self, count=None):
    #     pass

    # def cursor_position(self, line=None, column=None):
    #     pass

    # def cursor_to_column(self, column=None):
    #     pass

    # def cursor_to_line(self, line=None):
    #     pass

    # def bell(self, *args):
    #     pass

    # def alignment_display(self):
    #     pass

    def select_graphic_rendition(self, *attrs, private=False):
        """Set display attributes.

        :param list attrs: a list of display attributes to set.
        """
        replace = {}

        # Fast path for resetting all attributes.
        if not attrs or attrs == (0, ):
            self.cursor.attrs = self.default_char
            return
        else:
            attrs = list(reversed(attrs))

        while attrs:
            attr = attrs.pop()
            if attr == 0:
                # Reset all attributes.
                replace.update(self.default_char._asdict())
            elif attr in g.FG_ANSI:
                replace["fg"] = g.FG_ANSI[attr]
            elif attr in g.BG:
                replace["bg"] = g.BG_ANSI[attr]
            elif attr in g.TEXT:
                attr = g.TEXT[attr]
                replace[attr[1:]] = attr.startswith("+")
            elif attr in g.FG_AIXTERM:
                replace.update(fg=FG_AIXTERM[attr])
            elif attr in g.BG_AIXTERM:
                replace.update(bg=BG_AIXTERM[attr])
            elif attr in (g.FG_256, g.BG_256):
                key = "fg" if attr == g.FG_256 else "bg"
                try:
                    n = attrs.pop()
                    if n == 5:    # 256.
                        m = attrs.pop()
                        if m < 16:
                            replace[key] = ANSI_COLORS[m]
                        else:
                            replace[key] = g.FG_BG_256[m]
                    elif n == 2:  # 24bit.
                        # This is somewhat non-standard but is nonetheless
                        # supported in quite a few terminals. See discussion
                        # here https://gist.github.com/XVilka/8346728.
                        replace[key] = "{0:02x}{1:02x}{2:02x}".format(
                            attrs.pop(), attrs.pop(), attrs.pop())
                except IndexError:
                    pass

        self.cursor.attrs = self.cursor.attrs._replace(**replace)

    # def report_device_attributes(self, mode=0, **kwargs):
    #     pass

    # def report_device_status(self, mode):
    #     pass

    def write_process_input(self, data):
        self._process.write(data)

    # def debug(self, *args, **kwargs):
    #     pass

    def scroll_up(self, n):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        for y in range(top, bottom + 1):
            if y + n > bottom:
                # clear() empties the cells but keeps the instance attributes, the
                # marks and the links of the line which just scrolled away have to
                # go by hand
                self.drop_marks(y)
                self.drop_links(y)
                self.buffer[y].clear()
            else:
                self.buffer[y] = copy(self.buffer[y + n])
        self.dirty.update(range(self.lines))

    def scroll_down(self, n):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        for y in reversed(range(top, bottom + 1)):
            if y - n < top:
                self.drop_marks(y)
                self.drop_links(y)
                self.buffer[y].clear()
            else:
                self.buffer[y] = copy(self.buffer[y - n])
        self.dirty.update(range(self.lines))

    def attach_mark(self, kind, exit_code=None):
        """
        put an OSC 133 mark on the line the cursor is on, a second mark of the same
        kind replaces the first one, which is what a prompt redrawn in place does
        """
        line = self.buffer[self.cursor.y]
        mark = SemanticMark(kind, exit_code)
        marks = list(line_marks(line))
        for i, m in enumerate(marks):
            if m.kind == kind:
                marks[i] = mark
                break
        else:
            marks.append(mark)
        # the tuple is rebound and never mutated in place, a copy of this line in the
        # scrollback shares the very same tuple object with it
        line.semantic_marks = tuple(marks)
        # the row has to be written out again for the mark to reach the view
        self.dirty.add(self.cursor.y)

    def drop_marks(self, y):
        """
        forget the marks of a line which is about to be blanked in place
        """
        # the buffer is a defaultdict, get() keeps a row which does not exist yet
        # from being materialized and handed to the renderer
        line = self.buffer.get(y)
        if line is not None and hasattr(line, "semantic_marks"):
            del line.semantic_marks

    def set_semantic_mark(self, param):
        # OSC 133 reports the prompt and command boundaries of the shell, e.g.
        # ESC]133;A ESC\ in front of the prompt and ESC]133;D;0 ESC\ once the
        # command finished, see SHELL_INTEGRATION.md
        if self.alternate_buffer_mode:
            # a full screen application draws over the prompt, nothing it emits
            # describes the boundaries of the shell
            return
        fields = param.split(";")
        kind = fields[0].strip()
        if kind in (MARK_PROMPT, MARK_INPUT, MARK_OUTPUT):
            self.attach_mark(kind)
        elif kind == MARK_END:
            # a D with no status, an empty one or one which is not a plain decimal
            # number still marks a command which finished, only its status is
            # unknown. int() alone would take "1_0", "+9", non ascii digits and an
            # integer of any size from anything which can write to the pty, and a
            # consumer would then format or compare that, so the digits are checked
            # first. five of them is far more than a status can be worth
            status = fields[1].strip() if len(fields) > 1 else ""
            digits = status[1:] if status[:1] == "-" else status
            if digits.isdigit() and digits.isascii() and len(digits) <= 5:
                exit_code = int(status)
            else:
                exit_code = None
            self.attach_mark(MARK_END, exit_code)
        else:
            # P carries semantic properties and anything else is a subcommand of a
            # newer shell, both are consumed and dropped
            logger.debug("ignoring osc 133 subcommand: {}".format(param))

    def drop_links(self, y):
        """
        forget the hyperlink spans of a line which is about to be blanked in place
        or whose cells are about to be shifted around
        """
        # the buffer is a defaultdict, get() keeps a row which does not exist yet
        # from being materialized and handed to the renderer
        line = self.buffer.get(y)
        if line is not None and hasattr(line, "hyperlinks"):
            del line.hyperlinks

    def clip_links(self, line, columns):
        """
        drop what a line's spans say about columns which no longer exist, the screen
        just became narrower
        """
        spans = line_links(line)
        if not spans or spans[-1].end <= columns:
            return
        kept = []
        for span in spans:
            if span.start >= columns:
                continue
            if span.end > columns:
                kept.append(HyperlinkSpan(span.start, columns, span.link))
            else:
                kept.append(span)
        self.store_links(line, kept)

    def store_links(self, line, spans):
        """
        put the spans on a buffer line, the tuple is rebound and never mutated in
        place, a copy of this line in the scrollback shares the very same tuple
        object with it
        """
        if spans:
            line.hyperlinks = tuple(spans)
            self._link_seen = True
        elif hasattr(line, "hyperlinks"):
            del line.hyperlinks

    def link_cells(self, y, start, end, link):
        """
        Keep the hyperlink spans of a line in step with the cells which were just
        written to it. The cells from start to end, end excluded, are put inside
        link, or taken out of whatever span used to cover them when link is None.

        This is where the granularity differs from an OSC 133 mark: a mark belongs
        to a whole line, a link covers a run of cells which may start and end mid
        line, and a link which survives a wrap simply covers a run on the next line
        as well.
        """
        if start >= end:
            return
        line = self.buffer[y]
        spans = line_links(line)
        if link is None and not spans:
            return
        # how many cells this call covers, the merging below moves the bounds around
        drawn = end - start

        kept = []
        for span in spans:
            if span.end <= start or span.start >= end:
                kept.append(span)
                continue
            # the cells were overwritten, whatever they pointed at only survives on
            # either side of them
            if span.start < start:
                kept.append(HyperlinkSpan(span.start, start, span.link))
            if span.end > end:
                kept.append(HyperlinkSpan(end, span.end, span.link))

        if link is not None:
            for span in list(kept):
                # the run which is being drawn now and the one drawn a moment ago
                # are one span as soon as they touch, which is what keeps a link
                # from costing one span per character
                if span.link is link and (span.end == start or span.start == end):
                    start = min(start, span.start)
                    end = max(end, span.end)
                    kept.remove(span)
            kept.append(HyperlinkSpan(start, end, link))
            if len(kept) > 1:
                kept.sort(key=lambda s: s.start)

        self.store_links(line, kept)

        if link is not None and link is self._link:
            # a shell may open a link and never close it, e.g. because the command
            # printing it was killed halfway through. the link is dropped once it
            # covered MAX_LINK_CELLS cells, so the damage is bounded to a few
            # screens of text instead of the rest of the session
            self._link_cells += drawn
            if self._link_cells > MAX_LINK_CELLS:
                logger.debug("hyperlink left open, dropping it: {}".format(link.uri))
                self.close_hyperlink()

    def intern_link(self, uri, link_id):
        """
        one Hyperlink instance per target, so that a log full of the same link keeps
        one copy of the url and a reference per run of text
        """
        key = (link_id, uri)
        link = self._link_cache.get(key)
        if link is None:
            if len(self._link_cache) >= MAX_LINK_CACHE:
                # the cache exists only so that equal targets share one object,
                # forgetting it costs nothing but a little duplication further on
                self._link_cache.clear()
            link = Hyperlink(uri, link_id)
            self._link_cache[key] = link
        return link

    def close_hyperlink(self):
        """
        no cell drawn from here on is inside a hyperlink
        """
        self._link = None
        self._link_cells = 0

    def set_hyperlink(self, param):
        # OSC 8 opens a hyperlink, e.g. ESC]8;id=42;https://example.com ESC\, and
        # ESC]8;; ESC\ closes it again. everything drawn while one is open belongs
        # to it, including what is drawn after a wrap
        params, sep, uri = param.partition(";")
        if not sep:
            # not a well formed sequence, and the safe reading of it is "no link"
            self.close_hyperlink()
            return

        uri = safe_link_uri(uri)
        if not uri:
            # an empty target is the closing sequence, a refused one is a target we
            # will not store, and neither may leave the previous link open
            self.close_hyperlink()
            return

        link_id = ""
        for pair in params.split(":"):
            key, has_value, value = pair.partition("=")
            if not has_value:
                continue
            if key.strip() == "id":
                link_id = UNSAFE_IN_URI.sub("", value)
                if len(link_id) > MAX_LINK_ID_LENGTH:
                    # truncating would tie two links which merely share a long prefix
                    # together, and an id only ever joins runs of one link, so an
                    # oversized one is dropped instead. the link itself still stands
                    logger.debug("oversized osc 8 id dropped")
                    link_id = ""
            # every other key belongs to a newer terminal than this one and is
            # ignored on purpose, an unknown one is never a reason to fail

        self._link = self.intern_link(uri, link_id)
        self._link_cells = 0

    def set_cwd(self, param):
        # OSC 7 reports the working directory as a file uri every time it changes,
        # e.g. ESC]7;file://hostname/home/user/project ESC\
        cwd = uri_to_path(param)
        if cwd:
            logger.debug("cwd reported: {}".format(cwd))
            self.cwd = cwd

    def handle_iterm_protocol(self, param):
        m = FILE_PARAM_PATTERN.match(param)
        if m:
            arguments = {}
            for pair in m.group("arguments").split(";"):
                if "=" not in pair:
                    continue
                key, value = pair.split("=", 1)
                arguments[key] = value

            data = m.group("data")
            cr = m.group("cr")

            self.show_image_callback(data, arguments, cr)

    def set_show_image_callback(self, callback):
        self.show_image_callback = callback

    @property
    def alternate_buffer_mode(self):
        return self._alternate_buffer_mode

    @alternate_buffer_mode.setter
    def alternate_buffer_mode(self, value):
        self._alternate_buffer_mode = value

    def switch_to_screen(self, alt=False):
        # the buffer which is drawn to is about to be a different one, a link left
        # open by the application being entered or left must not cover it
        self.close_hyperlink()
        if alt:
            self.primary_buffer["buffer"] = self.buffer
            self.primary_buffer["history"] = self.history
            self.primary_buffer["cursor"] = self.cursor
            self.buffer = defaultdict(lambda: StaticDefaultDict(self.default_char))
            self.history = deque(maxlen=0)
            self.cursor = Cursor(0, 0)
        else:
            self.buffer = self.primary_buffer["buffer"]
            self.history = self.primary_buffer["history"]
            self.cursor = self.primary_buffer["cursor"]
            # resize() only knows about the buffer which is live, so a stashed
            # primary buffer still carries the geometry it was saved at. a cursor
            # from a taller screen would put the prompt on a row which is never
            # rendered, which looks exactly like the resize killed the terminal
            self.clamp_cursor()
            for line in self.buffer.values():
                # and cells past the right margin would render as an over long
                # line, render.py derives the length of a line from the largest
                # key of the buffer line
                for x in list(line.keys()):
                    if x >= self.columns:
                        line.pop(x, None)
                self.clip_links(line, self.columns)

        self.dirty.update(range(self.lines))

    def first_non_empty_line_from_bottom(self):
        found = -1
        for nz_line in reversed(range(self.lines)):
            text = "".join([c.data for c in self.buffer[nz_line].values()])
            if text and not text.isspace():
                found = nz_line
                break
        return found

    def push_lines_into_history(self, count=None, start=0):
        if self.alternate_buffer_mode:
            return
        if count is None:
            # find the first non-empty line from the botton
            count = self.first_non_empty_line_from_bottom() + 1
        self.history.extend(copy(self.buffer[y]) for y in range(start, start + count))


PLAIN_TEXT = "plain_text"

# an OSC sequence which is never terminated must not be able to grow without
# bound, the code and the parameter are capped and anything beyond is dropped
MAX_OSC_CODE_LENGTH = 5
MAX_OSC_PARAM_LENGTH = 4096
# OSC 1337 of the iTerm2 inline image protocol carries base64 encoded image data
MAX_OSC_IMAGE_PARAM_LENGTH = 4 * 1024 * 1024
# OSC 8 carries its params and its target, and safe_link_uri is the one place which
# decides that a target is too long. giving the sequence room for the longest one it
# could legitimately carry keeps that decision there and not here, where a discarded
# sequence would leave the previous link open, see the overflow branch below
MAX_OSC_LINK_PARAM_LENGTH = MAX_LINK_URI_LENGTH + MAX_LINK_ID_LENGTH + 64


def flatten_csi_subparams(subparams):
    """
    Map a colon separated CSI parameter of the ITU T.416 form, e.g. the
    "38:2::255:0:0" truecolor sequence neovim and delta emit, onto the legacy
    semicolon form select_graphic_rendition understands. The sub parameters are
    consumed instead of leaking their tail onto the screen as literal text.

    A parameter position always contributes at least one integer, even when it
    has no legacy equivalent. Contributing nothing would shorten the parameter
    list, and an SGR with no parameters at all is a full attribute reset in pyte,
    while a CSI handler with a required argument would raise on the short list.
    """
    attr = subparams[0]
    rest = subparams[1:]
    if attr in (g.FG_256, g.BG_256):
        if rest[0] == 5 and len(rest) >= 2:
            # 38:5:n, the indexed color
            return [attr, 5, rest[1]]
        elif rest[0] == 2 and len(rest) >= 4:
            # 38:2:r:g:b or 38:2:<color space id>:r:g:b, the color space id is
            # optional and usually empty, hence the rgb triple is taken from the end
            return [attr, 2] + rest[-3:]
    elif attr == 4:
        # the underline styles of "4:x", only the plain underline is supported,
        # 4:0 turns the underline off
        return [24] if rest[0] == 0 else [4]
    # e.g. 58:2:..., the underline color, which has no legacy equivalent. the bare
    # attribute is kept, select_graphic_rendition does not know it and leaves the
    # surrounding attributes alone, which is what dropping it should have done
    return [attr]


class TerminalStream(pyte.Stream):

    def __init__(self, *args, **kwargs):
        self.csi["S"] = "scroll_up"
        self.csi["T"] = "scroll_down"
        self.osc = {
            "0": "set_title",
            "1": "set_icon_name",
            "2": "set_title",
            "7": "set_cwd",
            "8": "set_hyperlink",
            # the dispatch table is captured by value when the parser generator is
            # built in super().__init__(), an entry added afterwards is never
            # dispatched, so every osc code belongs in this literal
            "133": "set_semantic_mark",
            "1337": "handle_iterm_protocol"
        }
        self.yield_what = None
        super().__init__(*args, **kwargs)

    def restart_parser(self):
        """
        Build a fresh parser generator, the way pyte does on attach. The generator is
        closed for good once a listener raised through it, and feeding a closed
        generator raises StopIteration, so without this a single bad sequence would
        leave the terminal blank for the rest of its life.
        """
        try:
            self._parser = self._parser_fsm()
            self._taking_plain_text = next(self._parser)
            self.yield_what = self._taking_plain_text
        except Exception as e:
            logger.error("cannot restart the parser: {}".format(e))

    def _parser_fsm(self):
        """
        Override to support "imgcat"
        """
        basic = self.basic
        listener = self.listener
        draw = listener.draw
        debug = listener.debug

        ESC, CSI_C1 = ctrl.ESC, ctrl.CSI_C1
        OSC_C1 = ctrl.OSC_C1
        SP_OR_GT = ctrl.SP + ">"
        NUL_OR_DEL = ctrl.NUL + ctrl.DEL
        CAN_OR_SUB = ctrl.CAN + ctrl.SUB
        ALLOWED_IN_CSI = "".join([ctrl.BEL, ctrl.BS, ctrl.HT, ctrl.LF,
                                  ctrl.VT, ctrl.FF, ctrl.CR])
        OSC_TERMINATORS = set([ctrl.ST_C0, ctrl.ST_C1, ctrl.BEL, ctrl.CR])

        def create_dispatcher(mapping):
            return defaultdict(lambda: debug, dict(
                (event, getattr(listener, attr))
                for event, attr in mapping.items()))

        basic_dispatch = create_dispatcher(basic)
        sharp_dispatch = create_dispatcher(self.sharp)
        escape_dispatch = create_dispatcher(self.escape)
        csi_dispatch = create_dispatcher(self.csi)
        osc_dispatch = create_dispatcher(self.osc)

        while True:
            # it is allowed to send
            # chunks of plain text directly to the listener, instead
            # of this generator.
            char = yield PLAIN_TEXT

            if char == ESC:
                # Most non-VT52 commands start with a left-bracket after the
                # escape and then a stream of parameters and a command; with
                # a single notable exception -- :data:`escape.DECOM` sequence,
                # which starts with a sharp.
                #
                # .. versionchanged:: 0.4.10
                #
                #    For compatibility with Linux terminal stream also
                #    recognizes ``ESC % C`` sequences for selecting control
                #    character set. However, in the current version these
                #    are noop.
                char = yield
                if char == "[":
                    char = CSI_C1  # Go to CSI.
                elif char == "]":
                    char = OSC_C1  # Go to OSC.
                else:
                    if char == "#":
                        sharp_dispatch[(yield)]()
                    if char == "%":
                        self.select_other_charset((yield))
                    elif char in "()":
                        code = yield
                        if self.use_utf8:
                            continue

                        # See http://www.cl.cam.ac.uk/~mgk25/unicode.html#term
                        # for the why on the UTF-8 restriction.
                        listener.define_charset(code, mode=char)
                    else:
                        escape_dispatch[char]()
                    continue    # Don't go to CSI.

            if char in basic:
                # Ignore shifts in UTF-8 mode. See
                # http://www.cl.cam.ac.uk/~mgk25/unicode.html#term for
                # the why on UTF-8 restriction.
                if (char == ctrl.SI or char == ctrl.SO) and self.use_utf8:
                    continue

                basic_dispatch[char]()

            elif char == CSI_C1:
                # All parameters are unsigned, positive decimal integers, with
                # the most significant digit sent first. Any parameter greater
                # than 9999 is set to 9999. If you do not specify a value, a 0
                # value is assumed.
                #
                # .. seealso::
                #
                #    `VT102 User Guide <http://vt100.net/docs/vt102-ug/>`_
                #        For details on the formatting of escape arguments.
                #
                #    `VT220 Programmer Ref. <http://vt100.net/docs/vt220-rm/>`_
                #        For details on the characters valid for use as
                #        arguments.
                params = []
                current = ""
                private = False
                # a colon separates the sub parameters of a single parameter,
                # they are collected apart from the plain parameters and folded
                # into them once the parameter is complete
                subparams = None
                while True:
                    char = yield

                    if char == "?":
                        private = True
                    elif char in ALLOWED_IN_CSI:
                        basic_dispatch[char]()
                    elif char in SP_OR_GT:
                        pass  # Secondary DA is not supported atm.
                    elif char in CAN_OR_SUB:
                        # If CAN or SUB is received during a sequence, the
                        # current sequence is aborted; terminal displays
                        # the substitute character, followed by characters
                        # in the sequence received after CAN or SUB.
                        draw(char)
                        break
                    elif char.isdigit():
                        current += char
                    elif char == ":":
                        if subparams is None:
                            subparams = []
                        subparams.append(min(int(current or 0), 9999))
                        current = ""
                    else:
                        if subparams is None:
                            params.append(min(int(current or 0), 9999))
                        else:
                            subparams.append(min(int(current or 0), 9999))
                            params.extend(flatten_csi_subparams(subparams))
                            subparams = None

                        if char == ";":
                            current = ""
                        else:
                            if private:
                                csi_dispatch[char](*params, private=True)
                            else:
                                csi_dispatch[char](*params)
                            break  # CSI is finished.

            elif char == OSC_C1:
                code = yield None
                if code == "R":
                    continue  # Reset palette. Not implemented.
                elif code == "P":
                    continue  # Set palette. Not implemented.

                # the code may span several characters, e.g. 1337 of the iTerm2
                # inline image protocol, it ends at the first ";"
                param = ""
                in_code = True
                overflow = False
                limit = MAX_OSC_PARAM_LENGTH
                while True:
                    char = yield None
                    if char == ESC:
                        char += yield None
                    if char in OSC_TERMINATORS:
                        break
                    elif in_code:
                        if char == ";":
                            in_code = False
                            if code == "1337":
                                limit = MAX_OSC_IMAGE_PARAM_LENGTH
                            elif code == "8":
                                limit = MAX_OSC_LINK_PARAM_LENGTH
                        elif len(code) < MAX_OSC_CODE_LENGTH:
                            code += char
                        else:
                            # no code we could ever dispatch, keep consuming the
                            # sequence but throw it away
                            in_code = False
                            overflow = True
                            limit = 0
                    elif len(param) < limit:
                        param += char
                    else:
                        overflow = True

                if overflow:
                    logger.debug("oversized osc sequence discarded: {}".format(code))
                    if code == "8":
                        # every other malformed OSC 8 closes the link which is open,
                        # and this one must too. dropping it silently would leave the
                        # link a previous sequence opened attached to everything the
                        # oversized one was trying to retarget, i.e. the text of a
                        # benign link would keep pointing at whatever was open before
                        osc_dispatch["8"]("")
                    continue

                if code in osc_dispatch:
                    osc_dispatch[code](param)
                else:
                    # dropping unknown osc codes is the correct failure mode
                    debug(param)

            elif char not in NUL_OR_DEL:
                draw(char)

    def feed(self, data):
        send = self._parser.send
        draw = self.listener.draw
        match_text = self._text_pattern.match
        yield_what = self.yield_what

        length = len(data)
        offset = 0
        while offset < length:
            if yield_what == PLAIN_TEXT:
                match = match_text(data, offset)
                if match:
                    start, offset = match.span()
                    draw(data[start:offset])
                else:
                    yield_what = None
            else:
                try:
                    yield_what = send(data[offset:offset + 1])
                except Exception:
                    # a listener which raises, e.g. a pyte handler reached with the
                    # parameter list a malformed sequence produced, closes the parser
                    # generator. rebuild it before the error propagates, otherwise every
                    # later feed raises StopIteration and only this chunk should be lost
                    self.restart_parser()
                    raise
                offset += 1

        self.yield_what = yield_what
