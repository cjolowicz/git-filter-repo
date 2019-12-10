import collections
import os
import subprocess
import sys
import textwrap

from .ancestrygraph import AncestryGraph
from .gettext import _
from .gitutils import GitUtils
from .pathquoting import PathQuoting
from .progress import ProgressWriter
from .subprocess import subproc


class RepoAnalyze(object):

    # First, several helper functions for analyze_commit()

    @staticmethod
    def equiv_class(stats, filename):
        return stats["equivalence"].get(filename, (filename,))

    @staticmethod
    def setup_equivalence_for_rename(stats, oldname, newname):
        # if A is renamed to B and B is renamed to C, then the user thinks of
        # A, B, and C as all being different names for the same 'file'.  We record
        # this as an equivalence class:
        #   stats['equivalence'][name] = (A,B,C)
        # for name being each of A, B, and C.
        old_tuple = stats["equivalence"].get(oldname, ())
        if newname in old_tuple:
            return
        elif old_tuple:
            new_tuple = tuple(list(old_tuple) + [newname])
        else:
            new_tuple = (oldname, newname)
        for f in new_tuple:
            stats["equivalence"][f] = new_tuple

    @staticmethod
    def setup_or_update_rename_history(stats, commit, oldname, newname):
        rename_commits = stats["rename_history"].get(oldname, set())
        rename_commits.add(commit)
        stats["rename_history"][oldname] = rename_commits

    @staticmethod
    def handle_renames(stats, commit, change_types, filenames):
        for index, change_type in enumerate(change_types):
            if change_type == ord(b"R"):
                oldname, newname = filenames[index], filenames[-1]
                RepoAnalyze.setup_equivalence_for_rename(stats, oldname, newname)
                RepoAnalyze.setup_or_update_rename_history(
                    stats, commit, oldname, newname
                )

    @staticmethod
    def handle_file(stats, graph, commit, modes, shas, filenames):
        mode, sha, filename = modes[-1], shas[-1], filenames[-1]

        # Figure out kind of deletions to undo for this file, and update lists
        # of all-names-by-sha and all-filenames
        delmode = "tree_deletions"
        if mode != b"040000":
            delmode = "file_deletions"
            stats["names"][sha].add(filename)
            stats["allnames"].add(filename)

        # If the file (or equivalence class of files) was recorded as deleted,
        # clearly it isn't anymore
        equiv = RepoAnalyze.equiv_class(stats, filename)
        for f in equiv:
            stats[delmode].pop(f, None)

        # If we get a modify/add for a path that was renamed, we may need to break
        # the equivalence class.  However, if the modify/add was on a branch that
        # doesn't have the rename in its history, we are still okay.
        need_to_break_equivalence = False
        if equiv[-1] != filename:
            for rename_commit in stats["rename_history"][filename]:
                if graph.is_ancestor(rename_commit, commit):
                    need_to_break_equivalence = True

        if need_to_break_equivalence:
            for f in equiv:
                if f in stats["equivalence"]:
                    del stats["equivalence"][f]

    @staticmethod
    def analyze_commit(stats, graph, commit, parents, date, file_changes):
        graph.add_commit_and_parents(commit, parents)
        for change in file_changes:
            modes, shas, change_types, filenames = change
            if len(parents) == 1 and change_types.startswith(b"R"):
                change_types = b"R"  # remove the rename score; we don't care
            if modes[-1] == b"160000":
                continue
            elif modes[-1] == b"000000":
                # Track when files/directories are deleted
                for f in RepoAnalyze.equiv_class(stats, filenames[-1]):
                    if any(x == b"040000" for x in modes[0:-1]):
                        stats["tree_deletions"][f] = date
                    else:
                        stats["file_deletions"][f] = date
            elif change_types.strip(b"AMT") == b"":
                RepoAnalyze.handle_file(stats, graph, commit, modes, shas, filenames)
            elif modes[-1] == b"040000" and change_types.strip(b"RAM") == b"":
                RepoAnalyze.handle_file(stats, graph, commit, modes, shas, filenames)
            elif change_types.strip(b"RAM") == b"":
                RepoAnalyze.handle_file(stats, graph, commit, modes, shas, filenames)
                RepoAnalyze.handle_renames(stats, commit, change_types, filenames)
            else:
                raise SystemExit(
                    _(
                        "Unhandled change type(s): %(change_type)s "
                        "(in commit %(commit)s)"
                    )
                    % ({"change_type": change_types, "commit": commit})
                )  # pragma: no cover

    @staticmethod
    def gather_data(args):
        unpacked_size, packed_size = GitUtils.get_blob_sizes()
        stats = {
            "names": collections.defaultdict(set),
            "allnames": set(),
            "file_deletions": {},
            "tree_deletions": {},
            "equivalence": {},
            "rename_history": collections.defaultdict(set),
            "unpacked_size": unpacked_size,
            "packed_size": packed_size,
            "num_commits": 0,
        }

        # Setup the rev-list/diff-tree process
        commit_parse_progress = ProgressWriter()
        num_commits = 0
        cmd = (
            "git rev-list --topo-order --reverse {}".format(" ".join(args.refs))
            + " | git diff-tree --stdin --always --root --format=%H%n%P%n%cd"
            + " --date=short -M -t -c --raw --combined-all-paths"
        )
        dtp = subproc.Popen(cmd, shell=True, bufsize=-1, stdout=subprocess.PIPE)
        f = dtp.stdout
        line = f.readline()
        if not line:
            raise SystemExit(_("Nothing to analyze; repository is empty."))
        cont = bool(line)
        graph = AncestryGraph()
        while cont:
            commit = line.rstrip()
            parents = f.readline().split()
            date = f.readline().rstrip()

            # We expect a blank line next; if we get a non-blank line then
            # this commit modified no files and we need to move on to the next.
            # If there is no line, we've reached end-of-input.
            line = f.readline()
            if not line:
                cont = False
            line = line.rstrip()

            # If we haven't reached end of input, and we got a blank line meaning
            # a commit that has modified files, then get the file changes associated
            # with this commit.
            file_changes = []
            if cont and not line:
                cont = False
                for line in f:
                    if not line.startswith(b":"):
                        cont = True
                        break
                    n = 1 + max(1, len(parents))
                    assert line.startswith(b":" * (n - 1))
                    relevant = line[n - 1 : -1]
                    splits = relevant.split(None, n)
                    modes = splits[0:n]
                    splits = splits[n].split(None, n)
                    shas = splits[0:n]
                    splits = splits[n].split(b"\t")
                    change_types = splits[0]
                    filenames = [PathQuoting.dequote(x) for x in splits[1:]]
                    file_changes.append([modes, shas, change_types, filenames])

            # Analyze this commit and update progress
            RepoAnalyze.analyze_commit(
                stats, graph, commit, parents, date, file_changes
            )
            num_commits += 1
            commit_parse_progress.show(_("Processed %d commits") % num_commits)

        # Show the final commits processed message and record the number of commits
        commit_parse_progress.finish()
        stats["num_commits"] = num_commits

        # Close the output, ensure rev-list|diff-tree pipeline completed successfully
        dtp.stdout.close()
        if dtp.wait():
            raise SystemExit(
                _("Error: rev-list|diff-tree pipeline failed; see above.")
            )  # pragma: no cover

        return stats

    @staticmethod
    def write_report(reportdir, stats):
        def datestr(datetimestr):
            return datetimestr if datetimestr else _("<present>").encode()

        def dirnames(path):
            while True:
                path = os.path.dirname(path)
                yield path
                if path == b"":
                    break

        # Compute aggregate size information for paths, extensions, and dirs
        total_size = {"packed": 0, "unpacked": 0}
        path_size = {
            "packed": collections.defaultdict(int),
            "unpacked": collections.defaultdict(int),
        }
        ext_size = {
            "packed": collections.defaultdict(int),
            "unpacked": collections.defaultdict(int),
        }
        dir_size = {
            "packed": collections.defaultdict(int),
            "unpacked": collections.defaultdict(int),
        }
        for sha in stats["names"]:
            size = {
                "packed": stats["packed_size"][sha],
                "unpacked": stats["unpacked_size"][sha],
            }
            for which in ("packed", "unpacked"):
                for name in stats["names"][sha]:
                    total_size[which] += size[which]
                    path_size[which][name] += size[which]
                    basename, ext = os.path.splitext(name)
                    ext_size[which][ext] += size[which]
                    for dirname in dirnames(name):
                        dir_size[which][dirname] += size[which]

        # Determine if and when extensions and directories were deleted
        ext_deleted_data = {}
        for name in stats["allnames"]:
            when = stats["file_deletions"].get(name, None)

            # Update the extension
            basename, ext = os.path.splitext(name)
            if when is None:
                ext_deleted_data[ext] = None
            elif ext in ext_deleted_data:
                if ext_deleted_data[ext] is not None:
                    ext_deleted_data[ext] = max(ext_deleted_data[ext], when)
            else:
                ext_deleted_data[ext] = when

        dir_deleted_data = {}
        for name in dir_size["packed"]:
            dir_deleted_data[name] = stats["tree_deletions"].get(name, None)

        with open(os.path.join(reportdir, b"README"), "bw") as f:
            # Give a basic overview of this file
            f.write(b"== %s ==\n" % _("Overall Statistics").encode())
            f.write(
                ("  %s: %d\n" % (_("Number of commits"), stats["num_commits"])).encode()
            )
            f.write(
                (
                    "  %s: %d\n" % (_("Number of filenames"), len(path_size["packed"]))
                ).encode()
            )
            f.write(
                (
                    "  %s: %d\n" % (_("Number of directories"), len(dir_size["packed"]))
                ).encode()
            )
            f.write(
                (
                    "  %s: %d\n"
                    % (_("Number of file extensions"), len(ext_size["packed"]))
                ).encode()
            )
            f.write(b"\n")
            f.write(
                (
                    "  %s: %d\n"
                    % (_("Total unpacked size (bytes)"), total_size["unpacked"])
                ).encode()
            )
            f.write(
                (
                    "  %s: %d\n"
                    % (_("Total packed size (bytes)"), total_size["packed"])
                ).encode()
            )
            f.write(b"\n")

            # Mention issues with the report
            f.write(("== %s ==\n" % _("Caveats")).encode())
            f.write(("=== %s ===\n" % _("Sizes")).encode())
            f.write(
                textwrap.dedent(
                    _(
                        """
        Packed size represents what size your repository would be if no
        trees, commits, tags, or other metadata were included (though it may
        fail to represent de-duplication; see below).  It also represents the
        current packing, which may be suboptimal if you haven't gc'ed for a
        while.

        Unpacked size represents what size your repository would be if no
        trees, commits, tags, or other metadata were included AND if no
        files were packed; i.e., without delta-ing or compression.

        Both unpacked and packed sizes can be slightly misleading.  Deleting
        a blob from history not save as much space as the unpacked size,
        because it is obviously normally stored in packed form.  Also,
        deleting a blob from history may not save as much space as its packed
        size either, because another blob could be stored as a delta against
        that blob, so when you remove one blob another blob's packed size may
        grow.

        Also, the sum of the packed sizes can add up to more than the
        repository size; if the same contents appeared in the repository in
        multiple places, git will automatically de-dupe and store only one
        copy, while the way sizes are added in this analysis adds the size
        for each file path that has those contents.  Further, if a file is
        ever reverted to a previous version's contents, the previous
        version's size will be counted multiple times in this analysis, even
        though git will only store it once.
        """
                    )[1:]
                ).encode()
            )
            f.write(b"\n")
            f.write(("=== %s ===\n" % _("Deletions")).encode())
            f.write(
                textwrap.dedent(
                    _(
                        """
        Whether a file is deleted is not a binary quality, since it can be
        deleted on some branches but still exist in others.  Also, it might
        exist in an old tag, but have been deleted in versions newer than
        that.  More thorough tracking could be done, including looking at
        merge commits where one side of history deleted and the other modified,
        in order to give a more holistic picture of deletions.  However, that
        algorithm would not only be more complex to implement, it'd also be
        quite difficult to present and interpret by users.  Since --analyze
        is just about getting a high-level rough picture of history, it instead
        implements the simplistic rule that is good enough for 98% of cases:
          A file is marked as deleted if the last commit in the fast-export
          stream that mentions the file lists it as deleted.
        This makes it dependent on topological ordering, but generally gives
        the "right" answer.
        """
                    )[1:]
                ).encode()
            )
            f.write(b"\n")
            f.write(("=== %s ===\n" % _("Renames")).encode())
            f.write(
                textwrap.dedent(
                    _(
                        """
        Renames share the same non-binary nature that deletions do, plus
        additional challenges:
          * If the renamed file is renamed again, instead of just two names for
            a path you can have three or more.
          * Rename pairs of the form (oldname, newname) that we consider to be
            different names of the "same file" might only be valid over certain
            commit ranges.  For example, if a new commit reintroduces a file
            named oldname, then new versions of oldname aren't the "same file"
            anymore.  We could try to portray this to the user, but it's easier
            for the user to just break the pairing and only report unbroken
            rename pairings to the user.
          * The ability for users to rename files differently in different
            branches means that our chains of renames will not necessarily be
            linear but may branch out.
        """
                    )[1:]
                ).encode()
            )
            f.write(b"\n")

        # Equivalence classes for names, so if folks only want to keep a
        # certain set of paths, they know the old names they want to include
        # too.
        with open(os.path.join(reportdir, b"renames.txt"), "bw") as f:
            seen = set()
            for pathname, equiv_group in sorted(
                stats["equivalence"].items(), key=lambda x: (x[1], x[0])
            ):
                if equiv_group in seen:
                    continue
                seen.add(equiv_group)
                f.write(
                    (
                        "{} ->\n    ".format(decode(equiv_group[0]))
                        + "\n    ".join(decode(x) for x in equiv_group[1:])
                        + "\n"
                    ).encode()
                )

        # List directories in reverse sorted order of unpacked size
        with open(os.path.join(reportdir, b"directories-deleted-sizes.txt"), "bw") as f:
            msg = "=== %s ===\n" % _("Deleted directories by reverse size")
            f.write(msg.encode())
            msg = _(
                "Format: unpacked size, packed size, date deleted, directory name\n"
            )
            f.write(msg.encode())
            for dirname, size in sorted(
                dir_size["packed"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                if dir_deleted_data[dirname]:
                    f.write(
                        b"  %10d %10d %-10s %s\n"
                        % (
                            dir_size["unpacked"][dirname],
                            size,
                            datestr(dir_deleted_data[dirname]),
                            dirname or _("<toplevel>").encode(),
                        )
                    )

        with open(os.path.join(reportdir, b"directories-all-sizes.txt"), "bw") as f:
            f.write(("=== %s ===\n" % _("All directories by reverse size")).encode())
            msg = _(
                "Format: unpacked size, packed size, date deleted, directory name\n"
            )
            f.write(msg.encode())
            for dirname, size in sorted(
                dir_size["packed"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                f.write(
                    b"  %10d %10d %-10s %s\n"
                    % (
                        dir_size["unpacked"][dirname],
                        size,
                        datestr(dir_deleted_data[dirname]),
                        dirname or _("<toplevel>").encode(),
                    )
                )

        # List extensions in reverse sorted order of unpacked size
        with open(os.path.join(reportdir, b"extensions-deleted-sizes.txt"), "bw") as f:
            msg = "=== %s ===\n" % _("Deleted extensions by reverse size")
            f.write(msg.encode())
            msg = _(
                "Format: unpacked size, packed size, date deleted, extension name\n"
            )
            f.write(msg.encode())
            for extname, size in sorted(
                ext_size["packed"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                if ext_deleted_data[extname]:
                    f.write(
                        b"  %10d %10d %-10s %s\n"
                        % (
                            ext_size["unpacked"][extname],
                            size,
                            datestr(ext_deleted_data[extname]),
                            extname or _("<no extension>").encode(),
                        )
                    )

        with open(os.path.join(reportdir, b"extensions-all-sizes.txt"), "bw") as f:
            f.write(("=== %s ===\n" % _("All extensions by reverse size")).encode())
            msg = _(
                "Format: unpacked size, packed size, date deleted, extension name\n"
            )
            f.write(msg.encode())
            for extname, size in sorted(
                ext_size["packed"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                f.write(
                    b"  %10d %10d %-10s %s\n"
                    % (
                        ext_size["unpacked"][extname],
                        size,
                        datestr(ext_deleted_data[extname]),
                        extname or _("<no extension>").encode(),
                    )
                )

        # List files in reverse sorted order of unpacked size
        with open(os.path.join(reportdir, b"path-deleted-sizes.txt"), "bw") as f:
            msg = "=== %s ===\n" % _("Deleted paths by reverse accumulated size")
            f.write(msg.encode())
            msg = _("Format: unpacked size, packed size, date deleted, path name(s)\n")
            f.write(msg.encode())
            for pathname, size in sorted(
                path_size["packed"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                when = stats["file_deletions"].get(pathname, None)
                if when:
                    f.write(
                        b"  %10d %10d %-10s %s\n"
                        % (
                            path_size["unpacked"][pathname],
                            size,
                            datestr(when),
                            pathname,
                        )
                    )

        with open(os.path.join(reportdir, b"path-all-sizes.txt"), "bw") as f:
            msg = "=== %s ===\n" % _("All paths by reverse accumulated size")
            f.write(msg.encode())
            msg = _(
                "Format: unpacked size, packed size, date deleted, pathectory name\n"
            )
            f.write(msg.encode())
            for pathname, size in sorted(
                path_size["packed"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                when = stats["file_deletions"].get(pathname, None)
                f.write(
                    b"  %10d %10d %-10s %s\n"
                    % (path_size["unpacked"][pathname], size, datestr(when), pathname)
                )

        # List of filenames and sizes in descending order
        with open(os.path.join(reportdir, b"blob-shas-and-paths.txt"), "bw") as f:
            f.write(
                (
                    "=== %s ===\n"
                    % _("Files by sha and associated pathnames in reverse size")
                ).encode()
            )
            f.write(
                _(
                    "Format: sha, unpacked size, packed size, filename(s) object stored as\n"
                ).encode()
            )
            for sha, size in sorted(
                stats["packed_size"].items(), key=lambda x: (x[1], x[0]), reverse=True
            ):
                if sha not in stats["names"]:
                    # Some objects in the repository might not be referenced, or not
                    # referenced by the branches/tags the user cares about; skip them.
                    continue
                names_with_sha = stats["names"][sha]
                if len(names_with_sha) == 1:
                    names_with_sha = names_with_sha.pop()
                else:
                    names_with_sha = b"[" + b", ".join(sorted(names_with_sha)) + b"]"
                f.write(
                    b"  %s %10d %10d %s\n"
                    % (sha, stats["unpacked_size"][sha], size, names_with_sha)
                )

    @staticmethod
    def run(args):
        git_dir = GitUtils.determine_git_dir(b".")

        # Create the report directory as necessary
        results_tmp_dir = os.path.join(git_dir, b"filter-repo")
        if not os.path.isdir(results_tmp_dir):
            os.mkdir(results_tmp_dir)
        reportdir = os.path.join(results_tmp_dir, b"analysis")
        if not args.force and os.path.isdir(reportdir):
            shutil.rmtree(reportdir)
        os.mkdir(reportdir)

        # Gather the data we need
        stats = RepoAnalyze.gather_data(args)

        # Write the reports
        sys.stdout.write(_("Writing reports to %s...") % decode(reportdir))
        sys.stdout.flush()
        RepoAnalyze.write_report(reportdir, stats)
        sys.stdout.write(_("done.\n"))
