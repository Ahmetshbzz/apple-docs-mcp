from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from apple_docs_mcp import connections


def _make_db(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("create table t (v text);")
    connection.execute("insert into t values (?)", (value,))
    connection.commit()
    connection.close()


def _opener(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_shared_reuses_one_connection_for_an_unchanged_file(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    _make_db(db, "first")
    connections.forget()

    first = connections.shared(db, _opener)
    second = connections.shared(db, _opener)

    assert first is second
    connections.forget()


def test_shared_reopens_when_the_file_changes(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    _make_db(db, "first")
    connections.forget()

    first = connections.shared(db, _opener)
    replacement = tmp_path / "b.sqlite"
    _make_db(replacement, "second")
    replacement.replace(db)
    later = time.time() + 5
    import os

    os.utime(db, (later, later))

    second = connections.shared(db, _opener)

    assert second is not first
    assert second.execute("select v from t").fetchone()[0] == "second"
    connections.forget()


def test_shared_keeps_connections_separate_per_thread(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    _make_db(db, "value")
    connections.forget()

    seen: list[sqlite3.Connection] = []
    main_connection = connections.shared(db, _opener)

    def worker() -> None:
        seen.append(connections.shared(db, _opener))
        connections.forget()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen and seen[0] is not main_connection
    connections.forget()


def test_shared_reports_the_openers_error_for_a_missing_file(tmp_path: Path) -> None:
    def strict(path: Path) -> sqlite3.Connection:
        raise FileNotFoundError(path)

    connections.forget()
    try:
        connections.shared(tmp_path / "absent.sqlite", strict)
    except FileNotFoundError:
        return
    raise AssertionError("the opener's error must reach the caller")
