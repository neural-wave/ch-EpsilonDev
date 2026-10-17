"""Build the two local assets the context step reads: the terminology index and the value sets.

Both derive from restricted terminologies loaded in Virtuoso, so they stay in assets/ (git-ignored)
and are never committed. Run this once on any machine that has the database, before starting the API.
It takes about a minute. The API never queries the database itself.
    python scripts/build_assets.py --endpoint http://localhost:8890/sparql
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from nl2sparql.config import Settings

SCRIPTS = Path(__file__).resolve().parent
BUILDERS = ("build_terminology_index.py", "build_value_sets.py")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=Settings.from_env().virtuoso_endpoint)
    args = parser.parse_args()
    for name in BUILDERS:
        started = time.perf_counter()
        print(f"== {name}")
        subprocess.run([sys.executable, str(SCRIPTS / name), "--endpoint", args.endpoint], check=True)
        print(f"   done in {time.perf_counter() - started:.0f} s")
    print("assets built: restart the API to load them")


if __name__ == "__main__":
    main()
