import io

from .ids import _IDS
from .pathquoting import PathQuoting


# Internal globals
_SKIPPED_COMMITS = set()
HASH_TO_ID = {}
ID_TO_HASH = {}


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


class Commit(_GitElementWithId):
    """
    This class defines our representation of commit elements. Commit elements
    contain all the information associated with a commit.
    """

    def __init__(
        self,
        branch,
        author_name,
        author_email,
        author_date,
        committer_name,
        committer_email,
        committer_date,
        message,
        file_changes,
        parents,
        original_id=None,
        encoding=None,  # encoding for message; None implies UTF-8
        **kwargs
    ):
        _GitElementWithId.__init__(self)
        self.old_id = self.id

        # Denote that this is a commit element
        self.type = "commit"

        # Record the affected branch
        self.branch = branch

        # Record original id
        self.original_id = original_id

        # Record author's name
        self.author_name = author_name

        # Record author's email
        self.author_email = author_email

        # Record date of authoring
        self.author_date = author_date

        # Record committer's name
        self.committer_name = committer_name

        # Record committer's email
        self.committer_email = committer_email

        # Record date the commit was made
        self.committer_date = committer_date

        # Record commit message and its encoding
        self.encoding = encoding
        self.message = message

        # List of file-changes associated with this commit. Note that file-changes
        # are also represented as git elements
        self.file_changes = file_changes

        self.parents = parents

    def dump(self, file_):
        """
        Write this commit element to a file.
        """
        self.dumped = 1
        HASH_TO_ID[self.original_id] = self.id
        ID_TO_HASH[self.id] = self.original_id

        # Make output to fast-import slightly easier for humans to read if the
        # message has no trailing newline of its own; cosmetic, but a nice touch...
        extra_newline = b"\n"
        if self.message.endswith(b"\n") or not (self.parents or self.file_changes):
            extra_newline = b""

        if not self.parents:
            file_.write(b"reset %s\n" % self.branch)
        file_.write(
            (
                b"commit %s\n"
                b"mark :%d\n"
                b"author %s <%s> %s\n"
                b"committer %s <%s> %s\n"
            )
            % (
                self.branch,
                self.id,
                self.author_name,
                self.author_email,
                self.author_date,
                self.committer_name,
                self.committer_email,
                self.committer_date,
            )
        )
        if self.encoding:
            file_.write(b"encoding %s\n" % self.encoding)
        file_.write(b"data %d\n%s%s" % (len(self.message), self.message, extra_newline))
        for i, parent in enumerate(self.parents):
            file_.write(b"from " if i == 0 else b"merge ")
            if isinstance(parent, int):
                file_.write(b":%d\n" % parent)
            else:
                file_.write(b"%s\n" % parent)
        for change in self.file_changes:
            change.dump(file_)
        if not self.parents and not self.file_changes:
            # Workaround a bug in pre-git-2.22 versions of fast-import with
            # the get-mark directive.
            file_.write(b"\n")
        file_.write(b"\n")

    def first_parent(self):
        """
        Return first parent commit
        """
        if self.parents:
            return self.parents[0]
        return None

    def skip(self, new_id=None):
        _SKIPPED_COMMITS.add(self.old_id or self.id)
        _GitElementWithId.skip(self, new_id)


class Tag(_GitElementWithId):
    """
    This class defines our representation of annotated tag elements.
    """

    def __init__(
        self,
        ref,
        from_ref,
        tagger_name,
        tagger_email,
        tagger_date,
        tag_msg,
        original_id=None,
    ):
        _GitElementWithId.__init__(self)
        self.old_id = self.id

        # Denote that this is a tag element
        self.type = "tag"

        # Store the name of the tag
        self.ref = ref

        # Store the entity being tagged (this should be a commit)
        self.from_ref = from_ref

        # Record original id
        self.original_id = original_id

        # Store the name of the tagger
        self.tagger_name = tagger_name

        # Store the email of the tagger
        self.tagger_email = tagger_email

        # Store the date
        self.tagger_date = tagger_date

        # Store the tag message
        self.message = tag_msg

    def dump(self, file_):
        """
        Write this tag element to a file
        """

        self.dumped = 1
        HASH_TO_ID[self.original_id] = self.id
        ID_TO_HASH[self.id] = self.original_id

        file_.write(b"tag %s\n" % self.ref)
        if write_marks and self.id:
            file_.write(b"mark :%d\n" % self.id)
        markfmt = b"from :%d\n" if isinstance(self.from_ref, int) else b"from %s\n"
        file_.write(markfmt % self.from_ref)
        if self.tagger_name:
            file_.write(b"tagger %s <%s> " % (self.tagger_name, self.tagger_email))
            file_.write(self.tagger_date)
            file_.write(b"\n")
        file_.write(b"data %d\n%s" % (len(self.message), self.message))
        file_.write(b"\n")


class Progress(_GitElement):
    """
    This class defines our representation of progress elements. The progress
    element only contains a progress message, which is printed by fast-import
    when it processes the progress output.
    """

    def __init__(self, message):
        _GitElement.__init__(self)

        # Denote that this is a progress element
        self.type = "progress"

        # Store the progress message
        self.message = message

    def dump(self, file_):
        """
        Write this progress element to a file
        """
        self.dumped = 1

        file_.write(b"progress %s\n" % self.message)
        file_.write(b"\n")


class Checkpoint(_GitElement):
    """
    This class defines our representation of checkpoint elements.  These
    elements represent events which force fast-import to close the current
    packfile, start a new one, and to save out all current branch refs, tags
    and marks.
    """

    def __init__(self):
        _GitElement.__init__(self)

        # Denote that this is a checkpoint element
        self.type = "checkpoint"

    def dump(self, file_):
        """
        Write this checkpoint element to a file
        """
        self.dumped = 1

        file_.write(b"checkpoint\n")
        file_.write(b"\n")


class LiteralCommand(_GitElement):
    """
    This class defines our representation of commands. The literal command
    includes only a single line, and is not processed in any special way.
    """

    def __init__(self, line):
        _GitElement.__init__(self)

        # Denote that this is a literal element
        self.type = "literal"

        # Store the command
        self.line = line

    def dump(self, file_):
        """
        Write this progress element to a file
        """
        self.dumped = 1

        file_.write(self.line)
