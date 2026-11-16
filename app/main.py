import argparse
import asyncio
import logging

from app.config import settings
from app.crawler import BookCrawler
from app.db import DatabaseManager
from app.site import SiteClient

logger = logging.getLogger("school_faghahat_crawler")


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
    crawler = BookCrawler()
    try:
        crawl_result = await crawler.crawl(
            source_book_id=arguments.source_book_id,
            only_volume_number=getattr(arguments, "volume", None),
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
    database.close()

    logger.info("Starting crawl-all for %s books...", len(target_books))
    for book_entry in target_books:
        crawler = BookCrawler()
        try:
            result = await crawler.crawl(book_entry["source_id"])
            logger.info("Successfully finished book %s: %s", book_entry["source_id"], result)
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

        registered_books_list = []
        for source_id, book_url in category_books.items():
            database.register_discovered_url(book_url, source_id)
            assigned_book_id = database.register_book(source_id, url=book_url)
            registered_books_list.append({
                "source_id": source_id,
                "book_id": assigned_book_id,
                "url": book_url,
            })
    finally:
        await site_client.close()
        database.close()

    limit_count = getattr(arguments, "limit", 0)
    if limit_count > 0:
        registered_books_list = registered_books_list[:limit_count]

    total_to_crawl = len(registered_books_list)
    logger.info("Starting crawl for %s books in category '%s'...", total_to_crawl, category_input)

    for index, book_entry in enumerate(registered_books_list, 1):
        crawler = BookCrawler()
        try:
            logger.info(
                "[%s/%s] Crawling book source_id=%s (book_id=%s)...",
                index,
                total_to_crawl,
                book_entry["source_id"],
                book_entry["book_id"],
            )
            result = await crawler.crawl(book_entry["source_id"])
            logger.info("Finished book %s: %s", book_entry["source_id"], result.get("book_id"))
        except Exception:
            logger.exception("Failed to crawl category book %s", book_entry["source_id"])
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

    resume_subparser = subparsers.add_parser("resume", help="Resume crawling a previously started book")
    resume_subparser.add_argument("--source-book-id", required=True, help="Source book ID in lib.eshia.ir")

    crawl_all_subparser = subparsers.add_parser("crawl-all", help="Crawl all books registered in SQLite")
    crawl_all_subparser.add_argument("--limit", type=int, default=0, help="Maximum number of books to crawl")

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
