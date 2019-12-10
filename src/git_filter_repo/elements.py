import io

from .ids import _IDS
from .pathquoting import PathQuoting


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


class FileChange(_GitElement):
    """
    This class defines our representation of file change elements. File change
    elements are components within a Commit element.
    """

    def __init__(self, type_, filename=None, id_=None, mode=None):
        _GitElement.__init__(self)

        # Denote the type of file-change (b'M' for modify, b'D' for delete, etc)
        # We could
        #   assert(type(type_) == bytes)
        # here but I don't just due to worries about performance overhead...
        self.type = type_

        # Record the name of the file being changed
        self.filename = filename

        # Record the mode (mode describes type of file entry (non-executable,
        # executable, or symlink)).
        self.mode = mode

        # blob_id is the id (mark) of the affected blob
        self.blob_id = id_

        if type_ == b"DELETEALL":
            assert filename is None and id_ is None and mode is None
            self.filename = b""  # Just so PathQuoting.enquote doesn't die
        else:
            assert filename is not None

        if type_ == b"M":
            assert id_ is not None and mode is not None
        elif type_ == b"D":
            assert id_ is None and mode is None
        elif type_ == b"R":  # pragma: no cover (now avoid fast-export renames)
            assert mode is None
            if id_ is None:
                raise SystemExit(_("new name needed for rename of %s") % filename)
            self.filename = (self.filename, id_)
            self.blob_id = None

    def dump(self, file_):
        """
        Write this file-change element to a file
        """
        skipped_blob = self.type == b"M" and self.blob_id is None
        if skipped_blob:
            return
        self.dumped = 1

        quoted_filename = PathQuoting.enquote(self.filename)
        if self.type == b"M" and isinstance(self.blob_id, int):
            file_.write(b"M %s :%d %s\n" % (self.mode, self.blob_id, quoted_filename))
        elif self.type == b"M":
            file_.write(b"M %s %s %s\n" % (self.mode, self.blob_id, quoted_filename))
        elif self.type == b"D":
            file_.write(b"D %s\n" % quoted_filename)
        elif self.type == b"DELETEALL":
            file_.write(b"deleteall\n")
        else:
            raise SystemExit(
                _("Unhandled filechange type: %s") % self.type
            )  # pragma: no cover
