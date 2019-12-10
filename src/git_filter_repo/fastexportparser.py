from .ids import _IDS
from .elements import (
    _SKIPPED_COMMITS,
    Blob,
    Commit,
    Checkpoint,
    FileChange,
    LiteralCommand,
    Progress,
    Reset,
    Tag,
)
from .pathquoting import PathQuoting


class FastExportParser(object):
    """
    A class for parsing and handling the output from fast-export. This
    class allows the user to register callbacks when various types of
    data are encountered in the fast-export output. The basic idea is that,
    FastExportParser takes fast-export output, creates the various objects
    as it encounters them, the user gets to use/modify these objects via
    callbacks, and finally FastExportParser outputs the modified objects
    in fast-import format (presumably so they can be used to create a new
    repo).
    """

    def __init__(
        self,
        tag_callback=None,
        commit_callback=None,
        blob_callback=None,
        progress_callback=None,
        reset_callback=None,
        checkpoint_callback=None,
        done_callback=None,
    ):
        # Members below simply store callback functions for the various git
        # elements
        self._tag_callback = tag_callback
        self._blob_callback = blob_callback
        self._reset_callback = reset_callback
        self._commit_callback = commit_callback
        self._progress_callback = progress_callback
        self._checkpoint_callback = checkpoint_callback
        self._done_callback = done_callback

        # Keep track of which refs appear from the export, and which make it to
        # the import (pruning of empty commits, renaming of refs, and creating
        # new manual objects and inserting them can cause these to differ).
        self._exported_refs = set()
        self._imported_refs = set()

        # A list of the branches we've seen, plus the last known commit they
        # pointed to.  An entry in latest_*commit will be deleted if we get a
        # reset for that branch.  These are used because of fast-import's weird
        # decision to allow having an implicit parent via naming the branch
        # instead of requiring branches to be specified via 'from' directives.
        self._latest_commit = {}
        self._latest_orig_commit = {}

        # A handle to the input source for the fast-export data
        self._input = None

        # A handle to the output file for the output we generate (we call dump
        # on many of the git elements we create).
        self._output = None

        # Stores the contents of the current line of input being parsed
        self._currentline = ""

        # Compile some regexes and cache those
        self._mark_re = re.compile(br"mark :(\d+)\n$")
        self._parent_regexes = {}
        parent_regex_rules = (b" :(\d+)\n$", b" ([0-9a-f]{40})\n")
        for parent_refname in (b"from", b"merge"):
            ans = [re.compile(parent_refname + x) for x in parent_regex_rules]
            self._parent_regexes[parent_refname] = ans
        self._quoted_string_re = re.compile(br'"(?:[^"\\]|\\.)*"')
        self._refline_regexes = {}
        for refline_name in (b"reset", b"commit", b"tag", b"progress"):
            self._refline_regexes[refline_name] = re.compile(refline_name + b" (.*)\n$")
        self._user_regexes = {}
        for user in (b"author", b"committer", b"tagger"):
            self._user_regexes[user] = re.compile(user + b" (.*?) <(.*?)> (.*)\n$")

    def _advance_currentline(self):
        """
        Grab the next line of input
        """
        self._currentline = self._input.readline()

    def _parse_optional_mark(self):
        """
        If the current line contains a mark, parse it and advance to the
        next line; return None otherwise
        """
        mark = None
        matches = self._mark_re.match(self._currentline)
        if matches:
            mark = int(matches.group(1))
            self._advance_currentline()
        return mark

    def _parse_optional_parent_ref(self, refname):
        """
        If the current line contains a reference to a parent commit, then
        parse it and advance the current line; otherwise return None. Note
        that the name of the reference ('from', 'merge') must match the
        refname arg.
        """
        orig_baseref, baseref = None, None
        rule, altrule = self._parent_regexes[refname]
        matches = rule.match(self._currentline)
        if matches:
            orig_baseref = int(matches.group(1))
            # We translate the parent commit mark to what it needs to be in
            # our mark namespace
            baseref = _IDS.translate(orig_baseref)
            self._advance_currentline()
        else:
            matches = altrule.match(self._currentline)
            if matches:
                orig_baseref = matches.group(1)
                baseref = orig_baseref
                self._advance_currentline()
        return orig_baseref, baseref

    def _parse_optional_filechange(self):
        """
        If the current line contains a file-change object, then parse it
        and advance the current line; otherwise return None. We only care
        about file changes of type b'M' and b'D' (these are the only types
        of file-changes that fast-export will provide).
        """
        filechange = None
        changetype = self._currentline[0:1]
        if changetype == b"M":
            (changetype, mode, idnum, path) = self._currentline.split(None, 3)
            if idnum[0:1] == b":":
                idnum = idnum[1:]
            path = path.rstrip(b"\n")
            # We translate the idnum to our id system
            if len(idnum) != 40:
                idnum = _IDS.translate(int(idnum))
            if idnum is not None:
                if path.startswith(b'"'):
                    path = PathQuoting.dequote(path)
                filechange = FileChange(b"M", path, idnum, mode)
            else:
                filechange = b"skipped"
            self._advance_currentline()
        elif changetype == b"D":
            (changetype, path) = self._currentline.split(None, 1)
            path = path.rstrip(b"\n")
            if path.startswith(b'"'):
                path = PathQuoting.dequote(path)
            filechange = FileChange(b"D", path)
            self._advance_currentline()
        elif changetype == b"R":  # pragma: no cover (now avoid fast-export renames)
            rest = self._currentline[2:-1]
            if rest.startswith(b'"'):
                m = self._quoted_string_re.match(rest)
                if not m:
                    raise SystemExit(_("Couldn't parse rename source"))
                orig = PathQuoting.dequote(m.group(0))
                new = rest[m.end() + 1 :]
            else:
                orig, new = rest.split(b" ", 1)
            if new.startswith(b'"'):
                new = PathQuoting.dequote(new)
            filechange = FileChange(b"R", orig, new)
            self._advance_currentline()
        return filechange

    def _parse_original_id(self):
        original_id = self._currentline[len(b"original-oid ") :].rstrip()
        self._advance_currentline()
        return original_id

    def _parse_encoding(self):
        encoding = self._currentline[len(b"encoding ") :].rstrip()
        self._advance_currentline()
        return encoding

    def _parse_ref_line(self, refname):
        """
        Parses string data (often a branch name) from current-line. The name of
        the string data must match the refname arg. The program will crash if
        current-line does not match, so current-line will always be advanced if
        this method returns.
        """
        matches = self._refline_regexes[refname].match(self._currentline)
        if not matches:
            raise SystemExit(
                _("Malformed %(refname)s line: '%(line)s'")
                % ({"refname": refname, "line": self._currentline})
            )  # pragma: no cover
        ref = matches.group(1)
        self._advance_currentline()
        return ref

    def _parse_user(self, usertype):
        """
        Get user name, email, datestamp from current-line. Current-line will
        be advanced.
        """
        user_regex = self._user_regexes[usertype]
        (name, email, when) = user_regex.match(self._currentline).groups()

        # TimeZone idiocy; IST is any of four timezones, so someone translated
        # it to something that was totally invalid...and it got recorded that
        # way.  Others have suggested just using an invalid timezone that
        # fast-import will not choke on.  Let's do that.  Note that +051800
        # seems to be the only weird timezone found in the wild, by me or some
        # other posts google returned on the subject...
        if when.endswith(b"+051800"):
            when = when[0:-7] + b"+0261"

        self._advance_currentline()
        return (name, email, when)

    def _parse_data(self):
        """
        Reads data from _input. Current-line will be advanced until it is beyond
        the data.
        """
        fields = self._currentline.split()
        assert fields[0] == b"data"
        size = int(fields[1])
        data = self._input.read(size)
        self._advance_currentline()
        if self._currentline == b"\n":
            self._advance_currentline()
        return data

    def _parse_blob(self):
        """
        Parse input data into a Blob object. Once the Blob has been created, it
        will be handed off to the appropriate callbacks. Current-line will be
        advanced until it is beyond this blob's data. The Blob will be dumped
        to _output once everything else is done (unless it has been skipped by
        the callback).
        """
        # Parse the Blob
        self._advance_currentline()
        id_ = self._parse_optional_mark()

        original_id = None
        if self._currentline.startswith(b"original-oid"):
            original_id = self._parse_original_id()

        data = self._parse_data()
        if self._currentline == b"\n":
            self._advance_currentline()

        # Create the blob
        blob = Blob(data, original_id)

        # If fast-export text had a mark for this blob, need to make sure this
        # mark translates to the blob's true id.
        if id_:
            blob.old_id = id_
            _IDS.record_rename(id_, blob.id)

        # Call any user callback to allow them to use/modify the blob
        if self._blob_callback:
            self._blob_callback(blob)

        # Now print the resulting blob
        if not blob.dumped:
            blob.dump(self._output)

    def _parse_reset(self):
        """
        Parse input data into a Reset object. Once the Reset has been created,
        it will be handed off to the appropriate callbacks. Current-line will
        be advanced until it is beyond the reset data. The Reset will be dumped
        to _output once everything else is done (unless it has been skipped by
        the callback).
        """
        # Parse the Reset
        ref = self._parse_ref_line(b"reset")
        self._exported_refs.add(ref)
        ignoreme, from_ref = self._parse_optional_parent_ref(b"from")
        if self._currentline == b"\n":
            self._advance_currentline()

        # fast-export likes to print extraneous resets that serve no purpose.
        # While we could continue processing such resets, that is a waste of
        # resources.  Also, we want to avoid recording that this ref was
        # seen in such cases, since this ref could be rewritten to nothing.
        if not from_ref:
            self._latest_commit.pop(ref, None)
            self._latest_orig_commit.pop(ref, None)
            return

        # Create the reset
        reset = Reset(ref, from_ref)

        # Call any user callback to allow them to modify the reset
        if self._reset_callback:
            self._reset_callback(reset)

        # Update metadata
        self._latest_commit[reset.ref] = reset.from_ref
        self._latest_orig_commit[reset.ref] = reset.from_ref

        # Now print the resulting reset
        if not reset.dumped:
            self._imported_refs.add(reset.ref)
            reset.dump(self._output)

    def _parse_commit(self):
        """
        Parse input data into a Commit object. Once the Commit has been created,
        it will be handed off to the appropriate callbacks. Current-line will
        be advanced until it is beyond the commit data. The Commit will be dumped
        to _output once everything else is done (unless it has been skipped by
        the callback OR the callback has removed all file-changes from the commit).
        """
        # Parse the Commit. This may look involved, but it's pretty simple; it only
        # looks bad because a commit object contains many pieces of data.
        branch = self._parse_ref_line(b"commit")
        self._exported_refs.add(branch)
        id_ = self._parse_optional_mark()

        original_id = None
        if self._currentline.startswith(b"original-oid"):
            original_id = self._parse_original_id()

        author_name = None
        if self._currentline.startswith(b"author"):
            (author_name, author_email, author_date) = self._parse_user(b"author")

        (committer_name, committer_email, committer_date) = self._parse_user(
            b"committer"
        )

        if not author_name:
            (author_name, author_email, author_date) = (
                committer_name,
                committer_email,
                committer_date,
            )

        encoding = None
        if self._currentline.startswith(b"encoding "):
            encoding = self._parse_encoding()

        commit_msg = self._parse_data()

        pinfo = [self._parse_optional_parent_ref(b"from")]
        # Due to empty pruning, we can have real 'from' and 'merge' lines that
        # due to commit rewriting map to a parent of None.  We need to record
        # 'from' if its non-None, and we need to parse all 'merge' lines.
        while self._currentline.startswith(b"merge "):
            pinfo.append(self._parse_optional_parent_ref(b"merge"))
        orig_parents, parents = [list(tmp) for tmp in zip(*pinfo)]

        # No parents is oddly represented as [None] instead of [], due to the
        # special 'from' handling.  Convert it here to a more canonical form.
        if parents == [None]:
            parents = []
        if orig_parents == [None]:
            orig_parents = []

        # fast-import format is kinda stupid in that it allows implicit parents
        # based on the branch name instead of requiring them to be specified by
        # 'from' directives.  The only way to get no parent is by using a reset
        # directive first, which clears the latest_commit_for_this_branch tracking.
        if not orig_parents and self._latest_commit.get(branch):
            parents = [self._latest_commit[branch]]
        if not orig_parents and self._latest_orig_commit.get(branch):
            orig_parents = [self._latest_orig_commit[branch]]

        # Get the list of file changes
        file_changes = []
        file_change = self._parse_optional_filechange()
        had_file_changes = file_change is not None
        while file_change:
            if not (type(file_change) == bytes and file_change == b"skipped"):
                file_changes.append(file_change)
            file_change = self._parse_optional_filechange()
        if self._currentline == b"\n":
            self._advance_currentline()

        # Okay, now we can finally create the Commit object
        commit = Commit(
            branch,
            author_name,
            author_email,
            author_date,
            committer_name,
            committer_email,
            committer_date,
            commit_msg,
            file_changes,
            parents,
            original_id,
            encoding,
        )

        # If fast-export text had a mark for this commit, need to make sure this
        # mark translates to the commit's true id.
        if id_:
            commit.old_id = id_
            _IDS.record_rename(id_, commit.id)

        # Call any user callback to allow them to modify the commit
        aux_info = {"orig_parents": orig_parents, "had_file_changes": had_file_changes}
        if self._commit_callback:
            self._commit_callback(commit, aux_info)

        # Now print the resulting commit, or if prunable skip it
        self._latest_orig_commit[branch] = commit.id
        if not (commit.old_id or commit.id) in _SKIPPED_COMMITS:
            self._latest_commit[branch] = commit.id
        if not commit.dumped:
            self._imported_refs.add(commit.branch)
            commit.dump(self._output)

    def _parse_tag(self):
        """
        Parse input data into a Tag object. Once the Tag has been created,
        it will be handed off to the appropriate callbacks. Current-line will
        be advanced until it is beyond the tag data. The Tag will be dumped
        to _output once everything else is done (unless it has been skipped by
        the callback).
        """
        # Parse the Tag
        tag = self._parse_ref_line(b"tag")
        self._exported_refs.add(b"refs/tags/" + tag)
        id_ = self._parse_optional_mark()
        ignoreme, from_ref = self._parse_optional_parent_ref(b"from")

        original_id = None
        if self._currentline.startswith(b"original-oid"):
            original_id = self._parse_original_id()

        tagger_name, tagger_email, tagger_date = None, None, None
        if self._currentline.startswith(b"tagger"):
            (tagger_name, tagger_email, tagger_date) = self._parse_user(b"tagger")
        tag_msg = self._parse_data()
        if self._currentline == b"\n":
            self._advance_currentline()

        # Create the tag
        tag = Tag(
            tag, from_ref, tagger_name, tagger_email, tagger_date, tag_msg, original_id
        )

        # If fast-export text had a mark for this tag, need to make sure this
        # mark translates to the tag's true id.
        if id_:
            tag.old_id = id_
            _IDS.record_rename(id_, tag.id)

        # Call any user callback to allow them to modify the tag
        if self._tag_callback:
            self._tag_callback(tag)

        # The tag might not point at anything that still exists (self.from_ref
        # will be None if the commit it pointed to and all its ancestors were
        # pruned due to being empty)
        if tag.from_ref:
            # Print out this tag's information
            if not tag.dumped:
                self._imported_refs.add(b"refs/tags/" + tag.ref)
                tag.dump(self._output)

    def _parse_progress(self):
        """
        Parse input data into a Progress object. Once the Progress has
        been created, it will be handed off to the appropriate
        callbacks. Current-line will be advanced until it is beyond the
        progress data. The Progress will be dumped to _output once
        everything else is done (unless it has been skipped by the callback).
        """
        # Parse the Progress
        message = self._parse_ref_line(b"progress")
        if self._currentline == b"\n":
            self._advance_currentline()

        # Create the progress message
        progress = Progress(message)

        # Call any user callback to allow them to modify the progress messsage
        if self._progress_callback:
            self._progress_callback(progress)

        # NOTE: By default, we do NOT print the progress message; git
        # fast-import would write it to fast_import_pipes which could mess with
        # our parsing of output from the 'ls' and 'get-mark' directives we send
        # to fast-import.  If users want these messages, they need to process
        # and handle them in the appropriate callback above.

    def _parse_checkpoint(self):
        """
        Parse input data into a Checkpoint object. Once the Checkpoint has
        been created, it will be handed off to the appropriate
        callbacks. Current-line will be advanced until it is beyond the
        checkpoint data. The Checkpoint will be dumped to _output once
        everything else is done (unless it has been skipped by the callback).
        """
        # Parse the Checkpoint
        self._advance_currentline()
        if self._currentline == b"\n":
            self._advance_currentline()

        # Create the checkpoint
        checkpoint = Checkpoint()

        # Call any user callback to allow them to drop the checkpoint
        if self._checkpoint_callback:
            self._checkpoint_callback(checkpoint)

        # NOTE: By default, we do NOT print the checkpoint message; although it
        # we would only realistically get them with --stdin, the fact that we
        # are filtering makes me think the checkpointing is less likely to be
        # reasonable.  In fact, I don't think it's necessary in general.  If
        # users do want it, they should process it in the checkpoint_callback.

    def _parse_literal_command(self):
        """
        Parse literal command.  Then just dump the line as is.
        """
        # Create the literal command object
        command = LiteralCommand(self._currentline)
        self._advance_currentline()

        # Now print the resulting literal command
        if not command.dumped:
            command.dump(self._output)

    def insert(self, obj):
        assert not obj.dumped
        obj.dump(self._output)
        if type(obj) == Commit:
            self._imported_refs.add(obj.branch)
        elif type(obj) in (Reset, Tag):
            self._imported_refs.add(obj.ref)

    def run(self, input, output):
        """
        This method filters fast export output.
        """
        # Set input. If no args provided, use stdin.
        self._input = input
        self._output = output

        # Run over the input and do the filtering
        self._advance_currentline()
        while self._currentline:
            if self._currentline.startswith(b"blob"):
                self._parse_blob()
            elif self._currentline.startswith(b"reset"):
                self._parse_reset()
            elif self._currentline.startswith(b"commit"):
                self._parse_commit()
            elif self._currentline.startswith(b"tag"):
                self._parse_tag()
            elif self._currentline.startswith(b"progress"):
                self._parse_progress()
            elif self._currentline.startswith(b"checkpoint"):
                self._parse_checkpoint()
            elif self._currentline.startswith(b"feature"):
                self._parse_literal_command()
            elif self._currentline.startswith(b"option"):
                self._parse_literal_command()
            elif self._currentline.startswith(b"done"):
                if self._done_callback:
                    self._done_callback()
                self._parse_literal_command()
                # Prevent confusion from others writing additional stuff that'll just
                # be ignored
                self._output.close()
            elif self._currentline.startswith(b"#"):
                self._parse_literal_command()
            elif (
                self._currentline.startswith(b"get-mark")
                or self._currentline.startswith(b"cat-blob")
                or self._currentline.startswith(b"ls")
            ):
                raise SystemExit(_("Unsupported command: '%s'") % self._currentline)
            else:
                raise SystemExit(_("Could not parse line: '%s'") % self._currentline)

    def get_exported_and_imported_refs(self):
        return self._exported_refs, self._imported_refs
