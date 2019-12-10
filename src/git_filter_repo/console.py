"""Console interface for git-filter-branch."""
import sys

from .filteringoptions import FilteringOptions
from .gettext import setup_gettext
from .repoanalyze import RepoAnalyze
from .repofilter import RepoFilter


def main():
    setup_gettext()
    args = FilteringOptions.parse_args(sys.argv[1:])
    if args.analyze:
        RepoAnalyze.run(args)
    else:
        filter = RepoFilter(args)
        filter.run()
