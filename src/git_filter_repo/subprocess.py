import os
import platform
import subprocess

from .utils import decode


class SubprocessWrapper(object):
    @staticmethod
    def decodify(args):
        if type(args) == str:
            return args
        else:
            assert type(args) == list
            return [decode(x) if type(x) == bytes else x for x in args]

    @staticmethod
    def call(*args, **kwargs):
        if "cwd" in kwargs:
            kwargs["cwd"] = decode(kwargs["cwd"])
        return subprocess.call(SubprocessWrapper.decodify(*args), **kwargs)

    @staticmethod
    def check_output(*args, **kwargs):
        if "cwd" in kwargs:
            kwargs["cwd"] = decode(kwargs["cwd"])
        return subprocess.check_output(SubprocessWrapper.decodify(*args), **kwargs)

    @staticmethod
    def Popen(*args, **kwargs):
        if "cwd" in kwargs:
            kwargs["cwd"] = decode(kwargs["cwd"])
        return subprocess.Popen(SubprocessWrapper.decodify(*args), **kwargs)


subproc = subprocess
if platform.system() == "Windows" or "PRETEND_UNICODE_ARGS" in os.environ:
    subproc = SubprocessWrapper


