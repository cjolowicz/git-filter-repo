import os
import subprocess
import sys

from .elements import FileChange
from .pathquoting import PathQuoting
from .progress import ProgressWriter
from .subprocess import subproc
from .utils import decode


class GitUtils(object):
    @staticmethod
    def get_commit_count(repo, *args):
        """
        Return the number of commits that have been made on repo.
        """
        if not args:
            args = ["--all"]
        if len(args) == 1 and isinstance(args[0], list):
            args = args[0]
        p1 = subproc.Popen(
            ["git", "rev-list"] + args,
            bufsize=-1,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repo,
        )
        p2 = subproc.Popen(["wc", "-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
        count = int(p2.communicate()[0])
        if p1.poll() != 0:
            raise SystemExit(
                _("%s does not appear to be a valid git repository") % repo
            )
        return count

    @staticmethod
    def get_total_objects(repo):
        """
        Return the number of objects (both packed and unpacked)
        """
        p1 = subproc.Popen(
            ["git", "count-objects", "-v"], stdout=subprocess.PIPE, cwd=repo
        )
        lines = p1.stdout.read().splitlines()
        # Return unpacked objects + packed-objects
        return int(lines[0].split()[1]) + int(lines[2].split()[1])

    @staticmethod
    def is_repository_bare(repo_working_dir):
        out = subproc.check_output(
            "git rev-parse --is-bare-repository".split(), cwd=repo_working_dir
        )
        return out.strip() == b"true"

    @staticmethod
    def determine_git_dir(repo_working_dir):
        d = subproc.check_output(
            "git rev-parse --git-dir".split(), cwd=repo_working_dir
        ).strip()
        if repo_working_dir == b"." or d.startswith(b"/"):
            return d
        return os.path.join(repo_working_dir, d)

    @staticmethod
    def get_refs(repo_working_dir):
        try:
            output = subproc.check_output("git show-ref".split(), cwd=repo_working_dir)
        except subprocess.CalledProcessError as e:
            # If error code is 1, there just aren't any refs; i.e. new repo.
            # If error code is other than 1, some other error (e.g. not a git repo)
            if e.returncode != 1:
                raise SystemExit("fatal: {}".format(e))
            output = ""
        return dict(reversed(x.split()) for x in output.splitlines())

    @staticmethod
    def get_blob_sizes(quiet=False):
        blob_size_progress = ProgressWriter()
        num_blobs = 0

        # Get sizes of blobs by sha1
        cmd = (
            "--batch-check=%(objectname) %(objecttype) "
            + "%(objectsize) %(objectsize:disk)"
        )
        cf = subproc.Popen(
            ["git", "cat-file", "--batch-all-objects", cmd],
            bufsize=-1,
            stdout=subprocess.PIPE,
        )
        unpacked_size = {}
        packed_size = {}
        for line in cf.stdout:
            sha, objtype, objsize, objdisksize = line.split()
            objsize, objdisksize = int(objsize), int(objdisksize)
            if objtype == b"blob":
                unpacked_size[sha] = objsize
                packed_size[sha] = objdisksize
            num_blobs += 1
            if not quiet:
                blob_size_progress.show(_("Processed %d blob sizes") % num_blobs)
        cf.wait()
        if not quiet:
            blob_size_progress.finish()
        return unpacked_size, packed_size

    @staticmethod
    def get_file_changes(repo, parent_hash, commit_hash):
        """
        Return a FileChanges list with the differences between parent_hash
        and commit_hash
        """
        file_changes = []

        cmd = ["git", "diff-tree", "-r", parent_hash, commit_hash]
        output = subproc.check_output(cmd, cwd=repo)
        for line in output.splitlines():
            fileinfo, path = line.split(b"\t", 1)
            if path.startswith(b'"'):
                path = PathQuoting.dequote(path)
            oldmode, mode, oldhash, newhash, changetype = fileinfo.split()
            if changetype == b"D":
                file_changes.append(FileChange(b"D", path))
            elif changetype in (b"A", b"M"):
                identifier = HASH_TO_ID.get(newhash, newhash)
                file_changes.append(FileChange(b"M", path, identifier, mode))
            else:  # pragma: no cover
                raise SystemExit("Unknown change type for line {}".format(line))

        return file_changes

    @staticmethod
    def print_my_version():
        with open(sys.argv[0], "br") as f:
            contents = f.read()
        # If people replaced @@LOCALEDIR@@ string to point at their local
        # directory, undo it so we can get original source version.
        contents = re.sub(
            br"^#\!/usr/bin/env python.*", br"#!/usr/bin/env python3", contents
        )
        contents = re.sub(
            br'(\("GIT_TEXTDOMAINDIR"\) or ").*"', br'\1@@LOCALEDIR@@"', contents
        )

        cmd = "git hash-object --stdin".split()
        version = subproc.check_output(cmd, input=contents).strip()
        print(decode(version[0:12]))
