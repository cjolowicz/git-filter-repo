import re


class PathQuoting:
    _unescape = {
        b"a": b"\a",
        b"b": b"\b",
        b"f": b"\f",
        b"n": b"\n",
        b"r": b"\r",
        b"t": b"\t",
        b"v": b"\v",
        b'"': b'"',
        b"\\": b"\\",
    }
    _unescape_re = re.compile(br'\\([a-z"\\]|[0-9]{3})')
    _escape = [bytes([x]) for x in range(127)] + [
        b"\\" + bytes(ord(c) for c in oct(x)[2:]) for x in range(127, 256)
    ]
    _reverse = dict(map(reversed, _unescape.items()))
    for x in _reverse:
        _escape[ord(x)] = b"\\" + _reverse[x]
    _special_chars = [len(x) > 1 for x in _escape]

    @staticmethod
    def unescape_sequence(orig):
        seq = orig.group(1)
        return PathQuoting._unescape[seq] if len(seq) == 1 else bytes([int(seq, 8)])

    @staticmethod
    def dequote(quoted_string):
        if quoted_string.startswith(b'"'):
            assert quoted_string.endswith(b'"')
            return PathQuoting._unescape_re.sub(
                PathQuoting.unescape_sequence, quoted_string[1:-1]
            )
        return quoted_string

    @staticmethod
    def enquote(unquoted_string):
        # Option 1: Quoting when fast-export would:
        #    pqsc = PathQuoting._special_chars
        #    if any(pqsc[x] for x in set(unquoted_string)):
        # Option 2, perf hack: do minimal amount of quoting required by fast-import
        if unquoted_string.startswith(b'"') or b"\n" in unquoted_string:
            pqe = PathQuoting._escape
            return b'"' + b"".join(pqe[x] for x in unquoted_string) + b'"'
        return unquoted_string
