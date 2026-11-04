"""Print SQLite capabilities for the interpreter running this CI job."""

import json

from environment import sqlite_probe

if __name__ == "__main__":
    print(json.dumps(sqlite_probe(), sort_keys=True))
