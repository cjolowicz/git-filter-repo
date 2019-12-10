import collections
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import time
import textwrap

from .ancestrygraph import AncestryGraph
from .elements import (
    _SKIPPED_COMMITS,
    Alias,
    Blob,
    Commit,
    HASH_TO_ID,
    ID_TO_HASH,
    Reset,
    Tag,
    write_marks,
)
from .gettext import _
from .gitutils import GitUtils
from .ids import _IDS
from .mailmap import MailmapInfo
from .pathquoting import PathQuoting
from .progress import ProgressWriter
from .subprocess import subproc
from .utils import decode


deleted_hash = b"0" * 40


class InputFileBackup:
    def __init__(self, input_file, output_file):
        self.input_file = input_file
        self.output_file = output_file

    def read(self, size):
        output = self.input_file.read(size)
        self.output_file.write(output)
        return output

    def readline(self):
        line = self.input_file.readline()
        self.output_file.write(line)
        return line


class DualFileWriter:
    def __init__(self, file1, file2):
        self.file1 = file1
        self.file2 = file2

    def write(self, *args):
        self.file1.write(*args)
        self.file2.write(*args)

    def flush(self):
        self.file1.flush()
        self.file2.flush()

    def close(self):
        self.file1.close()
        self.file2.close()


class RepoFilter(object):
    def __init__(
        self,
        args,
        filename_callback=None,
        message_callback=None,
        name_callback=None,
        email_callback=None,
        refname_callback=None,
        blob_callback=None,
        commit_callback=None,
        tag_callback=None,
        reset_callback=None,
        done_callback=None,
    ):

        self._args = args

        # Repo we are exporting
        self._repo_working_dir = None

        # Store callbacks for acting on objects printed by FastExport
        self._blob_callback = blob_callback
        self._commit_callback = commit_callback
        self._tag_callback = tag_callback
        self._reset_callback = reset_callback
        self._done_callback = done_callback

        # Store callbacks for acting on slices of FastExport objects
        self._filename_callback = filename_callback  # filenames from commits
        self._message_callback = message_callback  # commit OR tag message
        self._name_callback = name_callback  # author, committer, tagger
        self._email_callback = email_callback  # author, committer, tagger
        self._refname_callback = refname_callback  # from commit/tag/reset
        self._handle_arg_callbacks()

        # Defaults for input
        self._input = None
        self._fep = None  # Fast Export Process
        self._fe_orig = None  # Path to where original fast-export output stored
        self._fe_filt = None  # Path to where filtered fast-export output stored
        self._parser = None  # FastExportParser object we are working with

        # Defaults for output
        self._output = None
        self._fip = None  # Fast Import Process
        self._import_pipes = None
        self._managed_output = True

        # A tuple of (depth, list-of-ancestors).  Commits and ancestors are
        # identified by their id (their 'mark' in fast-export or fast-import
        # speak).  The depth of a commit is one more than the max depth of any
        # of its ancestors.
        self._graph = AncestryGraph()
        # Another one, for ancestry of commits in the original repo
        self._orig_graph = AncestryGraph()

        # Names of files that were tweaked in any commit; such paths could lead
        # to subsequent commits being empty
        self._files_tweaked = set()

        # A set of commit hash pairs (oldhash, newhash) which used to be merge
        # commits but due to filtering were turned into non-merge commits.
        # The commits probably have suboptimal commit messages (e.g. "Merge branch
        # next into master").
        self._commits_no_longer_merges = []

        # A dict of original_ids to new_ids; filtering commits means getting
        # new commit hash (sha1sums), and we record the mapping both for
        # diagnostic purposes and so we can rewrite commit messages.  Note that
        # the new_id can be None rather than a commit hash if the original
        # commit became empty and was pruned or was otherwise dropped.
        self._commit_renames = {}

        # A set of original_ids for which we have not yet gotten the
        # new_ids; we use OrderedDict because we need to know the order of
        # insertion, but the values are always ignored (and set to None).
        # If there was an OrderedSet class, I'd use it instead.
        self._pending_renames = collections.OrderedDict()

        # A dict of commit_hash[0:7] -> set(commit_hashes with that prefix).
        #
        # It's common for commit messages to refer to commits by abbreviated
        # commit hashes, as short as 7 characters.  To facilitate translating
        # such short hashes, we have a mapping of prefixes to full old hashes.
        self._commit_short_old_hashes = collections.defaultdict(set)

        # A set of commit hash references appearing in commit messages which
        # mapped to a valid commit that was removed entirely in the filtering
        # process.  The commit message will continue to reference the
        # now-missing commit hash, since there was nothing to map it to.
        self._commits_referenced_but_removed = set()

        # Progress handling (number of commits parsed, etc.)
        self._progress_writer = ProgressWriter()
        self._num_commits = 0

        # Size of blobs in the repo
        self._unpacked_size = {}

        # Other vars
        self._sanity_checks_handled = False
        self._finalize_handled = False
        self._orig_refs = None
        self._newnames = {}

        # Cache a few message translations for performance reasons
        self._parsed_message = _("Parsed %d commits")

        # Compile some regexes and cache those
        self._hash_re = re.compile(br"(\b[0-9a-f]{7,40}\b)")

    def _handle_arg_callbacks(self):
        def make_callback(argname, str):
            exec(
                "def callback({}, _do_not_use_this_var = None):\n".format(argname)
                + "  "
                + "\n  ".join(str.splitlines()),
                globals(),
            )
            return callback  # namespace['callback']

        def handle(type):
            callback_field = "_{}_callback".format(type)
            code_string = getattr(self._args, type + "_callback")
            if code_string:
                if getattr(self, callback_field):
                    raise SystemExit(
                        _(
                            "Error: Cannot pass a %s_callback to RepoFilter "
                            "AND pass --%s-callback" % (type, type)
                        )
                    )
                if "return " not in code_string and type not in (
                    "blob",
                    "commit",
                    "tag",
                    "reset",
                ):
                    raise SystemExit(
                        _("Error: --%s-callback should have a return statement") % type
                    )
                setattr(self, callback_field, make_callback(type, code_string))

        handle("filename")
        handle("message")
        handle("name")
        handle("email")
        handle("refname")
        handle("blob")
        handle("commit")
        handle("tag")
        handle("reset")

    def _run_sanity_checks(self):
        self._sanity_checks_handled = True
        if not self._managed_output:
            if not self._args.replace_refs:
                # If not _managed_output we don't want to make extra changes to the
                # repo, so set default to no-op 'update-no-add'
                self._args.replace_refs = "update-no-add"
            return

        if self._args.debug:
            print("[DEBUG] Passed arguments:\n{}".format(self._args))

        # Determine basic repository information
        target_working_dir = self._args.target or b"."
        self._orig_refs = GitUtils.get_refs(target_working_dir)
        is_bare = GitUtils.is_repository_bare(target_working_dir)

        # Determine if this is second or later run of filter-repo
        tmp_dir = self.results_tmp_dir(create_if_missing=False)
        already_ran = os.path.isfile(os.path.join(tmp_dir, b"already_ran"))

        # Default for --replace-refs
        if not self._args.replace_refs:
            self._args.replace_refs = (
                "update-or-add" if already_ran else "update-and-add"
            )

        # Do sanity checks from the correct directory
        if not self._args.force and not already_ran:
            cwd = os.getcwd()
            os.chdir(target_working_dir)
            RepoFilter.sanity_check(self._orig_refs, is_bare)
            os.chdir(cwd)

    @staticmethod
    def sanity_check(refs, is_bare):
        def abort(reason):
            raise SystemExit(
                _(
                    "Aborting: Refusing to overwrite repo history since this does not\n"
                    "look like a fresh clone.\n"
                    "  (%s)\n"
                    "To override, use --force."
                )
                % reason
            )

        # Make sure repo is fully packed, just like a fresh clone would be
        output = subproc.check_output("git count-objects -v".split())
        stats = dict(x.split(b": ") for x in output.splitlines())
        num_packs = int(stats[b"packs"])
        if stats[b"count"] != b"0" or num_packs > 1:
            abort(_("expected freshly packed repo"))

        # Make sure there is precisely one remote, named "origin"...or that this
        # is a new bare repo with no packs and no remotes
        output = subproc.check_output("git remote".split()).strip()
        if not (output == b"origin" or (num_packs == 0 and not output)):
            abort(_("expected one remote, origin"))

        # Avoid letting people running with weird setups and overwriting GIT_DIR
        # elsewhere
        git_dir = GitUtils.determine_git_dir(b".")
        if is_bare and git_dir != b".":
            abort(_("GIT_DIR must be ."))
        elif not is_bare and git_dir != b".git":
            abort(_("GIT_DIR must be .git"))

        # Make sure that all reflogs have precisely one entry
        reflog_dir = os.path.join(git_dir, b"logs")
        for root, dirs, files in os.walk(reflog_dir):
            for filename in files:
                pathname = os.path.join(root, filename)
                with open(pathname, "br") as f:
                    if len(f.read().splitlines()) > 1:
                        shortpath = pathname[len(reflog_dir) + 1 :]
                        abort(
                            _("expected at most one entry in the reflog for %s")
                            % decode(shortpath)
                        )

        # Make sure there are no stashed changes
        if b"refs/stash" in refs:
            abort(_("has stashed changes"))

        # Do extra checks in non-bare repos
        if not is_bare:
            # Avoid uncommitted, unstaged, or untracked changes
            if subproc.call("git diff --staged --quiet".split()):
                abort(_("you have uncommitted changes"))
            if subproc.call("git diff --quiet".split()):
                abort(_("you have unstaged changes"))
            if len(subproc.check_output("git ls-files -o".split())) > 0:
                abort(_("you have untracked changes"))

            # Avoid unpushed changes
            for refname, rev in refs.items():
                if not refname.startswith(b"refs/heads/"):
                    continue
                origin_ref = refname.replace(b"refs/heads/", b"refs/remotes/origin/")
                if origin_ref not in refs:
                    abort(
                        _("%s exists, but %s not found")
                        % (decode(refname), decode(origin_ref))
                    )
                if rev != refs[origin_ref]:
                    abort(
                        _("%s does not match %s")
                        % (decode(refname), decode(origin_ref))
                    )

            # Make sure there is only one worktree
            output = subproc.check_output("git worktree list".split())
            if len(output.splitlines()) > 1:
                abort(_("you have multiple worktrees"))

    @staticmethod
    def cleanup(repo, repack, reset, run_quietly=False, show_debuginfo=False):
        """ cleanup repo; if repack then expire reflogs and do a gc --prune=now.
        if reset then do a reset --hard.  Optionally also curb output if
        run_quietly is True, or go the opposite direction and show extra
        output if show_debuginfo is True. """
        assert not (run_quietly and show_debuginfo)

        if repack and not run_quietly and not show_debuginfo:
            print(_("Repacking your repo and cleaning out old unneeded objects"))
        quiet_flags = "--quiet" if run_quietly else ""
        cleanup_cmds = []
        if repack:
            cleanup_cmds = [
                "git reflog expire --expire=now --all".split(),
                "git gc {} --prune=now".format(quiet_flags).split(),
            ]
        if reset:
            cleanup_cmds.insert(0, "git reset {} --hard".format(quiet_flags).split())
        location_info = " (in {})".format(decode(repo)) if repo != b"." else ""
        for cmd in cleanup_cmds:
            if show_debuginfo:
                print("[DEBUG] Running{}: {}".format(location_info, " ".join(cmd)))
            subproc.call(cmd, cwd=repo)

    def _get_rename(self, old_hash):
        # If we already know the rename, just return it
        new_hash = self._commit_renames.get(old_hash, None)
        if new_hash:
            return new_hash

        # If it's not in the remaining pending renames, we don't know it
        if old_hash is not None and old_hash not in self._pending_renames:
            return None

        # Read through the pending renames until we find it or we've read them all,
        # and return whatever we might find
        self._flush_renames(old_hash)
        return self._commit_renames.get(old_hash, None)

    def _flush_renames(self, old_hash=None, limit=0):
        # Parse through self._pending_renames until we have read enough.  We have
        # read enough if:
        #   self._pending_renames is empty
        #   old_hash != None and we found a rename for old_hash
        #   limit > 0 and len(self._pending_renames) started less than 2*limit
        #   limit > 0 and len(self._pending_renames) < limit
        if limit and len(self._pending_renames) < 2 * limit:
            return
        fi_input, fi_output = self._import_pipes
        while self._pending_renames:
            orig_id, ignore = self._pending_renames.popitem(last=False)
            new_id = fi_output.readline().rstrip()
            self._commit_renames[orig_id] = new_id
            if old_hash == orig_id:
                return
            if limit and len(self._pending_renames) < limit:
                return

    def _translate_commit_hash(self, matchobj_or_oldhash):
        old_hash = matchobj_or_oldhash
        if not isinstance(matchobj_or_oldhash, bytes):
            old_hash = matchobj_or_oldhash.group(1)
        orig_len = len(old_hash)
        new_hash = self._get_rename(old_hash)
        if new_hash is None:
            if old_hash[0:7] not in self._commit_short_old_hashes:
                self._commits_referenced_but_removed.add(old_hash)
                return old_hash
            possibilities = self._commit_short_old_hashes[old_hash[0:7]]
            matches = [x for x in possibilities if x[0:orig_len] == old_hash]
            if len(matches) != 1:
                self._commits_referenced_but_removed.add(old_hash)
                return old_hash
            old_hash = matches[0]
            new_hash = self._get_rename(old_hash)

        assert new_hash is not None
        return new_hash[0:orig_len]

    def _trim_extra_parents(self, orig_parents, parents):
        """Due to pruning of empty commits, some parents could be non-existent
        (None) or otherwise redundant.  Remove the non-existent parents, and
        remove redundant parents so long as that doesn't transform a merge
        commit into a non-merge commit.

        Returns a tuple:
         (parents, new_first_parent_if_would_become_non_merge)"""

        if self._args.prune_degenerate == "never":
            return parents, None
        always_prune = self._args.prune_degenerate == "always"

        # Pruning of empty commits means multiple things:
        #   * An original parent of this commit may have been pruned causing the
        #     need to rewrite the reported parent to the nearest ancestor.  We
        #     want to know when we're dealing with such a parent.
        #   * Further, there may be no "nearest ancestor" if the entire history
        #     of that parent was also pruned.  (Detectable by the parent being
        #     'None')
        # Remove all parents rewritten to None, and keep track of which parents
        # were rewritten to an ancestor.
        tmp = zip(
            parents,
            orig_parents,
            [(x in _SKIPPED_COMMITS or always_prune) for x in orig_parents],
        )
        tmp2 = [x for x in tmp if x[0] is not None]
        if not tmp2:
            # All ancestors have been pruned; we have no parents.
            return [], None
        parents, orig_parents, is_rewritten = [list(x) for x in zip(*tmp2)]

        # We can't have redundant parents if we don't have at least 2 parents
        if len(parents) < 2:
            return parents, None

        # Remove duplicate parents (if both sides of history have lots of commits
        # which become empty due to pruning, the most recent ancestor on both
        # sides may be the same commit), except only remove parents that have
        # been rewritten due to previous empty pruning.
        seen = set()
        seen_add = seen.add
        # Deleting duplicate rewritten parents means keeping parents if either
        # they have not been seen or they are ones that have not been rewritten.
        parents_copy = parents
        uniq = [
            [p, orig_parents[i], is_rewritten[i]]
            for i, p in enumerate(parents)
            if not (p in seen or seen_add(p)) or not is_rewritten[i]
        ]
        parents, orig_parents, is_rewritten = [list(x) for x in zip(*uniq)]
        if len(parents) < 2:
            return parents_copy, parents[0]

        # Flatten unnecessary merges.  (If one side of history is entirely
        # empty commits that were pruned, we may end up attempting to
        # merge a commit with its ancestor.  Remove parents that are an
        # ancestor of another parent.)
        num_parents = len(parents)
        to_remove = []
        for cur in range(num_parents):
            if not is_rewritten[cur]:
                continue
            for other in range(num_parents):
                if cur == other:
                    continue
                if not self._graph.is_ancestor(parents[cur], parents[other]):
                    continue
                # parents[cur] is an ancestor of parents[other], so parents[cur]
                # seems redundant.  However, if it was intentionally redundant
                # (e.g. a no-ff merge) in the original, then we want to keep it.
                if not always_prune and self._orig_graph.is_ancestor(
                    orig_parents[cur], orig_parents[other]
                ):
                    continue
                # Okay so the cur-th parent is an ancestor of the other-th parent,
                # and it wasn't that way in the original repository; mark the
                # cur-th parent as removable.
                to_remove.append(cur)
                break  # cur removed, so skip rest of others -- i.e. check cur+=1
        for x in reversed(to_remove):
            parents.pop(x)
        if len(parents) < 2:
            return parents_copy, parents[0]

        return parents, None

    def _prunable(self, commit, new_1st_parent, had_file_changes, orig_parents):
        parents = commit.parents

        if self._args.prune_empty == "never":
            return False
        always_prune = self._args.prune_empty == "always"

        # For merge commits, unless there are prunable (redundant) parents, we
        # do not want to prune
        if len(parents) >= 2 and not new_1st_parent:
            return False

        if len(parents) < 2:
            # Special logic for commits that started empty...
            if not had_file_changes and not always_prune:
                had_parents_pruned = len(parents) < len(orig_parents) or (
                    len(orig_parents) == 1 and orig_parents[0] in _SKIPPED_COMMITS
                )
                # If the commit remains empty and had parents which were pruned,
                # then prune this commit; otherwise, retain it
                return not commit.file_changes and had_parents_pruned

            # We can only get here if the commit didn't start empty, so if it's
            # empty now, it obviously became empty
            if not commit.file_changes:
                return True

        # If there are no parents of this commit and we didn't match the case
        # above, then this commit cannot be pruned.  Since we have no parent(s)
        # to compare to, abort now to prevent future checks from failing.
        if not parents:
            return False

        # Similarly, we cannot handle the hard cases if we don't have a pipe
        # to communicate with fast-import
        if not self._import_pipes:
            return False

        # non-merge commits can only be empty if blob/file-change editing caused
        # all file changes in the commit to have the same file contents as
        # the parent.
        changed_files = set(change.filename for change in commit.file_changes)
        if len(orig_parents) < 2 and changed_files - self._files_tweaked:
            return False

        # Finally, the hard case: due to either blob rewriting, or due to pruning
        # of empty commits wiping out the first parent history back to the merge
        # base, the list of file_changes we have may not actually differ from our
        # (new) first parent's version of the files, i.e. this would actually be
        # an empty commit.  Check by comparing the contents of this commit to its
        # (remaining) parent.
        #
        # NOTE on why this works, for the case of original first parent history
        # having been pruned away due to being empty:
        #     The first parent history having been pruned away due to being
        #     empty implies the original first parent would have a tree (after
        #     filtering) that matched the merge base's tree.  Since
        #     file_changes has the changes needed to go from what would have
        #     been the first parent to our new commit, and what would have been
        #     our first parent has a tree that matches the merge base, then if
        #     the new first parent has a tree matching the versions of files in
        #     file_changes, then this new commit is empty and thus prunable.
        fi_input, fi_output = self._import_pipes
        self._flush_renames()  # Avoid fi_output having other stuff present
        # Optimization note: we could have two loops over file_changes, the
        # first doing all the self._output.write() calls, and the second doing
        # the rest.  But I'm worried about fast-import blocking on fi_output
        # buffers filling up so I instead read from it as I go.
        for change in commit.file_changes:
            parent = new_1st_parent or commit.parents[0]  # exists due to above checks
            quoted_filename = PathQuoting.enquote(change.filename)
            self._output.write(b"ls :%d %s\n" % (parent, quoted_filename))
            self._output.flush()
            parent_version = fi_output.readline().split()
            if change.type == b"D":
                if parent_version != [b"missing", quoted_filename]:
                    return False
            else:
                blob_sha = change.blob_id
                if isinstance(change.blob_id, int):
                    self._output.write(b"get-mark :%d\n" % change.blob_id)
                    self._output.flush()
                    blob_sha = fi_output.readline().rstrip()
                if parent_version != [change.mode, b"blob", blob_sha, quoted_filename]:
                    return False

        return True

    def _record_remapping(self, commit, orig_parents):
        new_id = None
        # Record the mapping of old commit hash to new one
        if commit.original_id and self._import_pipes:
            fi_input, fi_output = self._import_pipes
            self._output.write(b"get-mark :%d\n" % commit.id)
            self._output.flush()
            orig_id = commit.original_id
            self._commit_short_old_hashes[orig_id[0:7]].add(orig_id)
            # Note that we have queued up an id for later reading; flush a
            # few of the older ones if we have too many queued up
            self._pending_renames[orig_id] = None
            self._flush_renames(None, limit=40)
        # Also, record if this was a merge commit that turned into a non-merge
        # commit.
        if len(orig_parents) >= 2 and len(commit.parents) < 2:
            self._commits_no_longer_merges.append((commit.original_id, new_id))

    def callback_metadata(self, extra_items=dict()):
        return {
            "commit_rename_func": self._translate_commit_hash,
            "ancestry_graph": self._graph,
            "original_ancestry_graph": self._orig_graph,
            **extra_items,
        }

    def _tweak_blob(self, blob):
        if self._args.max_blob_size and len(blob.data) > self._args.max_blob_size:
            blob.skip()

        if blob.original_id in self._args.strip_blobs_with_ids:
            blob.skip()

        if self._args.replace_text:
            for literal, replacement in self._args.replace_text["literals"]:
                blob.data = blob.data.replace(literal, replacement)
            for regex, replacement in self._args.replace_text["regexes"]:
                blob.data = regex.sub(replacement, blob.data)

        if self._blob_callback:
            self._blob_callback(blob, self.callback_metadata())

    def _tweak_commit(self, commit, aux_info):
        def filename_matches(path_expression, pathname):
            """ Returns whether path_expression matches pathname or a leading
            directory thereof, allowing path_expression to not have a trailing
            slash even if it is meant to match a leading directory. """
            if path_expression == b"":
                return True
            n = len(path_expression)
            if pathname.startswith(path_expression) and (
                path_expression[n - 1 : n] == b"/"
                or len(pathname) == n
                or pathname[n : n + 1] == b"/"
            ):
                return True
            return False

        def newname(path_changes, pathname, use_base_name, filtering_is_inclusive):
            """ Applies filtering and rename changes from path_changes to pathname,
            returning any of None (file isn't wanted), original filename (file
            is wanted with original name), or new filename. """
            wanted = False
            full_pathname = pathname
            if use_base_name:
                pathname = os.path.basename(pathname)
            for (mod_type, match_type, path_exp) in path_changes:
                if mod_type == "filter" and not wanted:
                    assert match_type in ("match", "glob", "regex")
                    if match_type == "match" and filename_matches(path_exp, pathname):
                        wanted = True
                    if match_type == "glob" and fnmatch.fnmatch(pathname, path_exp):
                        wanted = True
                    if match_type == "regex" and path_exp.search(pathname):
                        wanted = True
                elif mod_type == "rename":
                    match, repl = path_exp
                    assert match_type in (
                        "match",
                        "regex",
                    )  # glob was translated to regex
                    if match_type == "match" and filename_matches(match, full_pathname):
                        full_pathname = full_pathname.replace(match, repl, 1)
                    if match_type == "regex":
                        full_pathname = match.sub(repl, full_pathname)
            return full_pathname if (wanted == filtering_is_inclusive) else None

        # Change the commit message according to callback
        if not self._args.preserve_commit_hashes:
            commit.message = self._hash_re.sub(
                self._translate_commit_hash, commit.message
            )
        if self._message_callback:
            commit.message = self._message_callback(commit.message)

        # Change the author & committer according to mailmap rules
        args = self._args
        if args.mailmap:
            commit.author_name, commit.author_email = args.mailmap.translate(
                commit.author_name, commit.author_email
            )
            commit.committer_name, commit.committer_email = args.mailmap.translate(
                commit.committer_name, commit.committer_email
            )
        # Change author & committer according to callbacks
        if self._name_callback:
            commit.author_name = self._name_callback(commit.author_name)
            commit.committer_name = self._name_callback(commit.committer_name)
        if self._email_callback:
            commit.author_email = self._email_callback(commit.author_email)
            commit.committer_email = self._email_callback(commit.committer_email)

        # Sometimes the 'branch' given is a tag; if so, rename it as requested so
        # we don't get any old tagnames
        if self._args.tag_rename:
            commit.branch = RepoFilter._do_tag_rename(args.tag_rename, commit.branch)
        if self._refname_callback:
            commit.branch = self._refname_callback(commit.branch)

        # Filter or rename the list of file changes
        orig_file_changes = set(commit.file_changes)
        new_file_changes = {}  # Assumes no renames or copies, otherwise collisions
        for change in commit.file_changes:
            # NEEDSWORK: _If_ we ever want to pass `--full-tree` to fast-export and
            # parse that output, we'll need to modify this block; `--full-tree`
            # issues a deleteall directive which has no filename, and thus this
            # block would normally strip it.  Of course, FileChange() and
            # _parse_optional_filechange() would need updates too.
            if change.type == b"DELETEALL":
                new_file_changes[b""] = change
                continue
            if change.filename in self._newnames:
                change.filename = self._newnames[change.filename]
            else:
                change.filename = newname(
                    args.path_changes,
                    change.filename,
                    args.use_base_name,
                    args.inclusive,
                )
                if self._filename_callback:
                    change.filename = self._filename_callback(change.filename)
                self._newnames[change.filename] = change.filename
            if not change.filename:
                continue  # Filtering criteria excluded this file; move on to next one
            if change.filename in new_file_changes:
                # Getting here means that path renaming is in effect, and caused one
                # path to collide with another.  That's usually bad, but can be okay
                # under two circumstances:
                #   1) Sometimes people have a file named OLDFILE in old revisions of
                #      history, and they rename to NEWFILE, and would like to rewrite
                #      history so that all revisions refer to it as NEWFILE.  As such,
                #      we can allow a collision when (at least) one of the two paths
                #      is a deletion.  Note that if OLDFILE and NEWFILE are unrelated
                #      this also allows the rewrite to continue, which makes sense
                #      since OLDFILE is no longer in the way.
                #   2) If OLDFILE and NEWFILE are exactly equal, then writing them
                #      both to the same location poses no problem; we only need one
                #      file.  (This could come up if someone copied a file in some
                #      commit, then later either deleted the file or kept it exactly
                #      in sync with the original with any changes, and then decides
                #      they want to rewrite history to only have one of the two files)
                colliding_change = new_file_changes[change.filename]
                if change.type == b"D":
                    # We can just throw this one away and keep the other
                    continue
                elif change.type == b"M" and (
                    change.mode == colliding_change.mode
                    and change.blob_id == colliding_change.blob_id
                ):
                    # The two are identical, so we can throw this one away and keep other
                    continue
                elif new_file_changes[change.filename].type != b"D":
                    raise SystemExit(
                        _("File renaming caused colliding pathnames!\n")
                        + _("  Commit: {}\n").format(commit.original_id)
                        + _("  Filename: {}").format(change.filename)
                    )
            # Strip files that are too large
            if (
                self._args.max_blob_size
                and self._unpacked_size.get(change.blob_id, 0)
                > self._args.max_blob_size
            ):
                continue
            if (
                self._args.strip_blobs_with_ids
                and change.blob_id in self._args.strip_blobs_with_ids
            ):
                continue
            # Otherwise, record the change
            new_file_changes[change.filename] = change
        commit.file_changes = [v for k, v in sorted(new_file_changes.items())]

        # Find out which files were modified by the callbacks.  Such paths could
        # lead to sebsequent commits being empty (e.g. if removed a line containing
        # a password from every version of a file that had the password, and some
        # later commit did nothing more than remove that line)
        final_file_changes = set(commit.file_changes)
        differences = orig_file_changes.symmetric_difference(final_file_changes)
        self._files_tweaked.update(x.filename for x in differences)

        # Record ancestry graph
        parents, orig_parents = commit.parents, aux_info["orig_parents"]
        if self._args.state_branch:
            external_parents = parents
        else:
            external_parents = [p for p in parents if not isinstance(p, int)]
        self._graph.record_external_commits(external_parents)
        self._orig_graph.record_external_commits(external_parents)
        self._graph.add_commit_and_parents(commit.id, parents)
        self._orig_graph.add_commit_and_parents(commit.old_id, orig_parents)

        # Prune parents (due to pruning of empty commits) if relevant
        old_1st_parent = parents[0] if parents else None
        parents, new_1st_parent = self._trim_extra_parents(orig_parents, parents)
        commit.parents = parents

        # If parents were pruned, then we need our file changes to be relative
        # to the new first parent
        if parents and old_1st_parent != parents[0]:
            commit.file_changes = GitUtils.get_file_changes(
                self._repo_working_dir, ID_TO_HASH[parents[0]], commit.original_id
            )

        # Call the user-defined callback, if any
        if self._commit_callback:
            self._commit_callback(commit, self.callback_metadata(aux_info))

        # Now print the resulting commit, or if prunable skip it
        if not commit.dumped:
            if not self._prunable(
                commit, new_1st_parent, aux_info["had_file_changes"], orig_parents
            ):
                self._insert_into_stream(commit)
                self._record_remapping(commit, orig_parents)
            else:
                rewrite_to = new_1st_parent or commit.first_parent()
                commit.skip(new_id=rewrite_to)
                if self._args.state_branch:
                    alias = Alias(
                        commit.old_id or commit.id, rewrite_to or deleted_hash
                    )
                    self._insert_into_stream(alias)
                reset = Reset(commit.branch, rewrite_to or deleted_hash)
                self._insert_into_stream(reset)
                self._commit_renames[commit.original_id] = None

        # Show progress
        self._num_commits += 1
        if not self._args.quiet:
            self._progress_writer.show(self._parsed_message % self._num_commits)

    @staticmethod
    def _do_tag_rename(rename_pair, tagname):
        old, new = rename_pair.split(b":", 1)
        old, new = b"refs/tags/" + old, b"refs/tags/" + new
        if tagname.startswith(old):
            return tagname.replace(old, new, 1)
        return tagname

    def _tweak_tag(self, tag):
        # Tweak the tag message according to callbacks
        if self._message_callback:
            tag.message = self._message_callback(tag.message)

        # Tweak the tag name according to tag-name-related callbacks
        tag_prefix = b"refs/tags/"
        fullref = tag_prefix + tag.ref
        if self._args.tag_rename:
            fullref = RepoFilter._do_tag_rename(self._args.tag_rename, fullref)
        if self._refname_callback:
            fullref = self._refname_callback(fullref)
            if not fullref.startswith(tag_prefix):
                msg = "Error: fast-import requires tags to be in refs/tags/ namespace."
                msg += "\n       {} renamed to {}".format(tag_prefix + tag.ref, fullref)
                raise SystemExit(msg)
        tag.ref = fullref[len(tag_prefix) :]

        # Tweak the tagger according to callbacks
        if self._args.mailmap:
            tag.tagger_name, tag.tagger_email = self._args.mailmap.translate(
                tag.tagger_name, tag.tagger_email
            )
        if self._name_callback:
            tag.tagger_name = self._name_callback(tag.tagger_name)
        if self._email_callback:
            tag.tagger_email = self._email_callback(tag.tagger_email)

        # Call general purpose tag callback
        if self._tag_callback:
            self._tag_callback(tag, self.callback_metadata())

    def _tweak_reset(self, reset):
        if self._args.tag_rename:
            reset.ref = RepoFilter._do_tag_rename(self._args.tag_rename, reset.ref)
        if self._refname_callback:
            reset.ref = self._refname_callback(reset.ref)
        if self._reset_callback:
            self._reset_callback(reset, self.callback_metadata())

    def results_tmp_dir(self, create_if_missing=True):
        target_working_dir = self._args.target or b"."
        git_dir = GitUtils.determine_git_dir(target_working_dir)
        d = os.path.join(git_dir, b"filter-repo")
        if create_if_missing and not os.path.isdir(d):
            os.mkdir(d)
        return d

    def _load_marks_file(self, marks_basename):
        full_branch = "refs/heads/{}".format(self._args.state_branch)
        marks_file = os.path.join(self.results_tmp_dir(), marks_basename)
        working_dir = self._args.target or b"."
        cmd = ["git", "-C", working_dir, "show-ref", full_branch]
        contents = b""
        if subproc.call(cmd, stdout=subprocess.DEVNULL) == 0:
            cmd = [
                "git",
                "-C",
                working_dir,
                "show",
                "%s:%s" % (full_branch, decode(marks_basename)),
            ]
            try:
                contents = subproc.check_output(cmd)
            except subprocess.CalledProcessError as e:  # pragma: no cover
                raise SystemExit(
                    _("Failed loading %s from %s") % (decode(marks_basename), branch)
                )
        if contents:
            biggest_id = max(int(x.split()[0][1:]) for x in contents.splitlines())
            _IDS._next_id = max(_IDS._next_id, biggest_id + 1)
        with open(marks_file, "bw") as f:
            f.write(contents)
        return marks_file

    def _save_marks_files(self):
        basenames = [b"source-marks", b"target-marks"]
        working_dir = self._args.target or b"."

        # Check whether the branch exists
        parent = []
        full_branch = "refs/heads/{}".format(self._args.state_branch)
        cmd = ["git", "-C", working_dir, "show-ref", full_branch]
        if subproc.call(cmd, stdout=subprocess.DEVNULL) == 0:
            parent = ["-p", full_branch]

        # Run 'git hash-object $MARKS_FILE' for each marks file, save result
        blob_hashes = {}
        for marks_basename in basenames:
            marks_file = os.path.join(self.results_tmp_dir(), marks_basename)
            if not os.path.isfile(marks_file):  # pragma: no cover
                raise SystemExit(
                    _("Failed to find %s to save to %s")
                    % (marks_file, self._args.state_branch)
                )
            cmd = ["git", "-C", working_dir, "hash-object", "-w", marks_file]
            blob_hashes[marks_basename] = subproc.check_output(cmd).strip()

        # Run 'git mktree' to create a tree out of it
        p = subproc.Popen(
            ["git", "-C", working_dir, "mktree"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        for b in basenames:
            p.stdin.write(b"100644 blob %s\t%s\n" % (blob_hashes[b], b))
        p.stdin.close()
        p.wait()
        tree = p.stdout.read().strip()

        # Create the new commit
        cmd = [
            "git",
            "-C",
            working_dir,
            "commit-tree",
            "-m",
            "New mark files",
            tree,
        ] + parent
        commit = subproc.check_output(cmd).strip()
        subproc.call(["git", "-C", working_dir, "update-ref", full_branch, commit])

    def importer_only(self):
        self._run_sanity_checks()
        self._setup_output()

    def set_output(self, outputRepoFilter):
        assert outputRepoFilter._output

        # set_output implies this RepoFilter is doing exporting, though may not
        # be the only one.
        self._setup_input(use_done_feature=False)

        # Set our output management up to pipe to outputRepoFilter's locations
        self._managed_output = False
        self._output = outputRepoFilter._output
        self._import_pipes = outputRepoFilter._import_pipes

        # Handle sanity checks, though currently none needed for export-only cases
        self._run_sanity_checks()

    def _setup_input(self, use_done_feature):
        if self._args.stdin:
            self._input = sys.stdin.detach()
            sys.stdin = None  # Make sure no one tries to accidentally use it
            self._fe_orig = None
        else:
            skip_blobs = (
                self._blob_callback is None
                and self._args.replace_text is None
                and self._args.source == self._args.target
            )
            extra_flags = []
            if skip_blobs:
                extra_flags.append("--no-data")
                if self._args.max_blob_size:
                    self._unpacked_size, packed_size = GitUtils.get_blob_sizes()
            if use_done_feature:
                extra_flags.append("--use-done-feature")
            if write_marks:
                extra_flags.append(b"--mark-tags")
            if self._args.state_branch:
                assert write_marks
                source_marks_file = self._load_marks_file(b"source-marks")
                extra_flags.extend(
                    [
                        b"--export-marks=" + source_marks_file,
                        b"--import-marks=" + source_marks_file,
                    ]
                )
            if self._args.preserve_commit_encoding is not None:  # pragma: no cover
                reencode = "no" if self._args.preserve_commit_encoding else "yes"
                extra_flags.append("--reencode=" + reencode)
            location = ["-C", self._args.source] if self._args.source else []
            fep_cmd = (
                ["git"]
                + location
                + [
                    "fast-export",
                    "--show-original-ids",
                    "--signed-tags=strip",
                    "--tag-of-filtered-object=rewrite",
                    "--fake-missing-tagger",
                    "--reference-excluded-parents",
                ]
                + extra_flags
                + self._args.refs
            )
            self._fep = subproc.Popen(fep_cmd, bufsize=-1, stdout=subprocess.PIPE)
            self._input = self._fep.stdout
            if self._args.dry_run or self._args.debug:
                self._fe_orig = os.path.join(
                    self.results_tmp_dir(), b"fast-export.original"
                )
                output = open(self._fe_orig, "bw")
                self._input = InputFileBackup(self._input, output)
                if self._args.debug:
                    tmp = [decode(x) if isinstance(x, bytes) else x for x in fep_cmd]
                    print("[DEBUG] Running: {}".format(" ".join(tmp)))
                    print(
                        "  (saving a copy of the output at {})".format(
                            decode(self._fe_orig)
                        )
                    )

    def _setup_output(self):
        if not self._args.dry_run:
            location = ["-C", self._args.target] if self._args.target else []
            fip_cmd = ["git"] + location + ["fast-import", "--force", "--quiet"]
            if self._args.state_branch:
                target_marks_file = self._load_marks_file(b"target-marks")
                fip_cmd.extend(
                    [
                        b"--export-marks=" + target_marks_file,
                        b"--import-marks=" + target_marks_file,
                    ]
                )
            self._fip = subproc.Popen(
                fip_cmd, bufsize=-1, stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            self._import_pipes = (self._fip.stdin, self._fip.stdout)
        if self._args.dry_run or self._args.debug:
            self._fe_filt = os.path.join(
                self.results_tmp_dir(), b"fast-export.filtered"
            )
            self._output = open(self._fe_filt, "bw")
        else:
            self._output = self._fip.stdin
        if self._args.debug:
            self._output = DualFileWriter(self._fip.stdin, self._output)
            tmp = [decode(x) if isinstance(x, bytes) else x for x in fip_cmd]
            print("[DEBUG] Running: {}".format(" ".join(tmp)))
            print(
                "  (using the following file as input: {})".format(
                    decode(self._fe_filt)
                )
            )

    def _migrate_origin_to_heads(self):
        refs_to_migrate = set(
            x for x in self._orig_refs if x.startswith(b"refs/remotes/origin/")
        )
        if not refs_to_migrate:
            return
        if self._args.debug:
            print("[DEBUG] Migrating refs/remotes/origin/* -> refs/heads/*")
        target_working_dir = self._args.target or b"."
        p = subproc.Popen(
            "git update-ref --no-deref --stdin".split(),
            stdin=subprocess.PIPE,
            cwd=target_working_dir,
        )
        for ref in refs_to_migrate:
            if ref == b"refs/remotes/origin/HEAD":
                p.stdin.write(b"delete %s %s\n" % (ref, self._orig_refs[ref]))
                del self._orig_refs[ref]
                continue
            newref = ref.replace(b"refs/remotes/origin/", b"refs/heads/")
            if newref not in self._orig_refs:
                p.stdin.write(b"create %s %s\n" % (newref, self._orig_refs[ref]))
            p.stdin.write(b"delete %s %s\n" % (ref, self._orig_refs[ref]))
            self._orig_refs[newref] = self._orig_refs[ref]
            del self._orig_refs[ref]
        p.stdin.close()
        if p.wait():
            raise SystemExit(_("git update-ref failed; see above"))  # pragma: no cover

        # Now remove
        if self._args.debug:
            print("[DEBUG] Removing 'origin' remote (rewritten history will no ")
            print("        longer be related; consider re-pushing it elsewhere.")
        subproc.call("git remote rm origin".split(), cwd=target_working_dir)

    def _final_commands(self):
        self._finalize_handled = True
        self._done_callback and self._done_callback()

        if not self._args.quiet:
            self._progress_writer.finish()

    def _ref_update(self, target_working_dir):
        # Start the update-ref process
        p = subproc.Popen(
            "git update-ref --no-deref --stdin".split(),
            stdin=subprocess.PIPE,
            cwd=target_working_dir,
        )

        # Remove replace_refs from _orig_refs
        replace_refs = {
            k: v for k, v in self._orig_refs.items() if k.startswith(b"refs/replace/")
        }
        reverse_replace_refs = collections.defaultdict(list)
        for k, v in replace_refs.items():
            reverse_replace_refs[v].append(k)
        all(map(self._orig_refs.pop, replace_refs))

        # Remove unused refs
        exported_refs, imported_refs = self.get_exported_and_imported_refs()
        refs_to_nuke = exported_refs - imported_refs
        if self._args.partial:
            refs_to_nuke = set()
        if refs_to_nuke and self._args.debug:
            print(
                "[DEBUG] Deleting the following refs:\n  "
                + decode(b"\n  ".join(refs_to_nuke))
            )
        p.stdin.write(b"".join([b"delete %s\n" % x for x in refs_to_nuke]))

        # Delete or update and add replace_refs; note that fast-export automatically
        # handles 'update-no-add', we only need to take action for the other four
        # choices for replace_refs.
        self._flush_renames()
        actual_renames = {k: v for k, v in self._commit_renames.items() if k != v}
        if self._args.replace_refs in ["delete-no-add", "delete-and-add"]:
            # Delete old replace refs, if unwanted
            replace_refs_to_nuke = set(replace_refs)
            if self._args.replace_refs == "delete-and-add":
                # git-update-ref won't allow us to update a ref twice, so be careful
                # to avoid deleting refs we'll later update
                replace_refs_to_nuke = replace_refs_to_nuke.difference(
                    [b"refs/replace/" + x for x in actual_renames]
                )
            p.stdin.write(b"".join([b"delete %s\n" % x for x in replace_refs_to_nuke]))
        if self._args.replace_refs in [
            "delete-and-add",
            "update-or-add",
            "update-and-add",
        ]:
            # Add new replace refs
            update_only = self._args.replace_refs == "update-or-add"
            p.stdin.write(
                b"".join(
                    [
                        b"update refs/replace/%s %s\n" % (old, new)
                        for old, new in actual_renames.items()
                        if new and not (update_only and old in reverse_replace_refs)
                    ]
                )
            )

        # Complete the update-ref process
        p.stdin.close()
        if p.wait():
            raise SystemExit(_("git update-ref failed; see above"))  # pragma: no cover

    def _record_metadata(self, metadata_dir, orig_refs):
        self._flush_renames()
        with open(os.path.join(metadata_dir, b"commit-map"), "bw") as f:
            f.write(("%-40s %s\n" % (_("old"), _("new"))).encode())
            for (old, new) in self._commit_renames.items():
                msg = b"%s %s\n" % (old, new if new != None else deleted_hash)
                f.write(msg)

        exported_refs, imported_refs = self.get_exported_and_imported_refs()

        batch_check_process = None
        batch_check_output_re = re.compile(b"^([0-9a-f]{40}) ([a-z]+) ([0-9]+)$")
        with open(os.path.join(metadata_dir, b"ref-map"), "bw") as f:
            for refname, old_hash in orig_refs.items():
                if refname not in exported_refs:
                    continue
                if refname not in imported_refs:
                    new_hash = deleted_hash
                elif old_hash in self._commit_renames:
                    new_hash = self._commit_renames[old_hash]
                    new_hash = new_hash if new_hash != None else deleted_hash
                else:  # Must be either an annotated tag, or a ref whose tip was pruned
                    if not batch_check_process:
                        cmd = "git cat-file --batch-check".split()
                        target_working_dir = self._args.target or b"."
                        batch_check_process = subproc.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            cwd=target_working_dir,
                        )
                    batch_check_process.stdin.write(refname + b"\n")
                    batch_check_process.stdin.flush()
                    line = batch_check_process.stdout.readline()
                    m = batch_check_output_re.match(line)
                    if m and m.group(2) in (b"tag", b"commit"):
                        new_hash = m.group(1)
                    elif line.endswith(b" missing\n"):
                        new_hash = deleted_hash
                    else:
                        raise SystemExit(
                            _(
                                "Failed to find new id for %(refname)s "
                                "(old id was %(old_hash)s)"
                            )
                            % ({"refname": refname, "old_hash": old_hash})
                        )  # pragma: no cover
                f.write(b"%s %s %s\n" % (old_hash, new_hash, refname))
            if self._args.source or self._args.target:
                new_refs = GitUtils.get_refs(self._args.target or b".")
                for ref, new_hash in new_refs.items():
                    if ref not in orig_refs and not ref.startswith(b"refs/replace/"):
                        old_hash = b"0" * len(new_hash)
                        f.write(b"%s %s %s\n" % (old_hash, new_hash, ref))
        if batch_check_process:
            batch_check_process.stdin.close()
            batch_check_process.wait()

        with open(os.path.join(metadata_dir, b"suboptimal-issues"), "bw") as f:
            issues_found = False
            if self._commits_no_longer_merges:
                issues_found = True

                f.write(
                    textwrap.dedent(
                        _(
                            """
          The following commits used to be merge commits but due to filtering
          are now regular commits; they likely have suboptimal commit messages
          (e.g. "Merge branch next into master").  Original commit hash on the
          left, commit hash after filtering/rewriting on the right:
          """
                        )[1:]
                    ).encode()
                )
                for oldhash, newhash in self._commits_no_longer_merges:
                    f.write("  {} {}\n".format(oldhash, newhash).encode())
                f.write(b"\n")

            if self._commits_referenced_but_removed:
                issues_found = True
                f.write(
                    textwrap.dedent(
                        _(
                            """
          The following commits were filtered out, but referenced in another
          commit message.  The reference to the now-nonexistent commit hash
          (or a substring thereof) was left as-is in any commit messages:
          """
                        )[1:]
                    ).encode()
                )
                for bad_commit_reference in self._commits_referenced_but_removed:
                    f.write("  {}\n".format(bad_commit_reference).encode())
                f.write(b"\n")

            if not issues_found:
                f.write(_("No filtering problems encountered.\n").encode())

        with open(os.path.join(metadata_dir, b"already_ran"), "bw") as f:
            f.write(
                _(
                    "This file exists to allow you to filter again without --force.\n"
                ).encode()
            )

    def finish(self):
        """ Alternative to run() when there is no input of our own to parse,
        meaning that run only really needs to close the handle to fast-import
        and let it finish, thus making a call to "run" feel like a misnomer. """
        assert not self._input
        assert self._managed_output
        self.run()

    def insert(self, obj, direct_insertion=False):
        if not direct_insertion:
            if type(obj) == Blob:
                self._tweak_blob(obj)
            elif type(obj) == Commit:
                aux_info = {
                    "orig_parents": obj.parents,
                    "had_file_changes": bool(obj.file_changes),
                }
                self._tweak_commit(obj, aux_info)
            elif type(obj) == Reset:
                self._tweak_reset(obj)
            elif type(obj) == Tag:
                self._tweak_tag(obj)
        self._insert_into_stream(obj)

    def _insert_into_stream(self, obj):
        if not obj.dumped:
            if self._parser:
                self._parser.insert(obj)
            else:
                obj.dump(self._output)

    def get_exported_and_imported_refs(self):
        return self._parser.get_exported_and_imported_refs()

    def run(self):
        start = time.time()
        if not self._input and not self._output:
            self._run_sanity_checks()
            if not self._args.dry_run and not self._args.partial:
                self._migrate_origin_to_heads()
            self._setup_input(use_done_feature=True)
            self._setup_output()
        assert self._sanity_checks_handled

        if self._input:
            # Create and run the filter
            self._repo_working_dir = self._args.source or b"."
            self._parser = FastExportParser(
                blob_callback=self._tweak_blob,
                commit_callback=self._tweak_commit,
                tag_callback=self._tweak_tag,
                reset_callback=self._tweak_reset,
                done_callback=self._final_commands,
            )
            self._parser.run(self._input, self._output)
            if not self._finalize_handled:
                self._final_commands()

            # Make sure fast-export completed successfully
            if not self._args.stdin and self._fep.wait():
                raise SystemExit(
                    _("Error: fast-export failed; see above.")
                )  # pragma: no cover

        # If we're not the manager of self._output, we should avoid post-run cleanup
        if not self._managed_output:
            return

        # Close the output and ensure fast-import successfully completes
        self._output.close()
        if not self._args.dry_run and self._fip.wait():
            raise SystemExit(
                _("Error: fast-import failed; see above.")
            )  # pragma: no cover

        # With fast-export and fast-import complete, update state if requested
        if self._args.state_branch:
            self._save_marks_files()

        # Notify user how long it took, before doing a gc and such
        repack = not self._args.partial
        msg = "New history written in {:.2f} seconds..."
        if repack:
            msg = "New history written in {:.2f} seconds; now repacking/cleaning..."
        print(msg.format(time.time() - start))

        # Exit early, if requested
        if self._args.dry_run:
            print(_("NOTE: Not running fast-import or cleaning up; --dry-run passed."))
            if self._fe_orig:
                print(_("      Requested filtering can be seen by comparing:"))
                print("        " + decode(self._fe_orig))
            else:
                print(_("      Requested filtering can be seen at:"))
            print("        " + decode(self._fe_filt))
            return

        target_working_dir = self._args.target or b"."
        if self._input:
            self._ref_update(target_working_dir)

            # Write out data about run
            self._record_metadata(self.results_tmp_dir(), self._orig_refs)

        # If repack, then nuke the reflogs and repack.  If reset, do a reset --hard
        reset = not GitUtils.is_repository_bare(target_working_dir)
        RepoFilter.cleanup(
            target_working_dir,
            repack,
            reset,
            run_quietly=self._args.quiet,
            show_debuginfo=self._args.debug,
        )

        # Let user know how long it took
        print(
            _("Completely finished after {:.2f} seconds.").format(time.time() - start)
        )


