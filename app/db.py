import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.config import settings


def get_current_utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatabaseManager:
    def __init__(self, database_path: Optional[str] = None) -> None:
        resolved_path = database_path or settings.SQLITE_PATH
        Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(resolved_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                source_id TEXT PRIMARY KEY,
                book_id TEXT UNIQUE,
                title TEXT,
                author TEXT,
                url TEXT
            );

            CREATE TABLE IF NOT EXISTS pages (
                source_id TEXT,
                volume INTEGER,
                source_page INTEGER,
                page_order INTEGER,
                status TEXT,
                chunks INTEGER DEFAULT 0,
                error TEXT,
                updated_at TEXT,
                PRIMARY KEY(source_id, volume, source_page)
            );

            CREATE TABLE IF NOT EXISTS discovered (
                url TEXT PRIMARY KEY,
                source_id TEXT
            );
            """
        )
        self.connection.commit()

    def register_book(
        self,
        source_id: str,
        title: str = "",
        author: str = "",
        url: str = "",
    ) -> str:
        existing_book = self.connection.execute(
            "SELECT book_id FROM books WHERE source_id = ?",
            (str(source_id),),
        ).fetchone()

        if existing_book:
            self.connection.execute(
                """
                UPDATE books
                SET title = CASE WHEN ? = '' THEN title ELSE ? END,
                    author = CASE WHEN ? = '' THEN author ELSE ? END,
                    url = CASE WHEN ? = '' THEN url ELSE ? END
                WHERE source_id = ?
                """,
                (title, title, author, author, url, url, str(source_id)),
            )
            self.connection.commit()
            return str(existing_book["book_id"])

        maximum_id_row = self.connection.execute(
            "SELECT MAX(CAST(book_id AS INTEGER)) AS max_id FROM books"
        ).fetchone()
        last_numeric_id = maximum_id_row["max_id"] if maximum_id_row else None
        next_numeric_id = max(1000000, (last_numeric_id or 999999) + 1)
        generated_book_id = str(next_numeric_id)

        self.connection.execute(
            "INSERT INTO books (source_id, book_id, title, author, url) VALUES (?, ?, ?, ?, ?)",
            (str(source_id), generated_book_id, title, author, url),
        )
        self.connection.commit()
        return generated_book_id

    def register_discovered_url(self, url: str, source_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO discovered (url, source_id) VALUES (?, ?)",
            (url, str(source_id)),
        )
        self.connection.commit()

    def list_books(self, limit: int = 0) -> List[Dict[str, Any]]:
        query = "SELECT * FROM books ORDER BY CAST(book_id AS INTEGER)"
        if limit > 0:
            query += f" LIMIT {int(limit)}"
        cursor = self.connection.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def get_completed_pages(self, source_id: str, volume_number: int) -> Set[int]:
        cursor = self.connection.execute(
            "SELECT source_page FROM pages WHERE source_id = ? AND volume = ? AND status = 'completed'",
            (str(source_id), int(volume_number)),
        )
        return {int(row["source_page"]) for row in cursor.fetchall()}

    def mark_page_status(
        self,
        source_id: str,
        volume_number: int,
        page_number: int,
        page_order: int,
        status: str,
        chunks_count: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO pages (source_id, volume, source_page, page_order, status, chunks, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, volume, source_page) DO UPDATE SET
                page_order = excluded.page_order,
                status = excluded.status,
                chunks = excluded.chunks,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                str(source_id),
                int(volume_number),
                int(page_number),
                int(page_order),
                status,
                int(chunks_count),
                error_message,
                get_current_utc_timestamp(),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


DB = DatabaseManager
