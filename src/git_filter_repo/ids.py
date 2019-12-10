class _IDs(object):
    """
    A class that maintains the 'name domain' of all the 'marks' (short int
    id for a blob/commit git object). The reason this mechanism is necessary
    is because the text of fast-export may refer to an object using a different
    mark than the mark that was assigned to that object using IDS.new(). This
    class allows you to translate the fast-export marks (old) to the marks
    assigned from IDS.new() (new).

    Note that there are two reasons why the marks may differ: (1) The
    user manually creates Blob or Commit objects (for insertion into the
    stream) (2) We're reading the data from two different repositories
    and trying to combine the data (git fast-export will number ids from
    1...n, and having two 1's, two 2's, two 3's, causes issues).
    """

    def __init__(self):
        """
    Init
    """
        # The id for the next created blob/commit object
        self._next_id = 1

        # A map of old-ids to new-ids (1:1 map)
        self._translation = {}

        # A map of new-ids to every old-id that points to the new-id (1:N map)
        self._reverse_translation = {}

    def new(self):
        """
        Should be called whenever a new blob or commit object is created. The
        returned value should be used as the id/mark for that object.
        """
        rv = self._next_id
        self._next_id += 1
        return rv

    def record_rename(self, old_id, new_id, handle_transitivity=False):
        """
        Record that old_id is being renamed to new_id.
        """
        if old_id != new_id:
            # old_id -> new_id
            self._translation[old_id] = new_id

            # Transitivity will be needed if new commits are being inserted mid-way
            # through a branch.
            if handle_transitivity:
                # Anything that points to old_id should point to new_id
                if old_id in self._reverse_translation:
                    for id_ in self._reverse_translation[old_id]:
                        self._translation[id_] = new_id

            # Record that new_id is pointed to by old_id
            if new_id not in self._reverse_translation:
                self._reverse_translation[new_id] = []
            self._reverse_translation[new_id].append(old_id)

    def translate(self, old_id):
        """
        If old_id has been mapped to an alternate id, return the alternate id.
        """
        if old_id in self._translation:
            return self._translation[old_id]
        else:
            return old_id

    def __str__(self):
        """
        Convert IDs to string; used for debugging
        """
        rv = "Current count: %d\nTranslation:\n" % self._next_id
        for k in sorted(self._translation):
            rv += "  %d -> %s\n" % (k, self._translation[k])

        rv += "Reverse translation:\n"
        for k in sorted(self._reverse_translation):
            rv += "  " + str(k) + " -> " + str(self._reverse_translation[k]) + "\n"

        return rv


_IDS = _IDs()


def record_id_rename(old_id, new_id):
    """
    Register a new translation
    """
    handle_transitivity = True
    _IDS.record_rename(old_id, new_id, handle_transitivity)
