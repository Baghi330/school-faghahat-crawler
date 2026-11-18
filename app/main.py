import argparse
import asyncio
import logging
from pathlib import Path
from typing import Optional, Set, Tuple

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


def parse_exclude_file_entries(
    cli_exclude_file: Optional[str] = None,
    cli_exclude_ids: Optional[str] = None,
) -> Tuple[Set[str], Set[str]]:
    excluded_ids: Set[str] = set()
    excluded_categories: Set[str] = set()

    if cli_exclude_ids:
        for raw_id in cli_exclude_ids.split(","):
            cleaned = normalize_numeric_string(raw_id).strip()
            if cleaned:
                if cleaned.isdigit():
                    excluded_ids.add(cleaned)
                else:
                    excluded_categories.add(cleaned)

    candidate_paths = []
    if cli_exclude_file:
        candidate_paths.append(Path(cli_exclude_file))
    else:
        candidate_paths.extend([
            Path("data/exclude_books.txt"),
            Path("exclude_books.txt"),
        ])

    for file_path in candidate_paths:
        if file_path.exists() and file_path.is_file():
            try:
                with open(file_path, "r", encoding="utf-8") as file_handle:
                    for line in file_handle:
                        line_content = line.split("#", 1)[0].strip()
                        if not line_content:
                            continue
                        cleaned_line = normalize_numeric_string(line_content)
                        if cleaned_line.isdigit():
                            excluded_ids.add(cleaned_line)
                        else:
                            for part in line_content.split(","):
                                part_cleaned = part.strip()
                                if not part_cleaned:
                                    continue
                                part_normalized = normalize_numeric_string(part_cleaned)
                                if part_normalized.isdigit():
                                    excluded_ids.add(part_normalized)
                                else:
                                    excluded_categories.add(part_cleaned)
            except Exception as read_error:
                logger.warning("Could not read exclude file %s: %s", file_path, read_error)

    return excluded_ids, excluded_categories


def load_excluded_source_ids(
    cli_exclude_file: Optional[str] = None,
    cli_exclude_ids: Optional[str] = None,
) -> Set[str]:
    excluded_ids, _ = parse_exclude_file_entries(
        cli_exclude_file=cli_exclude_file,
        cli_exclude_ids=cli_exclude_ids,
    )
    return excluded_ids


