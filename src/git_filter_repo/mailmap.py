import os
import re


class MailmapInfo(object):
    def __init__(self, filename):
        self.changes = {}
        self._parse_file(filename)

    def _parse_file(self, filename):
        name_and_email_re = re.compile(br"(.*?)\s*<([^>]+)>\s*")
        comment_re = re.compile(br"\s*#.*")
        if not os.access(filename, os.R_OK):
            raise SystemExit(_("Cannot read %s") % decode(filename))
        with open(filename, "br") as f:
            count = 0
            for line in f:
                count += 1
                err = "Unparseable mailmap file: line #{} is bad: {}".format(
                    count, line
                )
                # Remove comments
                line = comment_re.sub(b"", line)
                # Remove leading and trailing whitespace
                line = line.strip()
                if not line:
                    continue

                m = name_and_email_re.match(line)
                if not m:
                    raise SystemExit(err)
                proper_name, proper_email = m.groups()
                if len(line) == m.end():
                    self.changes[(None, proper_email)] = (proper_name, proper_email)
                    continue
                rest = line[m.end() :]
                m = name_and_email_re.match(rest)
                if m:
                    commit_name, commit_email = m.groups()
                    if len(rest) != m.end():
                        raise SystemExit(err)
                else:
                    commit_name, commit_email = rest, None
                self.changes[(commit_name, commit_email)] = (proper_name, proper_email)

    def translate(self, name, email):
        """ Given a name and email, return the expected new name and email from the
        mailmap if there is a translation rule for it, otherwise just return
        the given name and email."""
        for old, new in self.changes.items():
            old_name, old_email = old
            new_name, new_email = new
            if (email == old_email or not old_email) and (
                name == old_name or not old_name
            ):
                return (new_name or name, new_email or email)
        return (name, email)
