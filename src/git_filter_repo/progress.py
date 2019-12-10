import sys
import time


class ProgressWriter(object):
    def __init__(self):
        self._last_progress_update = time.time()
        self._last_message = None

    def show(self, msg):
        self._last_message = msg
        now = time.time()
        if now - self._last_progress_update > 0.1:
            self._last_progress_update = now
            sys.stdout.write("\r{}".format(msg))
            sys.stdout.flush()

    def finish(self):
        self._last_progress_update = 0
        if self._last_message:
            self.show(self._last_message)
            sys.stdout.write("\n")


