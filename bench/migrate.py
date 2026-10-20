"""The benchmark's import CLI (the importer is deltos.sources.notes).

  python3 -m bench.migrate --git repo:commit:label[,...] --out out/real/x --workers 2
  python3 -m bench.migrate --replay out/real/x/migrate-log.jsonl --out out/real/x-replay
"""
from deltos.sources.notes import *  # noqa: F401,F403
from deltos.sources.notes import main

if __name__ == "__main__":
    main()
