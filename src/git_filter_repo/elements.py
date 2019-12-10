import io

from .ids import _IDS


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


class _GitElementWithId(_GitElement):
    """
    The base class for Git elements that have IDs (commits and blobs)
    """

    def __init__(self):
        _GitElement.__init__(self)

        # The mark (short, portable id) for this element
        self.id = _IDS.new()

        # The previous mark for this element
        self.old_id = None

    def skip(self, new_id=None):
        """
        This element will no longer be automatically written to output. When a
        commit gets skipped, it's ID will need to be translated to that of its
        parent.
        """
        self.dumped = 2

        _IDS.record_rename(self.old_id or self.id, new_id)


class Blob(_GitElementWithId):
    """
    This class defines our representation of git blob elements (i.e. our
    way of representing file contents).
    """

    def __init__(self, data, original_id=None):
        _GitElementWithId.__init__(self)

        # Denote that this is a blob
        self.type = "blob"

        # Record original id
        self.original_id = original_id

        # Stores the blob's data
        assert type(data) == bytes
        self.data = data

    def dump(self, file_):
        """
        Write this blob element to a file.
        """
        self.dumped = 1
        HASH_TO_ID[self.original_id] = self.id
        ID_TO_HASH[self.id] = self.original_id

        file_.write(b"blob\n")
        file_.write(b"mark :%d\n" % self.id)
        file_.write(b"data %d\n%s" % (len(self.data), self.data))
        file_.write(b"\n")


class Reset(_GitElement):
    """
    This class defines our representation of git reset elements.  A reset
    event is the creation (or recreation) of a named branch, optionally
    starting from a specific revision).
    """

    def __init__(self, ref, from_ref=None):
        _GitElement.__init__(self)

        # Denote that this is a reset
        self.type = "reset"

        # The name of the branch being (re)created
        self.ref = ref

        # Some reference to the branch/commit we are resetting from
        self.from_ref = from_ref

    def dump(self, file_):
        """
        Write this reset element to a file
        """
        self.dumped = 1

        file_.write(b"reset %s\n" % self.ref)
        if self.from_ref:
            if isinstance(self.from_ref, int):
                file_.write(b"from :%d\n" % self.from_ref)
            else:
                file_.write(b"from %s\n" % self.from_ref)
            file_.write(b"\n")
