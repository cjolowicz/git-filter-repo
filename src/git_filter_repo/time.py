from datetime import tzinfo, timedelta, datetime
import re


def _timedelta_to_seconds(delta):
    """
    Converts timedelta to seconds
    """
    offset = delta.days * 86400 + delta.seconds + (delta.microseconds + 0.0) / 1000000
    return round(offset)


class FixedTimeZone(tzinfo):
    """
    Fixed offset in minutes east from UTC.
    """

    tz_re = re.compile(br"^([-+]?)(\d\d)(\d\d)$")

    def __init__(self, offset_string):
        tzinfo.__init__(self)
        sign, hh, mm = FixedTimeZone.tz_re.match(offset_string).groups()
        factor = -1 if (sign and sign == b"-") else 1
        self._offset = timedelta(minutes=factor * (60 * int(hh) + int(mm)))
        self._offset_string = offset_string

    def utcoffset(self, dt):
        return self._offset

    def tzname(self, dt):
        return self._offset_string

    def dst(self, dt):
        return timedelta(0)


def string_to_date(datestring):
    (unix_timestamp, tz_offset) = datestring.split()
    return datetime.fromtimestamp(int(unix_timestamp), FixedTimeZone(tz_offset))


def date_to_string(dateobj):
    epoch = datetime.fromtimestamp(0, dateobj.tzinfo)
    return b"%d %s" % (
        int(_timedelta_to_seconds(dateobj - epoch)),
        dateobj.tzinfo.tzname(0),
    )


