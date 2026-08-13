import time
from wcwidth import wcwidth
from functools import partial, update_wrapper
from contextlib import contextmanager
from weakref import WeakKeyDictionary
import shlex


def shlex_split(shell_cmd):
    # it is a version of shlex.split which supports trimming quotes and windows path
    args = shlex.split(shell_cmd, posix=False)
    for i, a in enumerate(args):
        if a.startswith('"') and a.endswith('"'):
            args[i] = a[1:-1]
        elif a.startswith("'") and a.endswith("'"):
            args[i] = a[1:-1]
    return args


def available_panel_name(window, panel_name):
    if not window.find_output_panel(panel_name):
        return panel_name

    count = 2
    while True:
        new_panel_name = "{} {:d}".format(panel_name, count)
        if not window.find_output_panel(new_panel_name):
            return new_panel_name
        else:
            count += 1


# where the region key counter starts over. the colors of a row and the underlines of
# its hyperlinks both come out of it, so a busy terminal climbs it twice as fast as it
# used to and the wrap is worth getting right
MAX_HIGHLIGHT_KEY = 100000000


def get_highlight_key(view):
    """
    make region keys incremental and recyclable
    """
    value = view.settings().get("terminus.highlight_counter", 0)

    if value >= MAX_HIGHLIGHT_KEY:
        # starting over at terminus#1 hands out a key a row on screen is still
        # holding: add_regions would replace that row's regions with these, and that
        # row's erase_regions would then take these away again. the walk below only
        # works downwards from a counter which is above everything in use, so on the
        # wrap the first key nobody holds has to be found from the bottom
        value = 0
        while value < MAX_HIGHLIGHT_KEY:
            regions = view.get_regions("terminus#{}".format(value + 1))
            if not regions or regions[0].empty():
                break
            value += 1
        value += 1
        view.settings().set("terminus.highlight_counter", value)
        return "terminus#{}".format(value)

    while value >= 1:
        regions = view.get_regions("terminus#{}".format(value))
        if regions and not regions[0].empty():
            break
        value -= 1
    value += 1
    view.settings().set("terminus.highlight_counter", value)
    return "terminus#{}".format(value)


class Responsive:
    """
    the object `responsive` wraps a function in, see `responsive`
    """

    def __init__(self, f, period, default):
        self.f = f
        self.period = period
        self.default = default
        # time of the last real call, a plain function keeps it here
        self._t = 0
        # a method keeps one per instance, a single cell shared by all instances would
        # let whichever instance calls first eat the token and every other instance
        # would get `default` instead of a real answer. the dictionary is weak so that
        # a decorated method never keeps its instance alive
        self._instance_t = WeakKeyDictionary()
        update_wrapper(self, f)

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        # bind, the throttle state is looked up per instance in `_call`
        return partial(self._call, instance)

    def __call__(self, *args, **kwargs):
        # a plain function, e.g. one decorated inside another function, lands here and
        # gets the single shared cell, which is what it wants. calling a decorated
        # method unbound, Terminal.is_hosted(terminal), also lands here and shares that
        # cell with every other such call, use terminal.is_hosted() instead
        return self._call(None, *args, **kwargs)

    def _call(self, instance, *args, **kwargs):
        now = time.time()
        if instance is None:
            if now - self._t <= self.period:
                return self.default
            self._t = now
            return self.f(*args, **kwargs)
        else:
            if now - self._instance_t.get(instance, 0) <= self.period:
                return self.default
            self._instance_t[instance] = now
            return self.f(instance, *args, **kwargs)


def responsive(period=0.1, default=True):
    """
    make a function more responsive
    """
    def wrapper(f):
        return Responsive(f, period, default)

    return wrapper


@contextmanager
def intermission(period=0.1):
    """
    intermission of period seconds.
    """
    startt = time.time()
    yield
    deltat = time.time() - startt
    if deltat < period:
        time.sleep(period - deltat)


def rev_wcwidth(text, width):
    """
    Given a text, return the location such that the substring has width `width`.
    """
    if width == 0:
        return -1

    w = 0
    i = -1
    # loop over to check for double width chars
    for i, c in enumerate(text):
        w += wcwidth(c)
        if w >= width:
            break
    if w >= width:
        return i
    else:
        return i + width - w


def set_settings_on_change(settings, keys, on_change=None):
    if not isinstance(keys, list):
        singleton = True
        keys = [keys]
    else:
        singleton = False

    _key = "terminus_{}".format(".".join(keys))

    if on_change is None:
        settings.clear_on_change(_key)
        return

    _cached = {}
    for key in keys:
        _cached[key] = settings.get(key, None)

    def check_cache_values(on_change):
        run_on_change = False

        for key in keys:
            value = settings.get(key)
            if _cached[key] != value:
                _cached[key] = value
                run_on_change = True

        if run_on_change:
            if singleton and len(_cached) == 1:
                on_change(list(_cached.values())[0])
            else:
                on_change(_cached)

    settings.add_on_change(_key, lambda: check_cache_values(on_change))
