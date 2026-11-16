import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.chunk import chunk_page_content
from app.config import settings
from app.db import DatabaseManager
from app.parse import (
    extract_available_volumes,
    extract_book_info,
    extract_page_content,
    extract_toc_links,
    extract_volume_navigation,
)
from app.site import SiteClient

logger = logging.getLogger(__name__)


def extract_volume_and_page_from_url(url: str) -> Tuple[int, int]:
    path = urlparse(url).path
    match = re.search(r"/(\d+)/(\d+)/(\d+)/?$", path)
    if match:
        return int(match.group(2)), int(match.group(3))
    return 1, 0


def detect_language(text: str) -> str:
    if re.search(r"[گچپژکی]", text):
        return "fa"
    if re.search(r"[\u0600-\u06ff]", text):
        return "ar"
    return "en"


class BookCrawler:
    def __init__(self) -> None:
        self.database = DatabaseManager()
        self.site_client = SiteClient()

    async def close(self) -> None:
        await self.site_client.close()
        self.database.close()

    def get_book_directory(self, book_id: str) -> Path:
        book_directory_path = Path(settings.OUTPUT_DIR) / "books" / str(book_id)
        book_directory_path.mkdir(parents=True, exist_ok=True)
        return book_directory_path

    def append_jsonl_records(self, file_path: Path, records: List[Dict[str, Any]]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("a", encoding="utf-8") as file_handle:
            for record in records:
                file_handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def write_atomic_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_file_path = file_path.with_suffix(file_path.suffix + ".tmp")
        temporary_file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_file_path.replace(file_path)

    async def crawl(self, source_book_id: str, only_volume_number: Optional[int] = None) -> Dict[str, Any]:
        initial_info_url = f"{settings.BASE_URL.rstrip('/')}/{source_book_id}/1/0"
        try:
            initial_html = await self.site_client.fetch_html(initial_info_url)
        except Exception:
            initial_info_url = f"{settings.BASE_URL.rstrip('/')}/{source_book_id}/1/1"
            initial_html = await self.site_client.fetch_html(initial_info_url)

        title, author = extract_book_info(initial_html, initial_info_url)
        book_id = self.database.register_book(source_book_id, title, author, initial_info_url)
        book_directory = self.get_book_directory(book_id)

        all_detected_volumes = extract_available_volumes(initial_html)
        target_volumes = [int(only_volume_number)] if only_volume_number is not None else all_detected_volumes

        toc_items_raw = extract_toc_links(initial_html, initial_info_url)
        table_of_contents: List[Dict[str, Any]] = []
        for toc_order, (toc_title, toc_url) in enumerate(toc_items_raw, 1):
            volume_number, page_number = extract_volume_and_page_from_url(toc_url)
            table_of_contents.append({
                "title": toc_title,
                "url": toc_url,
                "volume_number": volume_number,
                "page_number": page_number,
                "toc_order": toc_order,
            })

        book_metadata = {
            "book_id": str(book_id),
            "source_book_id": str(source_book_id),
            "title": title or f"کتاب {source_book_id}",
            "author": author or None,
            "source": "school_faghahat",
            "source_url": initial_info_url,
            "toc": table_of_contents,
            "volumes": target_volumes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.write_atomic_json(book_directory / "book.json", book_metadata)

        total_pages_saved = 0
        total_chunks_saved = 0
        encountered_errors: List[Dict[str, str]] = []
        global_page_order = 0

        for volume_number in target_volumes:
            volume_first_page_url = self.site_client.build_page_url(source_book_id, volume_number, 0)
            try:
                volume_landing_html = await self.site_client.fetch_html(volume_first_page_url)
            except Exception:
                volume_first_page_url = self.site_client.build_page_url(source_book_id, volume_number, 1)
                try:
                    volume_landing_html = await self.site_client.fetch_html(volume_first_page_url)
                except Exception as landing_error:
                    logger.warning("Could not load landing page for volume %s: %s", volume_number, landing_error)
                    volume_landing_html = ""

            volume_navigation = extract_volume_navigation(volume_landing_html)
            first_page_number = volume_navigation["first_page"] or 1
            last_page_number = volume_navigation["last_page"]
            is_bounded_by_last_page = last_page_number is not None

            if not is_bounded_by_last_page:
                last_page_number = min(settings.MAX_PAGES_PER_VOLUME, 1000)

            completed_pages = self.database.get_completed_pages(source_book_id, volume_number)
            volume_output_file = book_directory / f"volume_{volume_number:03d}.jsonl"

            consecutive_empty_pages_count = 0

            for page_number in range(first_page_number, last_page_number + 1):
                global_page_order += 1

                if page_number in completed_pages:
                    continue

                page_url = self.site_client.build_page_url(source_book_id, volume_number, page_number)
                try:
                    page_html = await self.site_client.fetch_html(page_url)
                    page_heading, paragraphs, footnotes = extract_page_content(page_html)

                    if not paragraphs:
                        consecutive_empty_pages_count += 1
                        self.database.mark_page_status(
                            source_book_id, volume_number, page_number, global_page_order, "empty"
                        )
                        if not is_bounded_by_last_page and consecutive_empty_pages_count >= 5 and page_number > 10:
                            logger.info(
                                "Stopping volume %s at page %s due to consecutive empty pages",
                                volume_number,
                                page_number,
                            )
                            break
                        continue

                    consecutive_empty_pages_count = 0
                    chunks = chunk_page_content(paragraphs)
                    page_identifier = f"{book_id}_v{volume_number:03d}_po{global_page_order:06d}"

                    chunk_records: List[Dict[str, Any]] = []
                    for sub_order, chunk_content in enumerate(chunks, 1):
                        record: Dict[str, Any] = {
                            "id": f"{page_identifier}_c{sub_order:03d}",
                            "book_id": str(book_id),
                            "book_title": title or f"کتاب {source_book_id}",
                            "author": author or None,
                            "volume_number": volume_number,
                            "page_number": page_number,
                            "page_order": global_page_order,
                            "page_id": page_identifier,
                            "page_chunk_count": len(chunks),
                            "sub_order": sub_order,
                            "child_order": global_page_order * 10000 + sub_order,
                            "content": chunk_content,
                            "lang": detect_language(chunk_content),
                            "meta_info": {
                                "source": "school_faghahat",
                                "source_url": page_url,
                                "source_book_id": str(source_book_id),
                                "section_type": "book_page",
                                "title": page_heading or None,
                            },
                            "footnotes": [
                                {"text": footnote_text, "main_type": "source_footnote"}
                                for footnote_text in footnotes
                            ],
                        }
                        chunk_records.append({key: value for key, value in record.items() if value is not None})

                    self.append_jsonl_records(volume_output_file, chunk_records)
                    self.database.mark_page_status(
                        source_book_id,
                        volume_number,
                        page_number,
                        global_page_order,
                        "completed",
                        len(chunk_records),
                    )
                    total_chunks_saved += len(chunk_records)
                    total_pages_saved += 1
                    logger.info(
                        "Saved book=%s volume=%s page=%s chunks=%s",
                        book_id,
                        volume_number,
                        page_number,
                        len(chunk_records),
                    )

                except Exception as error:
                    logger.exception("Failed to process page: %s", page_url)
                    encountered_errors.append({"url": page_url, "error": str(error)})
                    self.database.mark_page_status(
                        source_book_id,
                        volume_number,
                        page_number,
                        global_page_order,
                        "error",
                        0,
                        str(error),
                    )

        manifest_data = {
            **book_metadata,
            "crawl_updated_at": datetime.now(timezone.utc).isoformat(),
            "last_run_pages_saved": total_pages_saved,
            "last_run_chunks_saved": total_chunks_saved,
            "errors": encountered_errors,
            "files": sorted(file_path.name for file_path in book_directory.glob("volume_*.jsonl")),
        }
        self.write_atomic_json(book_directory / "manifest.json", manifest_data)

        return {
            "book_id": str(book_id),
            "source_book_id": str(source_book_id),
            "pages_saved_this_run": total_pages_saved,
            "chunks_saved_this_run": total_chunks_saved,
            "output": str(book_directory),
            "errors": len(encountered_errors),
        }


Crawler = BookCrawler
