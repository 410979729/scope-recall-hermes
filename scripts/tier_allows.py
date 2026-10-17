"""What the runs of each process tier may do, in one table.

``scripts/check.py`` sets a run up and selects its tests by this table, and ``tests/v11_guard.py`` refuses whatever the
table does not grant, so what is selected and what is refused cannot drift apart.  A tier that is not listed runs every
test in the interpreter: no child process and no socket.
"""

#: Tier -> what its runs may do besides running tests in the interpreter:
#:
#: * ``process``: start child processes of their own and open loopback sockets;
#: * ``git``: run git on the checkout (``tests/v11_guard.py`` limits which commands);
#: * ``build``: run the uv helper and the Windows shells that build and install the wheel;
#: * ``node``: run the node executable the dsh plugin tests drive.
TIER_ALLOWS: dict[str, frozenset[str]] = {
    "native": frozenset({"process"}),
    "host": frozenset({"process", "node"}),
    "migration": frozenset({"process"}),
    "integration": frozenset({"process", "git"}),
    "packaging": frozenset({"process", "git", "build"}),
    "release": frozenset({"process", "git", "build"}),
}
