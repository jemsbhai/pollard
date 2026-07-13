from pathlib import Path

from pollard import SQLiteStore

from .test_store_shared import root_and_children


def test_sqlite_store_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    with SQLiteStore(db_path) as store:
        root, children = root_and_children(store)
        expected_ids = [child.id for child in children]

    with SQLiteStore(db_path) as reopened:
        assert reopened.get(root.id).payload == {"run": "shared"}
        assert reopened.children(root.id) == sorted(
            expected_ids,
            key=lambda item: (reopened.get(item).kind, item),
        )


def test_gitignore_excludes_sqlite_wal_sidecars() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "*.db-wal" in gitignore
    assert "*.db-shm" in gitignore
