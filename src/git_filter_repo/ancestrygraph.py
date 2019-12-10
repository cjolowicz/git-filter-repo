class AncestryGraph(object):
    """
    A class that maintains a direct acycle graph of commits for the purpose of
    determining if one commit is the ancestor of another.
    """

    def __init__(self):
        self.cur_value = 0

        # A mapping from the external identifers given to us to the simple integers
        # we use in self.graph
        self.value = {}

        # A tuple of (depth, list-of-ancestors).  Values and keys in this graph are
        # all integers from the self.value dict.  The depth of a commit is one more
        # than the max depth of any of its ancestors.
        self.graph = {}

    def record_external_commits(self, external_commits):
        """
        Record in graph that each commit in external_commits exists, and is
        treated as a root commit with no parents.
        """
        for c in external_commits:
            if c not in self.value:
                self.cur_value += 1
                self.value[c] = self.cur_value
                self.graph[self.cur_value] = (1, [])

    def add_commit_and_parents(self, commit, parents):
        """
        Record in graph that commit has the given parents.  parents _MUST_ have
        been first recorded.  commit _MUST_ not have been recorded yet.
        """
        assert all(p in self.value for p in parents)
        assert commit not in self.value

        # Get values for commit and parents
        self.cur_value += 1
        self.value[commit] = self.cur_value
        graph_parents = [self.value[x] for x in parents]

        # Determine depth for commit, then insert the info into the graph
        depth = 1
        if parents:
            depth += max(self.graph[p][0] for p in graph_parents)
        self.graph[self.cur_value] = (depth, graph_parents)

    def is_ancestor(self, possible_ancestor, check):
        """
        Return whether possible_ancestor is an ancestor of check
        """
        a, b = self.value[possible_ancestor], self.value[check]
        a_depth = self.graph[a][0]
        ancestors = [b]
        visited = set()
        while ancestors:
            ancestor = ancestors.pop()
            if ancestor in visited:
                continue
            visited.add(ancestor)
            depth, more_ancestors = self.graph[ancestor]
            if ancestor == a:
                return True
            elif depth <= a_depth:
                continue
            ancestors.extend(more_ancestors)
        return False
