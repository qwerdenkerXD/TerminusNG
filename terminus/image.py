import logging
import struct
from .vendor import imghdr

logger = logging.getLogger('Terminus')

# the largest size in pixels we accept from a width or height argument, a guard
# against absurd values printed by the shell
MAX_LENGTH = 1 << 16


# see https://bugs.python.org/issue16512#msg198034
# not added to imghdr.tests because of potential issues with reloads
def _is_jpg(h):
    return h.startswith(b'\xff\xd8')


def get_image_info(databytes):
    head = databytes[0:32]
    if len(head) != 32:
        return
    what = imghdr.what(None, head)
    if what == 'png':
        check = struct.unpack('>i', head[4:8])[0]
        if check != 0x0d0a1a0a:
            return
        width, height = struct.unpack('>ii', head[16:24])
    elif what == 'gif':
        width, height = struct.unpack('<HH', head[6:10])
    elif what == 'jpeg' or _is_jpg(head):
        datalen = len(databytes)
        pos = 0
        size = 2
        ftype = 0
        while not 0xc0 <= ftype <= 0xcf or ftype in (0xc4, 0xc8, 0xcc):
            if size < 0:
                # a corrupt segment length would walk us backwards
                logger.debug("malformed jpeg segment length")
                return
            pos += size
            byte = databytes[pos:pos + 1]
            if not byte:
                logger.debug("truncated jpeg header at {}/{}".format(pos, datalen))
                return
            while ord(byte) == 0xff:
                byte = databytes[pos:pos + 1]
                pos += 1
                if not byte:
                    logger.debug("truncated jpeg header at {}/{}".format(pos, datalen))
                    return
            ftype = ord(byte)
            chunk = databytes[pos:pos + 2]
            if len(chunk) != 2:
                logger.debug("truncated jpeg header at {}/{}".format(pos, datalen))
                return
            size = struct.unpack('>H', chunk)[0] - 2
            pos += 2
        # We are at a SOFn block
        pos += 1  # Skip `precision' byte.
        chunk = databytes[pos:pos + 4]
        if len(chunk) != 4:
            logger.debug("truncated jpeg header at {}/{}".format(pos, datalen))
            return
        height, width = struct.unpack('>HH', chunk)

    elif what == "bmp":
        if head[0:2].decode() != "BM":
            return
        width, height = struct.unpack('II', head[18:26])
    else:
        return

    if width <= 0 or height <= 0:
        logger.debug("bogus image dimensions {}x{}".format(width, height))
        return

    return what, width, height


def _parse_length(value, em_width, max_length):
    # parse an iTerm2 width or height argument into pixels, `None` is returned
    # for "auto" and for anything we cannot make sense of, meaning the natural
    # size of the image should be used instead
    if value is None:
        return None
    value = str(value).strip().lower()
    if not value or value == "auto":
        return None
    try:
        if value[-1] == "%":
            # percentages may be fractional, e.g. "50.5%"
            length = int(max_length * float(value[:-1]) / 100)
        elif value[-2:] == "px":
            length = int(float(value[:-2]))
        else:
            # a bare number is a number of character cells
            length = int(float(value) * em_width)
    except (ValueError, OverflowError):
        logger.debug("cannot parse image dimension: {}".format(value))
        return None
    if length <= 0:
        return None
    return min(length, MAX_LENGTH)


def _preserve_ratio(value):
    # the OSC parser hands us strings, but iTerm2 documents preserveAspectRatio
    # as 0 or 1, so accept both spellings and default to preserving the ratio
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    value = str(value).strip().lower()
    if value in ("0", "false"):
        return False
    if value in ("1", "true", ""):
        return True
    logger.debug("unknown preserveAspectRatio value: {}".format(value))
    return True


def image_resize(img_width, img_height, width, height, em_width, max_width, preserve_ratio=1):
    # a corrupt header could hand us zero or negative dimensions, keep the
    # divisions below safe
    if not img_width or img_width < 1:
        logger.debug("bogus image width {}".format(img_width))
        img_width = 1
    if not img_height or img_height < 1:
        logger.debug("bogus image height {}".format(img_height))
        img_height = 1
    if max_width < 1:
        max_width = 1

    max_height = max_width * img_height / img_width

    width = _parse_length(width, em_width, max_width)
    height = _parse_length(height, em_width, max_height)

    if width and not height:
        height = img_height * width / img_width
    if height and not width:
        width = img_width * height / img_height

    if not width:
        width = img_width
    if not height:
        height = img_height

    ratio = img_width / img_height

    if _preserve_ratio(preserve_ratio):
        area = width * height
        # both must stay at least one pixel, a wide and short image otherwise
        # truncates the height to zero and the next division blows up
        height = max(1, int((area / ratio) ** 0.5))
        width = max(1, int(area / height))

    width = max(1, int(width))
    height = max(1, int(height))

    if width > max_width:
        height = max(1, int(height * max_width / width))
        width = int(max_width)

    return (width, height)
