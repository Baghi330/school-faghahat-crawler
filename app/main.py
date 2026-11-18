import argparse
import asyncio
import logging
from pathlib import Path
from typing import Optional

from app.config import settings
from app.crawler import BookCrawler
from app.db import DatabaseManager
from app.site import SiteClient

logger = logging.getLogger("school_faghahat_crawler")


def normalize_numeric_string(raw_text: str) -> str:
    persian_arabic_digits = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩"
    ascii_digits = "01234567890123456789"
    translation_table = str.maketrans(persian_arabic_digits, ascii_digits)
    return raw_text.translate(translation_table).strip()


def prompt_for_start_book_id(cli_value: Optional[int] = None) -> int:
    if cli_value is not None and cli_value > 0:
        return cli_value

    output_books_directory = Path(settings.OUTPUT_DIR) / "books"
    existing_numeric_ids = []
    if output_books_directory.exists():
        for entry in output_books_directory.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                existing_numeric_ids.append(int(entry.name))

    suggested_start_id = (max(existing_numeric_ids) + 1) if existing_numeric_ids else 1000000

    prompt_text = f"شناسه عددی شروع کتاب (book_id) را وارد کنید [پیش‌فرض: {suggested_start_id}]: "
    try:
        user_input = normalize_numeric_string(input(prompt_text))
        if user_input.isdigit():
            return int(user_input)
    except (EOFError, KeyboardInterrupt):
        pass

    return suggested_start_id


async def execute_discovery() -> None:
    database = DatabaseManager()
    site_client = SiteClient()
    try:
        discovered_books = await site_client.discover_books()
        for source_id, book_url in discovered_books.items():
            database.register_discovered_url(book_url, source_id)
            database.register_book(source_id, url=book_url)
        logger.info("Discovered %s candidate books.", len(discovered_books))
    finally:
        await site_client.close()
        database.close()


def execute_list_books() -> None:
    database = DatabaseManager()
    try:
        books_list = database.list_books()
        logger.info("Total registered books: %s", len(books_list))
        for book_entry in books_list:
            logger.info(
                "BookID: %s | SourceID: %s | Title: %s",
                book_entry["book_id"],
                book_entry["source_id"],
                book_entry["title"] or "بدون عنوان",
            )
    finally:
        database.close()


async def execute_single_crawl(arguments: argparse.Namespace) -> None:
    start_book_id = prompt_for_start_book_id(getattr(arguments, "start_book_id", None))
    crawler = BookCrawler()
    try:
        crawl_result = await crawler.crawl(
            source_book_id=arguments.source_book_id,
            only_volume_number=getattr(arguments, "volume", None),
            custom_book_id=str(start_book_id),
        )
        logger.info("Crawl completed successfully: %s", crawl_result)
    finally:
        await crawler.close()


async def execute_crawl_all(arguments: argparse.Namespace) -> None:
    if not settings.IS_CRAWL_ALL_ENABLED:
        raise SystemExit(
            "برای فعال‌سازی Crawl همه کتاب‌ها، IS_CRAWL_ALL_ENABLED=true را در .env قرار دهید."
        )

    database = DatabaseManager()
    target_books = database.list_books(arguments.limit)

    completed_count = 0
    remaining_books = []
    for book_entry in target_books:
        if database.is_book_completed(book_entry["source_id"]):
            completed_count += 1
        else:
            remaining_books.append(book_entry)

    database.close()

    if completed_count > 0:
        logger.info("Found %s already completed books. Skipping them.", completed_count)

    total_to_crawl = len(remaining_books)
    if total_to_crawl == 0:
        logger.info("All books are already completed!")
        return

    start_book_id = prompt_for_start_book_id(getattr(arguments, "start_book_id", None))
    logger.info(
        "Starting crawl for %s books (Starting Book ID: %s)...",
        total_to_crawl,
        start_book_id,
    )

    for offset, book_entry in enumerate(remaining_books):
        current_book_id = str(start_book_id + offset)
        crawler = BookCrawler()
        try:
            logger.info(
                "[%s/%s] Crawling book source_id=%s with book_id=%s...",
                offset + 1,
                total_to_crawl,
                book_entry["source_id"],
                current_book_id,
            )
            result = await crawler.crawl(
                source_book_id=book_entry["source_id"],
                custom_book_id=current_book_id,
            )
            logger.info("Successfully finished book %s: %s", book_entry["source_id"], result.get("book_id"))
        except Exception:
            logger.exception("Failed to crawl book %s", book_entry["source_id"])
        finally:
            await crawler.close()


