import io


class _GitElement(object):
    """
    The base class for all git elements that we create.
    """

    def __init__(self):
        # A string that describes what type of Git element this is
        self.type = None

        # A flag telling us if this Git element has been dumped
        # (i.e. printed) or skipped.  Typically elements that have been
        # dumped or skipped will not be dumped again.
        self.dumped = 0

    def dump(self, file_):
        """
        This version should never be called. Derived classes need to
        override! We should note that subclasses should implement this
        method such that the output would match the format produced by
        fast-export.
        """
        raise SystemExit(
            _("Unimplemented function: %s") % type(self).__name__ + ".dump()"
        )  # pragma: no cover

    def __bytes__(self):
        """
        Convert GitElement to bytestring; used for debugging
        """
        old_dumped = self.dumped
        writeme = io.BytesIO()
        self.dump(writeme)
        output_lines = writeme.getvalue().splitlines()
        writeme.close()
        self.dumped = old_dumped
        return b"%s:\n  %s" % (type(self).__name__.encode(), b"\n  ".join(output_lines))

    def skip(self, new_id=None):
        """
        Ensures this element will not be written to output
        """
        self.dumped = 2


