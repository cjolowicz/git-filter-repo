"""
git-filter-repo filters git repositories, similar to git filter-branch, BFG
repo cleaner, and others.  The basic idea is that it works by running
     git fast-export <options> | filter | git fast-import <options>
where this program not only launches the whole pipeline but also serves as
the 'filter' in the middle.  It does a few additional things on top as well
in order to make it into a well-rounded filtering tool.

git-filter-repo can also be used as a library for more involved filtering
operations; however:
    ***** API BACKWARD COMPATIBILITY CAVEAT *****
    Programs using git-filter-repo as a library can reach pretty far into its
    internals, but I am not prepared to guarantee backward compatibility of
    all APIs.  I suspect changes will be rare, but I reserve the right to
    change any API.  Since it is assumed that repository filtering is
    something one would do very rarely, and in particular that it's a
    one-shot operation, this should not be a problem in practice for anyone.
    However, if you want to re-use a program you have written that uses
    git-filter-repo as a library (or makes use of one of its --*-callback
    arguments), you should either make sure you are using the same version of
    git and git-filter-repo, or make sure to re-test it.

    If there are particular pieces of the API you are concerned about, and
    there is not already a testcase for it in t9391-lib-usage.sh or
    t9392-python-callback.sh, please contribute a testcase.  That will not
    prevent me from changing the API, but it will allow you to look at the
    history of a testcase to see whether and how the API changed.
    ***** END API BACKWARD COMPATIBILITY CAVEAT *****
"""
from .console import (
    ProgressWriter,
    GitUtils,
    FilteringOptions,
    RepoFilter,
)
from .ids import record_id_rename
from .time import string_to_date, date_to_string
from .elements import Blob, Checkpoint, Commit, FileChange, Progress, Reset, Tag
from .fastexportparser import FastExportParser

__version__ = "2.24.0"
__all__ = [
    "Blob",
    "Reset",
    "FileChange",
    "Commit",
    "Tag",
    "Progress",
    "Checkpoint",
    "FastExportParser",
    "ProgressWriter",
    "string_to_date",
    "date_to_string",
    "record_id_rename",
    "GitUtils",
    "FilteringOptions",
    "RepoFilter",
]
