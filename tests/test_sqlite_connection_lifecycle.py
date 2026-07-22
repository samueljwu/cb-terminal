import sqlite3
import tempfile
import unittest
from pathlib import Path

from cb_terminal.storage.canonical_store import CanonicalStore
from cb_terminal.storage.price_history_store import PriceHistoryStore
from cb_terminal.storage.sqlite_store import CbTerminalStore


class SqliteConnectionLifecycleTests(unittest.TestCase):
    def test_store_connection_contexts_close_their_database_handles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stores = (
                CbTerminalStore(root / "terminal.sqlite"),
                PriceHistoryStore(root / "prices.sqlite"),
                CanonicalStore(root / "canonical.sqlite"),
            )

            for store in stores:
                with self.subTest(store=type(store).__name__):
                    with store._connect() as connection:
                        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")

    def test_connection_context_closes_after_an_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CbTerminalStore(Path(tmpdir) / "terminal.sqlite")

            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with store._connect() as connection:
                    raise RuntimeError("test failure")

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