async def execute_crawl_category(arguments: argparse.Namespace) -> None:
    site_client = SiteClient()
    database = DatabaseManager()
    category_input = arguments.category

    logger.info("Discovering books for category: %s", category_input)
    try:
        category_books = await site_client.discover_category_books(category_input)
        if not category_books:
            logger.warning("No books found for category: %s", category_input)
            return

        logger.info("Discovered %s books in category '%s'", len(category_books), category_input)

        for source_id, book_url in category_books.items():
            database.register_discovered_url(book_url, source_id)
    finally:
        await site_client.close()

    limit_count = getattr(arguments, "limit", 0)
    category_items = list(category_books.items())
    if limit_count > 0:
        category_items = category_items[:limit_count]

    completed_books = []
    remaining_books = []

    for source_id, book_url in category_items:
        existing_book_id = database.is_book_completed(source_id)
        if existing_book_id:
            completed_books.append((source_id, existing_book_id))
        else:
            remaining_books.append((source_id, book_url))

    database.close()

    if completed_books:
        logger.info(
            "Found %s already completed books in category '%s'. They will be skipped.",
            len(completed_books),
            category_input,
        )

    if not remaining_books:
        logger.info("All %s books in category '%s' are already completed!", len(category_items), category_input)
        return

    total_remaining = len(remaining_books)
    start_book_id = prompt_for_start_book_id(getattr(arguments, "start_book_id", None))

    logger.info(
        "Resuming crawl for %s remaining books in category '%s' (Starting Book ID: %s)...",
        total_remaining,
        category_input,
        start_book_id,
    )

    for offset, (source_id, book_url) in enumerate(remaining_books):
        current_book_id = str(start_book_id + offset)
        crawler = BookCrawler()
        try:
            logger.info(
                "[%s/%s] Crawling book source_id=%s with book_id=%s...",
                offset + 1,
                total_remaining,
                source_id,
                current_book_id,
            )
            result = await crawler.crawl(
                source_book_id=source_id,
                custom_book_id=current_book_id,
            )
            logger.info("Finished book %s: %s", source_id, result.get("book_id"))
        except Exception:
            logger.exception("Failed to crawl category book %s", source_id)
        finally:
            await crawler.close()


def main() -> None:
    numeric_log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    argument_parser = argparse.ArgumentParser(
        description="School Faghahat Crawler -> JSON/JSONL"
    )
    subparsers = argument_parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="Discover books from the library")
    subparsers.add_parser("list-books", help="List registered books in SQLite")

    crawl_subparser = subparsers.add_parser("crawl", help="Crawl a single book")
    crawl_subparser.add_argument("--source-book-id", required=True, help="Source book ID in lib.eshia.ir")
    crawl_subparser.add_argument("--volume", type=int, default=None, help="Specific volume to crawl")
    crawl_subparser.add_argument("--start-book-id", type=int, default=None, help="Starting book ID")

    resume_subparser = subparsers.add_parser("resume", help="Resume crawling a previously started book")
    resume_subparser.add_argument("--source-book-id", required=True, help="Source book ID in lib.eshia.ir")
    resume_subparser.add_argument("--start-book-id", type=int, default=None, help="Starting book ID")

    crawl_all_subparser = subparsers.add_parser("crawl-all", help="Crawl all books registered in SQLite")
    crawl_all_subparser.add_argument("--limit", type=int, default=0, help="Maximum number of books to crawl")
    crawl_all_subparser.add_argument("--start-book-id", type=int, default=None, help="Starting book ID")

    category_subparser = subparsers.add_parser("crawl-category", help="Crawl all books in a specific category")
    category_subparser.add_argument(
        "--category",
        required=True,
        help="Category name or URL (e.g., 'سیاسی' or 'https://lib.eshia.ir/سیاسی/all')",
    )
    category_subparser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of books to crawl in this category",
    )
    category_subparser.add_argument("--start-book-id", type=int, default=None, help="Starting book ID")

    arguments = argument_parser.parse_args()

    if arguments.command == "discover":
        asyncio.run(execute_discovery())
    elif arguments.command == "list-books":
        execute_list_books()
    elif arguments.command in ("crawl", "resume"):
        asyncio.run(execute_single_crawl(arguments))
    elif arguments.command == "crawl-all":
        asyncio.run(execute_crawl_all(arguments))
    elif arguments.command == "crawl-category":
        asyncio.run(execute_crawl_category(arguments))


if __name__ == "__main__":
    main()
