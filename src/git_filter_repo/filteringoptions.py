import argparse
import fnmatch
import os
import re
import subprocess

from .subprocess import subproc
from .utils import decode


def glob_to_regex(glob_bytestr):
    "Translate glob_bytestr into a regex on bytestrings"

    # fnmatch.translate is idiotic and won't accept bytestrings
    if decode(glob_bytestr).encode() != glob_bytestr:  # pragma: no cover
        raise SystemExit(_("Error: Cannot handle glob %s").format(glob_bytestr))

    # Create regex operating on string
    regex = fnmatch.translate(decode(glob_bytestr))

    # FIXME: This is an ugly hack...
    # fnmatch.translate tries to do multi-line matching and wants the glob to
    # match up to the end of the input, which isn't relevant for us, so we
    # have to modify the regex.  fnmatch.translate has used different regex
    # constructs to achieve this with different python versions, so we have
    # to check for each of them and then fix it up.  It would be much better
    # if fnmatch.translate could just take some flags to allow us to specify
    # what we want rather than employing this hackery, but since it
    # doesn't...
    if regex.endswith(r"\Z(?ms)"):  # pragma: no cover
        regex = regex[0:-7]
    elif regex.startswith(r"(?s:") and regex.endswith(r")\Z"):  # pragma: no cover
        regex = regex[4:-3]

    # Finally, convert back to regex operating on bytestr
    return regex.encode()


