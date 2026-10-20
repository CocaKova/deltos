from pathlib import Path

from deltos.doctor import maybe_outdated, review, things_of
from deltos.store import Store


def test_old_unconfirmed_volatile_entry_is_listed(tmp_path):
    s = Store(tmp_path, today="2026-10-09")
    for n in range(6):   # "version" entries change a lot in this memory
        s.name(f"app{n}", [], "app")
        for i, d in enumerate(("2026-06-01", "2026-07-01", "2026-08-01")):
            s.remember(f"app{n}", "version", f"1.{i}", since=d)
    s.name("Notely", [], "app")
    s.remember("Notely", "package", "com.example.notely", since="2026-06-27")      # static kind: never listed
    s.remember("Notely", "github version", "v1.19.0", since="2026-07-16")      # old, volatile kind
    s.remember("Notely", "release v1.0.0", "the far shore", since="2026-07-16")  # names one release: a record
    for i, st in enumerate(("building", "testing", "released", "paused", "live", "stopped")):   # it kept changing after
        s.remember("Notely", "status", st, since=f"2026-09-0{i + 1}")
    got = [(t.name, a.name) for _, t, a, _ in maybe_outdated(things_of(tmp_path), "2026-10-09")]
    assert ("Notely", "Github version") in got
    assert ("Notely", "Package") not in got and ("Notely", "Release v1.0.0") not in got
    s.remember("Notely", "github version", "v1.19.0", since="2026-10-09")   # confirmed: off the list
    got = [(t.name, a.name) for _, t, a, _ in maybe_outdated(things_of(tmp_path), "2026-10-09")]
    assert ("Notely", "Github version") not in got