async def resolve_all_excluded_source_ids(
    site_client: SiteClient,
    cli_exclude_file: Optional[str] = None,
    cli_exclude_ids: Optional[str] = None,
    current_category: Optional[str] = None,
) -> Tuple[Set[str], Set[str]]:
    excluded_ids, excluded_categories = parse_exclude_file_entries(
        cli_exclude_file=cli_exclude_file,
        cli_exclude_ids=cli_exclude_ids,
    )

    all_excluded_ids = set(excluded_ids)

    for category_rule in excluded_categories:
        try:
            if "/" not in category_rule and not category_rule.startswith("http") and current_category:
                matched_books = await site_client.discover_category_books(current_category, category_rule)
                if not matched_books:
                    matched_books = await site_client.discover_category_books(category_rule)
            else:
                matched_books = await site_client.discover_category_books(category_rule)

            if matched_books:
                logger.info(
                    "Excluding %s books belonging to category/subcategory '%s'",
                    len(matched_books),
                    category_rule,
                )
                all_excluded_ids.update(matched_books.keys())
            else:
                logger.info("Checked excluded category '%s' (0 books returned)", category_rule)
        except Exception as resolve_error:
            logger.warning("Could not resolve excluded category '%s': %s", category_rule, resolve_error)

    return all_excluded_ids, excluded_categories


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

    site_client = SiteClient()
    try:
        excluded_source_ids, excluded_categories = await resolve_all_excluded_source_ids(
            site_client=site_client,
            cli_exclude_file=getattr(arguments, "exclude_file", None),
            cli_exclude_ids=getattr(arguments, "exclude", None),
        )
    finally:
        await site_client.close()

    database = DatabaseManager()
    all_target_books = database.list_books(0)

    if excluded_source_ids:
        logger.info("Loaded %s excluded book IDs to ignore.", len(excluded_source_ids))

    completed_count = 0
    skipped_excluded_count = 0
    remaining_books = []
    for book_entry in all_target_books:
        source_id = str(book_entry["source_id"])
        if source_id in excluded_source_ids:
            skipped_excluded_count += 1
            continue
        if database.is_book_completed(source_id):
            completed_count += 1
        else:
            remaining_books.append(book_entry)

    database.close()

    if skipped_excluded_count > 0:
        logger.info("Ignored %s books based on exclude list.", skipped_excluded_count)

    if completed_count > 0:
        logger.info("Found %s already completed books. Skipping them.", completed_count)

    limit_count = getattr(arguments, "limit", 0)
    if limit_count > 0:
        remaining_books = remaining_books[:limit_count]

    total_to_crawl = len(remaining_books)
    if total_to_crawl == 0:
        logger.info("All books are already completed or excluded!")
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
    raw_categories_input = arguments.category
    category_list: List[str] = []
    if isinstance(raw_categories_input, str):
        raw_list = [raw_categories_input]
    else:
        raw_list = list(raw_categories_input)

    for item in raw_list:
        for sub_item in item.split(","):
            cleaned = sub_item.strip()
            if cleaned:
                category_list.append(cleaned)

    if not category_list:
        logger.error("No valid categories provided.")
        return

    site_client = SiteClient()
    database = DatabaseManager()

    subcategory_input = getattr(arguments, "sub_category", None)

    if len(category_list) == 1 and subcategory_input:
        category_tasks = [(category_list[0], subcategory_input)]
    else:
        category_tasks = [(cat, None) for cat in category_list]

    excluded_source_ids, excluded_categories = await resolve_all_excluded_source_ids(
        site_client=site_client,
        cli_exclude_file=getattr(arguments, "exclude_file", None),
        cli_exclude_ids=getattr(arguments, "exclude", None),
        current_category=category_list[0] if len(category_list) == 1 else None,
    )

    combined_category_books: Dict[str, str] = {}
    from urllib.parse import unquote

    for cat_name, sub_name in category_tasks:
        cat_display = f"{cat_name} / {sub_name}" if sub_name else cat_name

        unquoted_cat = unquote(cat_name).strip("/")
        unquoted_sub = unquote(sub_name).strip("/") if sub_name else ""
        normalized_cat = unquoted_cat.replace(" ", "_")
        normalized_sub = unquoted_sub.replace(" ", "_") if unquoted_sub else ""

        is_directly_excluded = False
        for cat_rule in excluded_categories:
            unquoted_rule = unquote(cat_rule).strip("/")
            clean_rule = unquoted_rule.replace(" ", "_")
            if clean_rule in (normalized_cat, normalized_sub, f"{normalized_cat}/{normalized_sub}") or unquoted_rule in (unquoted_cat, unquoted_sub, f"{unquoted_cat}/{unquoted_sub}"):
                is_directly_excluded = True
                break
            if cat_rule in (cat_name, sub_name, cat_display):
                is_directly_excluded = True
                break

        if is_directly_excluded:
            logger.info("Category '%s' is explicitly marked as excluded. Skipping.", cat_display)
            continue

        logger.info("Discovering books for category: %s", cat_display)
        try:
            cat_books = await site_client.discover_category_books(cat_name, sub_name)
            if not cat_books:
                logger.warning("No books found for category: %s", cat_display)
                continue

            logger.info("Discovered %s books in category '%s'", len(cat_books), cat_display)
            for source_id, book_url in cat_books.items():
                database.register_discovered_url(book_url, source_id)
                combined_category_books.setdefault(source_id, book_url)
        except Exception as error:
            logger.exception("Failed to discover books for category '%s': %s", cat_display, error)

    await site_client.close()

    if not combined_category_books:
        logger.warning("No books found across the specified categories.")
        database.close()
        return

    logger.info("Total %s unique books discovered across %s categories.", len(combined_category_books), len(category_tasks))

    limit_count = getattr(arguments, "limit", 0)

    completed_books = []
    skipped_excluded_books = []
    remaining_books = []

    for source_id, book_url in combined_category_books.items():
        if str(source_id) in excluded_source_ids:
            skipped_excluded_books.append((source_id, book_url))
            continue

        existing_book_id = database.is_book_completed(source_id)
        if existing_book_id:
            completed_books.append((source_id, existing_book_id))
        else:
            remaining_books.append((source_id, book_url))

    database.close()

    if skipped_excluded_books:
        logger.info("Ignored %s books based on exclude list.", len(skipped_excluded_books))

    if completed_books:
        logger.info("Found %s already completed books. They will be skipped.", len(completed_books))

    if limit_count > 0:
        remaining_books = remaining_books[:limit_count]

    if not remaining_books:
        logger.info("All %s books across the selected categories are completed or excluded!", len(combined_category_books))
        return

    total_remaining = len(remaining_books)
    start_book_id = prompt_for_start_book_id(getattr(arguments, "start_book_id", None))

    categories_summary = ", ".join(category_list)
    logger.info(
        "Starting crawl for %s remaining books in [%s] (Starting Book ID: %s)...",
        total_remaining,
        categories_summary,
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
    crawl_all_subparser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="Comma-separated source book IDs to exclude/ignore (e.g., '12345,26186')",
    )
    crawl_all_subparser.add_argument(
        "--exclude-file",
        type=str,
        default=None,
        help="Path to file containing excluded source book IDs (default: data/exclude_books.txt)",
    )

    category_subparser = subparsers.add_parser("crawl-category", help="Crawl books in one or more categories")
    category_subparser.add_argument(
        "--category",
        "--categories",
        nargs="+",
        required=True,
        help="One or more category names or URLs (e.g., 'علوم حدیث' 'تاریخ' 'اخلاق')",
    )
    category_subparser.add_argument(
        "--sub-category",
        "--subcategory",
        "--sub",
        type=str,
        default=None,
        dest="sub_category",
        help="Sub-category name (e.g., 'تاریخ' or 'دفاع_مقدس')",
    )
    category_subparser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of books to crawl in this category",
    )
    category_subparser.add_argument("--start-book-id", type=int, default=None, help="Starting book ID")
    category_subparser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="Comma-separated source book IDs to exclude/ignore (e.g., '12345,26186')",
    )
    category_subparser.add_argument(
        "--exclude-file",
        type=str,
        default=None,
        help="Path to file containing excluded source book IDs (default: data/exclude_books.txt)",
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