class FilteringOptions(object):
    class AppendFilter(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            suffix = option_string[len("--path-") :] or "match"
            if suffix.startswith("rename"):
                mod_type = "rename"
                match_type = option_string[len("--path-rename-") :] or "match"
                values = values.split(b":", 1)
                if (
                    values[0]
                    and values[1]
                    and not (values[0].endswith(b"/") == values[1].endswith(b"/"))
                ):
                    raise SystemExit(
                        _(
                            "Error: With --path-rename, if OLD_NAME and "
                            "NEW_NAME are both non-empty and either ends "
                            "with a slash then both must."
                        )
                    )
            else:
                mod_type = "filter"
                match_type = suffix
            if match_type == "regex":
                values = re.compile(values)
            items = getattr(namespace, self.dest, []) or []
            items.append((mod_type, match_type, values))
            setattr(namespace, self.dest, items)

    class HelperFilter(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            af = FilteringOptions.AppendFilter(dest="path_changes", option_strings=None)
            dirname = values if values[-1] == b"/" else values + b"/"
            if option_string == "--subdirectory-filter":
                af(parser, namespace, dirname, "--path-match")
                af(parser, namespace, dirname + b":", "--path-rename")
            elif option_string == "--to-subdirectory-filter":
                af(parser, namespace, b":" + dirname, "--path-rename")
            else:
                raise SystemExit(
                    _("Error: HelperFilter given invalid option_string: %s")
                    % option_string
                )  # pragma: no cover

    class FileWithPathsFilter(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            if not namespace.path_changes:
                namespace.path_changes = []
            namespace.path_changes += FilteringOptions.get_paths_from_file(values)

    @staticmethod
    def create_arg_parser():
        # Include usage in the summary, so we can put the description first
        summary = _(
            """Rewrite (or analyze) repository history

    git-filter-repo destructively rewrites history (unless --analyze or
    --dry-run are given) according to specified rules.  It refuses to do any
    rewriting unless either run from a clean fresh clone, or --force was
    given.

    Basic Usage:
      git-filter-repo --analyze
      git-filter-repo [FILTER/RENAME/CONTROL OPTIONS]

    See EXAMPLES section for details.
    """
        ).rstrip()

        # Provide a long helpful examples section
        example_text = _(
            """CALLBACKS

    All callback functions are of the same general format.  For a command line
    argument like
      --foo-callback 'BODY'

    the following code will be compiled and called:
      def foo_callback(foo):
        BODY

    Thus, to replace 'Jon' with 'John' in author/committer/tagger names:
      git filter-repo --name-callback 'return name.replace(b"Jon", b"John")'

    To remove all 'Tested-by' tags in commit (or tag) messages:
      git filter-repo --message-callback 'return re.sub(br"\\nTested-by:.*", "", message)'

    To remove all .DS_Store files:
      git filter-repo --filename-callback 'return None if os.path.basename(filename) == b".DS_Store" else filename'

    For more detailed examples and explanations AND caveats, see
      https://github.com/newren/git-filter-repo#callbacks

EXAMPLES

    To get a bunch of reports mentioning renames that have occurred in
    your repo and listing sizes of objects aggregated by any of path,
    directory, extension, or blob-id:
      git filter-repo --analyze

    (These reports can help you choose how to filter your repo; it can
    be useful to re-run this command after filtering to regenerate the
    report and verify the changes look correct.)

    To extract the history that touched just 'guides' and 'tools/releases':
      git filter-repo --path guides/ --path tools/releases

    To remove foo.zip and bar/baz/zips from every revision in history:
      git filter-repo --path foo.zip --path bar/baz/zips/ --invert-paths

    To replace the text 'password' with 'p455w0rd':
      git filter-repo --replace-text <(echo "password==>p455w0rd")

    To use the current version of the .mailmap file to update authors,
    committers, and taggers throughout history and make it permanent:
      git filter-repo --use-mailmap

    To extract the history of 'src/', rename all files to have a new leading
    directory 'my-module' (e.g. src/foo.java -> my-module/src/foo.java), and
    add a 'my-module-' prefix to all tags:
      git filter-repo --path src/ --to-subdirectory-filter my-module --tag-rename '':'my-module-'

    For more detailed examples and explanations, see
      https://github.com/newren/git-filter-repo#examples"""
        )

        # Create the basic parser
        parser = argparse.ArgumentParser(
            description=summary,
            usage=argparse.SUPPRESS,
            add_help=False,
            epilog=example_text,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

        analyze = parser.add_argument_group(title=_("Analysis"))
        analyze.add_argument(
            "--analyze",
            action="store_true",
            help=_(
                "Analyze repository history and create a report that may be "
                "useful in determining what to filter in a subsequent run. "
                "Will not modify your repo."
            ),
        )

        path = parser.add_argument_group(
            title=_("Filtering based on paths " "(see also --filename-callback)")
        )
        path.add_argument(
            "--invert-paths",
            action="store_false",
            dest="inclusive",
            help=_(
                "Invert the selection of files from the specified "
                "--path-{match,glob,regex} options below, i.e. only select "
                "files matching none of those options."
            ),
        )

        path.add_argument(
            "--path-match",
            "--path",
            metavar="DIR_OR_FILE",
            type=os.fsencode,
            action=FilteringOptions.AppendFilter,
            dest="path_changes",
            help=_(
                "Exact paths (files or directories) to include in filtered "
                "history.  Multiple --path options can be specified to get "
                "a union of paths."
            ),
        )
        path.add_argument(
            "--path-glob",
            metavar="GLOB",
            type=os.fsencode,
            action=FilteringOptions.AppendFilter,
            dest="path_changes",
            help=_(
                "Glob of paths to include in filtered history. Multiple "
                "--path-glob options can be specified to get a union of "
                "paths."
            ),
        )
        path.add_argument(
            "--path-regex",
            metavar="REGEX",
            type=os.fsencode,
            action=FilteringOptions.AppendFilter,
            dest="path_changes",
            help=_(
                "Regex of paths to include in filtered history. Multiple "
                "--path-regex options can be specified to get a union of "
                "paths"
            ),
        )
        path.add_argument(
            "--use-base-name",
            action="store_true",
            help=_(
                "Match on file base name instead of full path from the top "
                "of the repo.  Incompatible with --path-rename."
            ),
        )

        rename = parser.add_argument_group(
            title=_("Renaming based on paths " "(see also --filename-callback)")
        )
        rename.add_argument(
            "--path-rename",
            "--path-rename-match",
            metavar="OLD_NAME:NEW_NAME",
            dest="path_changes",
            type=os.fsencode,
            action=FilteringOptions.AppendFilter,
            help=_(
                "Path to rename; if filename or directory matches OLD_NAME "
                "rename to NEW_NAME.  Multiple --path-rename options can be "
                "specified."
            ),
        )

        helpers = parser.add_argument_group(title=_("Path shortcuts"))
        helpers.add_argument(
            "--paths-from-file",
            metavar="FILENAME",
            type=os.fsencode,
            action=FilteringOptions.FileWithPathsFilter,
            dest="path_changes",
            help=_(
                "Specify several path filtering and renaming directives, one "
                "per line.  Lines with '==>' in them specify path renames, "
                "and lines can begin with 'literal:' (the default), 'glob:', "
                "or 'regex:' to specify different matching styles"
            ),
        )
        helpers.add_argument(
            "--subdirectory-filter",
            metavar="DIRECTORY",
            action=FilteringOptions.HelperFilter,
            type=os.fsencode,
            help=_(
                "Only look at history that touches the given subdirectory "
                "and treat that directory as the project root.  Equivalent "
                "to using '--path DIRECTORY/ --path-rename DIRECTORY/:'"
            ),
        )
        helpers.add_argument(
            "--to-subdirectory-filter",
            metavar="DIRECTORY",
            action=FilteringOptions.HelperFilter,
            type=os.fsencode,
            help=_(
                "Treat the project root as instead being under DIRECTORY. "
                "Equivalent to using '--path-rename :DIRECTORY/'"
            ),
        )

        contents = parser.add_argument_group(
            title=_("Content editing filters " "(see also --blob-callback)")
        )
        contents.add_argument(
            "--replace-text",
            metavar="EXPRESSIONS_FILE",
            help=_(
                "A file with expressions that, if found, will be replaced. "
                "By default, each expression is treated as literal text, "
                "but 'regex:' and 'glob:' prefixes are supported.  You can "
                "end the line with '==>' and some replacement text to "
                "choose a replacement choice other than the default of "
                "'***REMOVED***'. "
            ),
        )
        contents.add_argument(
            "--strip-blobs-bigger-than",
            metavar="SIZE",
            dest="max_blob_size",
            default=0,
            help=_(
                "Strip blobs (files) bigger than specified size (e.g. '5M', "
                "'2G', etc)"
            ),
        )
        contents.add_argument(
            "--strip-blobs-with-ids",
            metavar="BLOB-ID-FILENAME",
            help=_(
                "Read git object ids from each line of the given file, and "
                "strip all of them from history"
            ),
        )

        refrename = parser.add_argument_group(
            title=_("Renaming of refs " "(see also --refname-callback)")
        )
        refrename.add_argument(
            "--tag-rename",
            metavar="OLD:NEW",
            type=os.fsencode,
            help=_(
                "Rename tags starting with OLD to start with NEW.  For "
                "example, --tag-rename foo:bar will rename tag foo-1.2.3 "
                "to bar-1.2.3; either OLD or NEW can be empty."
            ),
        )

        messages = parser.add_argument_group(
            title=_("Filtering of commit messages " "(see also --message-callback)")
        )
        messages.add_argument(
            "--preserve-commit-hashes",
            action="store_true",
            help=_(
                "By default, since commits are rewritten and thus gain new "
                "hashes, references to old commit hashes in commit messages "
                "are replaced with new commit hashes (abbreviated to the same "
                "length as the old reference).  Use this flag to turn off "
                "updating commit hashes in commit messages."
            ),
        )
        messages.add_argument(
            "--preserve-commit-encoding",
            action="store_true",
            help=_(
                "Do not reencode commit messages into UTF-8.  By default, if "
                "the commit object specifies an encoding for the commit "
                "message, the message is re-encoded into UTF-8."
            ),
        )

        people = parser.add_argument_group(
            title=_(
                "Filtering of names & emails "
                "(see also --name-callback "
                "and --email-callback)"
            )
        )
        people.add_argument(
            "--mailmap",
            dest="mailmap",
            metavar="FILENAME",
            type=os.fsencode,
            help=_(
                "Use specified mailmap file (see git-shortlog(1) for "
                "details on the format) when rewriting author, committer, "
                "and tagger names and emails.  If the specified file is "
                "part of git history, historical versions of the file will "
                "be ignored; only the current contents are consulted."
            ),
        )
        people.add_argument(
            "--use-mailmap",
            dest="mailmap",
            action="store_const",
            const=".mailmap",
            help=_("Same as: '--mailmap .mailmap' "),
        )

        parents = parser.add_argument_group(title=_("Parent rewriting"))
        parents.add_argument(
            "--replace-refs",
            default=None,
            choices=[
                "delete-no-add",
                "delete-and-add",
                "update-no-add",
                "update-or-add",
                "update-and-add",
            ],
            help=_(
                "Replace refs (see git-replace(1)) are used to rewrite "
                "parents (unless turned off by the usual git mechanism); this "
                "flag specifies what do do with those refs afterward. "
                "Replace refs can either be deleted or updated to point at new "
                "commit hashes.  Also, new replace refs can be added for each "
                "commit rewrite.  With 'update-or-add', new replace refs are "
                "only added for commit rewrites that aren't used to update an "
                "existing replace ref. default is 'update-and-add' if "
                "$GIT_DIR/filter-repo/already_ran does not exist; "
                "'update-or-add' otherwise."
            ),
        )
        parents.add_argument(
            "--prune-empty",
            default="auto",
            choices=["always", "auto", "never"],
            help=_(
                "Whether to prune empty commits.  'auto' (the default) means "
                "only prune commits which become empty (not commits which were "
                "empty in the original repo, unless their parent was pruned). "
                "When the parent of a commit is pruned, the first non-pruned "
                "ancestor becomes the new parent."
            ),
        )
        parents.add_argument(
            "--prune-degenerate",
            default="auto",
            choices=["always", "auto", "never"],
            help=_(
                "Since merge commits are needed for history topology, they "
                "are typically exempt from pruning.  However, they can become "
                "degenerate with the pruning of other commits (having fewer "
                "than two parents, having one commit serve as both parents, or "
                "having one parent as the ancestor of the other.)  If such "
                "merge commits have no file changes, they can be pruned.  The "
                "default ('auto') is to only prune empty merge commits which "
                "become degenerate (not which started as such)."
            ),
        )

        callback = parser.add_argument_group(title=_("Generic callback code snippets"))
        callback.add_argument(
            "--filename-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing filenames; see CALLBACKS "
                "sections below."
            ),
        )
        callback.add_argument(
            "--message-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing messages (both commit "
                "messages and tag messages); see CALLBACKS section below."
            ),
        )
        callback.add_argument(
            "--name-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing names of people; see "
                "CALLBACKS section below."
            ),
        )
        callback.add_argument(
            "--email-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing emails addresses; see "
                "CALLBACKS section below."
            ),
        )
        callback.add_argument(
            "--refname-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing refnames; see CALLBACKS "
                "section below."
            ),
        )

        callback.add_argument(
            "--blob-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing blob objects; see "
                "CALLBACKS section below."
            ),
        )
        callback.add_argument(
            "--commit-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing commit objects; see "
                "CALLBACKS section below."
            ),
        )
        callback.add_argument(
            "--tag-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing tag objects; see CALLBACKS "
                "section below."
            ),
        )
        callback.add_argument(
            "--reset-callback",
            metavar="FUNCTION_BODY",
            help=_(
                "Python code body for processing reset objects; see "
                "CALLBACKS section below."
            ),
        )

        desc = _(
            "Specifying alternate source or target locations implies --partial,\n"
            "except that the normal default for --replace-refs is used.  However,\n"
            "unlike normal uses of --partial, this doesn't risk mixing old and new\n"
            "history since the old and new histories are in different repositories."
        )
        location = parser.add_argument_group(
            title=_("Location to filter from/to"), description=desc
        )
        location.add_argument(
            "--source", type=os.fsencode, help=_("Git repository to read from")
        )
        location.add_argument(
            "--target",
            type=os.fsencode,
            help=_("Git repository to overwrite with filtered history"),
        )

        misc = parser.add_argument_group(title=_("Miscellaneous options"))
        misc.add_argument(
            "--help",
            "-h",
            action="store_true",
            help=_("Show this help message and exit."),
        )
        misc.add_argument(
            "--version",
            action="store_true",
            help=_("Display filter-repo's version and exit."),
        )
        misc.add_argument(
            "--force",
            "-f",
            action="store_true",
            help=_(
                "Rewrite history even if the current repo does not look "
                "like a fresh clone."
            ),
        )
        misc.add_argument(
            "--partial",
            action="store_true",
            help=_(
                "Do a partial history rewrite, resulting in the mixture of "
                "old and new history.  This implies a default of "
                "update-no-add for --replace-refs, disables rewriting "
                "refs/remotes/origin/* to refs/heads/*, disables removing "
                "of the 'origin' remote, disables removing unexported refs, "
                "disables expiring the reflog, and disables the automatic "
                "post-filter gc.  Also, this modifies --tag-rename and "
                "--refname-callback options such that instead of replacing "
                "old refs with new refnames, it will instead create new "
                "refs and keep the old ones around.  Use with caution."
            ),
        )
        # WARNING: --refs presents a problem with become-degenerate pruning:
        #   * Excluding a commit also excludes its ancestors so when some other
        #     commit has an excluded ancestor as a parent we have no way of
        #     knowing what it is an ancestor of without doing a special
        #     full-graph walk.
        misc.add_argument(
            "--refs",
            nargs="+",
            help=_(
                "Limit history rewriting to the specified refs.  Implies "
                "--partial.  In addition to the normal caveats of --partial "
                "(mixing old and new history, no automatic remapping of "
                "refs/remotes/origin/* to refs/heads/*, etc.), this also may "
                "cause problems for pruning of degenerate empty merge "
                "commits when negative revisions are specified."
            ),
        )

        misc.add_argument(
            "--dry-run",
            action="store_true",
            help=_(
                "Do not change the repository.  Run `git fast-export` and "
                "filter its output, and save both the original and the "
                "filtered version for comparison.  This also disables "
                "rewriting commit messages due to not knowing new commit "
                "IDs and disables filtering of some empty commits due to "
                "inability to query the fast-import backend."
            ),
        )
        misc.add_argument(
            "--debug",
            action="store_true",
            help=_(
                "Print additional information about operations being "
                "performed and commands being run.  When used together "
                "with --dry-run, also show extra information about what "
                "would be run."
            ),
        )
        # WARNING: --state-branch has some problems:
        #   * It does not work well with manually inserted objects (user creating
        #     Blob() or Commit() or Tag() objects and calling
        #     RepoFilter.insert(obj) on them).
        #   * It does not work well with multiple source or multiple target repos
        #   * It doesn't work so well with pruning become-empty commits (though
        #     --refs doesn't work so well with it either)
        # These are probably fixable, given some work (e.g. re-importing the
        # graph at the beginning to get the AncestryGraph right, doing our own
        # export of marks instead of using fast-export --export-marks, etc.), but
        # for now just hide the option.
        misc.add_argument(
            "--state-branch",
            # help=_("Enable incremental filtering by saving the mapping of old "
            #       "to new objects to the specified branch upon exit, and"
            #       "loading that mapping from that branch (if it exists) "
            #       "upon startup."))
            help=argparse.SUPPRESS,
        )
        misc.add_argument(
            "--stdin",
            action="store_true",
            help=_(
                "Instead of running `git fast-export` and filtering its "
                "output, filter the fast-export stream from stdin.    The "
                "stdin must be in the expected input format (e.g. it needs "
                "to include original-oid directives)."
            ),
        )
        misc.add_argument(
            "--quiet",
            action="store_true",
            help=_("Pass --quiet to other git commands called"),
        )
        return parser

    @staticmethod
    def sanity_check_args(args):
        if args.analyze and args.path_changes:
            raise SystemExit(
                _(
                    "Error: --analyze is incompatible with --path* flags; "
                    "it's a read-only operation."
                )
            )
        if args.analyze and args.stdin:
            raise SystemExit(_("Error: --analyze is incompatible with --stdin."))
        # If no path_changes are found, initialize with empty list but mark as
        # not inclusive so that all files match
        if args.path_changes == None:
            args.path_changes = []
            args.inclusive = False
        else:
            # Similarly, if we have no filtering paths, then no path should be
            # filtered out.  Based on how newname() works, the easiest way to
            # achieve that is setting args.inclusive to False.
            if not any(x[0] == "filter" for x in args.path_changes):
                args.inclusive = False
            # Also check for incompatible --use-base-name and --path-rename flags.
            if args.use_base_name:
                if any(x[0] == "rename" for x in args.path_changes):
                    raise SystemExit(
                        _(
                            "Error: --use-base-name and --path-rename are "
                            "incompatible."
                        )
                    )
        # Also throw some sanity checks on git version here;
        # PERF: remove these checks once new enough git versions are common
        p = subproc.Popen(
            "git fast-export -h".split(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        p.wait()
        output = p.stdout.read()
        if b"--mark-tags" not in output:  # pragma: no cover
            from . import elements
            elements.write_marks = False
            if args.state_branch:
                raise SystemExit(
                    _(
                        "Error: need a version of git whose fast-export "
                        "command has the --mark-tags option"
                    )
                )
        if b"--reencode" not in output:  # pragma: no cover
            if args.preserve_commit_encoding:
                raise SystemExit(
                    _(
                        "Error: need a version of git whose fast-export "
                        "command has the --reencode option"
                    )
                )
            else:
                # Set args.preserve_commit_encoding to None which we'll check for later
                # to avoid passing --reencode=yes to fast-export (that option was the
                # default prior to git-2.23)
                args.preserve_commit_encoding = None
            # If we don't have fast-exoprt --reencode, we may also be missing
            # diff-tree --combined-all-paths, which is even more important...
            p = subproc.Popen(
                "git diff-tree -h".split(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            p.wait()
            output = p.stdout.read()
            if b"--combined-all-paths" not in output:
                raise SystemExit(
                    _(
                        "Error: need a version of git whose diff-tree "
                        "command has the --combined-all-paths option"
                    )
                )
        # End of sanity checks on git version
        if args.max_blob_size:
            suffix = args.max_blob_size[-1]
            if suffix not in "1234567890":
                mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
                if suffix not in mult:
                    raise SystemExit(
                        _(
                            "Error: could not parse --strip-blobs-bigger-than"
                            " argument %s"
                        )
                        % args.max_blob_size
                    )
                args.max_blob_size = int(args.max_blob_size[0:-1]) * mult[suffix]
            else:
                args.max_blob_size = int(args.max_blob_size)

    @staticmethod
    def get_replace_text(filename):
        replace_literals = []
        replace_regexes = []
        with open(filename, "br") as f:
            for line in f:
                line = line.rstrip(b"\r\n")

                # Determine the replacement
                replacement = b"***REMOVED***"
                if b"==>" in line:
                    line, replacement = line.rsplit(b"==>", 1)

                # See if we need to match via regex
                regex = None
                if line.startswith(b"regex:"):
                    regex = line[6:]
                elif line.startswith(b"glob:"):
                    regex = glob_to_regex(line[5:])
                if regex:
                    replace_regexes.append((re.compile(regex), replacement))
                else:
                    # Otherwise, find the literal we need to replace
                    if line.startswith(b"literal:"):
                        line = line[8:]
                    if not line:
                        continue
                    replace_literals.append((line, replacement))
        return {"literals": replace_literals, "regexes": replace_regexes}

    @staticmethod
    def get_paths_from_file(filename):
        new_path_changes = []
        with open(filename, "br") as f:
            for line in f:
                line = line.rstrip(b"\r\n")

                # Skip blank lines
                if not line:
                    continue

                # Determine the replacement
                match_type, repl = "literal", None
                if b"==>" in line:
                    line, repl = line.rsplit(b"==>", 1)

                # See if we need to match via regex
                match_type = "match"  # a.k.a. 'literal'
                if line.startswith(b"regex:"):
                    match_type = "regex"
                    match = re.compile(line[6:])
                elif line.startswith(b"glob:"):
                    match_type = "glob"
                    match = line[5:]
                    if repl:
                        raise SystemExit(
                            _(
                                "Error: In %s, 'glob:' and '==>' are incompatible (renaming globs makes no sense)"
                                % decode(filename)
                            )
                        )
                else:
                    if line.startswith(b"literal:"):
                        match = line[8:]
                    else:
                        match = line
                    if repl is not None:
                        if (
                            match
                            and repl
                            and match.endswith(b"/") != repl.endswith(b"/")
                        ):
                            raise SystemExit(
                                _(
                                    "Error: When rename directories, if OLDNAME "
                                    "and NEW_NAME are both non-empty and either "
                                    "ends with a slash then both must."
                                )
                            )

                # Record the filter or rename
                if repl is not None:
                    new_path_changes.append(["rename", match_type, (match, repl)])
                else:
                    new_path_changes.append(["filter", match_type, match])
            return new_path_changes

    @staticmethod
    def default_options():
        return FilteringOptions.parse_args([], error_on_empty=False)

    @staticmethod
    def parse_args(input_args, error_on_empty=True):
        parser = FilteringOptions.create_arg_parser()
        if not input_args and error_on_empty:
            parser.print_usage()
            raise SystemExit(_("No arguments specified."))
        args = parser.parse_args(input_args)
        if args.help:
            parser.print_help()
            raise SystemExit()
        if args.version:
            GitUtils.print_my_version()
            raise SystemExit()
        FilteringOptions.sanity_check_args(args)
        if args.mailmap:
            args.mailmap = MailmapInfo(args.mailmap)
        if args.replace_text:
            args.replace_text = FilteringOptions.get_replace_text(args.replace_text)
        if args.strip_blobs_with_ids:
            with open(args.strip_blobs_with_ids, "br") as f:
                args.strip_blobs_with_ids = set(f.read().split())
        else:
            args.strip_blobs_with_ids = set()
        if (args.partial or args.refs) and not args.replace_refs:
            args.replace_refs = "update-no-add"
        if args.refs or args.source or args.target:
            args.partial = True
        if not args.refs:
            args.refs = ["--all"]
        return args
