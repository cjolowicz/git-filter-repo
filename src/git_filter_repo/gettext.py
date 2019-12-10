import gettext
import os


def gettext_poison(msg):
    if "GIT_TEST_GETTEXT_POISON" in os.environ:  # pragma: no cover
        return "# GETTEXT POISON #"
    return gettext.gettext(msg)


_ = gettext_poison


def setup_gettext():
    TEXTDOMAIN = "git-filter-repo"
    podir = os.environ.get("GIT_TEXTDOMAINDIR") or "@@LOCALEDIR@@"
    if not os.path.isdir(podir):  # pragma: no cover
        podir = None  # Python has its own fallback; use that

    ## This looks like the most straightforward translation of the relevant
    ## code in git.git:gettext.c and git.git:perl/Git/I18n.pm:
    # import locale
    # locale.setlocale(locale.LC_MESSAGES, "");
    # locale.setlocale(locale.LC_TIME, "");
    # locale.textdomain(TEXTDOMAIN);
    # locale.bindtextdomain(TEXTDOMAIN, podir);
    ## but the python docs suggest using the gettext module (which doesn't
    ## have setlocale()) instead, so:
    gettext.textdomain(TEXTDOMAIN)
    gettext.bindtextdomain(TEXTDOMAIN, podir)


