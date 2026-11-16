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
    build_page_to_heading_mapping,
    extract_available_volumes,
    extract_book_info,
    extract_category,
    extract_headings_tree,
    extract_page_content,
    extract_volume_navigation,
    find_matching_heading_entry,
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


def attach_page_ids_to_headings_tree(
    headings_tree: List[Dict[str, Any]],
    book_id: str,
    export_volume_number: int,
) -> None:
    for parent_item in headings_tree:
        if parent_item.get("page_number") is not None:
            parent_item["page_id"] = f"{book_id}_vol{export_volume_number}_p{parent_item['page_number']}"
        else:
            parent_item["page_id"] = None

        for child_item in parent_item.get("children", []):
            if child_item.get("page_number") is not None:
                child_item["page_id"] = f"{book_id}_vol{export_volume_number}_p{child_item['page_number']}"
            else:
                child_item["page_id"] = None


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

    async def fetch_headings_tree(
        self,
        source_book_id: str,
        site_volume_number: int,
        candidate_pages: List[int],
    ) -> Tuple[List[Dict[str, Any]], int]:
        for page_number in candidate_pages:
            if not page_number or page_number <= 0:
                continue
            candidate_url = self.site_client.build_page_url(source_book_id, site_volume_number, page_number)
            try:
                candidate_html = await self.site_client.fetch_html(candidate_url)
                if ".fehresttable" in candidate_html or "fehresttable" in candidate_html:
                    extracted_tree, count = extract_headings_tree(candidate_html)
                    if extracted_tree:
                        logger.info(
                            "Extracted %s top-level heading sections from page %s",
                            len(extracted_tree),
                            page_number,
                        )
                        return extracted_tree, count
            except Exception as fetch_error:
                logger.debug("Failed to check heading candidate page %s: %s", page_number, fetch_error)

        return [], 0

    async def crawl(self, source_book_id: str, only_volume_number: Optional[int] = None) -> Dict[str, Any]:
        initial_info_url = f"{settings.BASE_URL.rstrip('/')}/{source_book_id}/1/0"
        try:
            initial_html = await self.site_client.fetch_html(initial_info_url)
        except Exception:
            initial_info_url = f"{settings.BASE_URL.rstrip('/')}/{source_book_id}/1/1"
            initial_html = await self.site_client.fetch_html(initial_info_url)

        title, author = extract_book_info(initial_html, initial_info_url)
        book_category = extract_category(initial_html)
        book_id = self.database.register_book(
            source_id=source_book_id,
            title=title,
            author=author,
            url=initial_info_url,
            category=book_category,
        )
        book_directory = self.get_book_directory(book_id)

        all_detected_volumes = extract_available_volumes(initial_html)
        is_single_volume_book = len(all_detected_volumes) <= 1

        volume_mappings: List[Tuple[int, int]] = []
        if is_single_volume_book:
            site_vol = all_detected_volumes[0] if all_detected_volumes else 1
            export_vol = 0
            volume_mappings.append((site_vol, export_vol))
        else:
            if only_volume_number is not None:
                requested_vol = int(only_volume_number)
                volume_mappings.append((requested_vol, requested_vol))
            else:
                for detected_vol in all_detected_volumes:
                    volume_mappings.append((detected_vol, detected_vol))

        all_book_headings_trees: List[Dict[str, Any]] = []
        total_sections_count = 0

        total_pages_saved = 0
        total_chunks_saved = 0
        encountered_errors: List[Dict[str, str]] = []
        global_page_order = 0

        for site_volume_number, export_volume_number in volume_mappings:
            volume_first_page_url = self.site_client.build_page_url(source_book_id, site_volume_number, 0)
            try:
                volume_landing_html = await self.site_client.fetch_html(volume_first_page_url)
            except Exception:
                volume_first_page_url = self.site_client.build_page_url(source_book_id, site_volume_number, 1)
                try:
                    volume_landing_html = await self.site_client.fetch_html(volume_first_page_url)
                except Exception as landing_error:
                    logger.warning("Could not load landing page for volume %s: %s", site_volume_number, landing_error)
                    volume_landing_html = ""

            volume_navigation = extract_volume_navigation(volume_landing_html)
            first_page_number = volume_navigation["first_page"] or 1
            last_page_number = volume_navigation["last_page"]
            toc_navigation_page = volume_navigation["toc_page"]
            is_bounded_by_last_page = last_page_number is not None

            candidate_pages: List[int] = []
            if toc_navigation_page:
                candidate_pages.extend([toc_navigation_page, toc_navigation_page + 1, toc_navigation_page - 1])
            if last_page_number:
                candidate_pages.extend([last_page_number, last_page_number - 1])

            volume_headings_tree, volume_sections_count = await self.fetch_headings_tree(
                source_book_id=source_book_id,
                site_volume_number=site_volume_number,
                candidate_pages=candidate_pages,
            )

            if volume_headings_tree:
                attach_page_ids_to_headings_tree(volume_headings_tree, str(book_id), export_volume_number)
                all_book_headings_trees.extend(volume_headings_tree)
                total_sections_count += volume_sections_count

            page_to_heading_mappings = build_page_to_heading_mapping(volume_headings_tree)

            if not is_bounded_by_last_page:
                last_page_number = min(settings.MAX_PAGES_PER_VOLUME, 1000)

            completed_pages = self.database.get_completed_pages(source_book_id, export_volume_number)
            volume_output_file = book_directory / f"volume_{export_volume_number:03d}.jsonl"

            consecutive_empty_pages_count = 0

            for page_number in range(first_page_number, last_page_number + 1):
                global_page_order += 1

                if page_number in completed_pages:
                    continue

                page_url = self.site_client.build_page_url(source_book_id, site_volume_number, page_number)
                try:
                    page_html = await self.site_client.fetch_html(page_url)
                    page_heading, paragraphs, footnotes = extract_page_content(page_html)

                    if not paragraphs:
                        consecutive_empty_pages_count += 1
                        self.database.mark_page_status(
                            source_book_id, export_volume_number, page_number, global_page_order, "empty"
                        )
                        if not is_bounded_by_last_page and consecutive_empty_pages_count >= 5 and page_number > 10:
                            logger.info(
                                "Stopping volume %s at page %s due to consecutive empty pages",
                                export_volume_number,
                                page_number,
                            )
                            break
                        continue

                    consecutive_empty_pages_count = 0
                    chunks = chunk_page_content(paragraphs)
                    page_identifier = f"{book_id}_vol{export_volume_number}_p{page_number}"

                    matching_heading_entry = find_matching_heading_entry(page_number, page_to_heading_mappings)
                    section_title = matching_heading_entry["section"] if matching_heading_entry else (page_heading or None)
                    parent_section_id = matching_heading_entry["parent_section_id"] if matching_heading_entry else None
                    child_id = matching_heading_entry["child_id"] if matching_heading_entry else None
                    child_path = matching_heading_entry["child_path"] if matching_heading_entry else None
                    headings_list = matching_heading_entry["headings"] if matching_heading_entry else None
                    bab_title = matching_heading_entry.get("bab") if matching_heading_entry else None

                    chunk_records: List[Dict[str, Any]] = []
                    for sub_order, chunk_content in enumerate(chunks, 1):
                        detected_lang = detect_language(chunk_content)
                        record: Dict[str, Any] = {
                            "id": f"{page_identifier}_c{sub_order}",
                            "main_type": book_category or "عمومی",
                            "sub_type": "book_page",
                            "book_id": str(book_id),
                            "content": chunk_content,
                            "content_fa": chunk_content if detected_lang == "fa" else None,
                            "content_xml": None,
                            "author": author or None,
                            "meta_info": {
                                "ref_books": None,
                                "sanad_xml": None,
                                "narrator": None,
                                "surah_index": None,
                                "ayah_number": None,
                                "surah": None,
                                "surah_name": None,
                                "section_type": "book_page",
                                "title": page_heading or None,
                                "tag_or_narrator": None,
                                "date": None,
                                "source_url": page_url,
                                "source": "school_faghahat",
                                "keywords": None,
                            },
                            "book_title": title or f"کتاب {source_book_id}",
                            "volume": str(export_volume_number),
                            "section": section_title,
                            "page": str(page_number),
                            "volume_number": int(export_volume_number),
                            "section_number": None,
                            "page_number": int(page_number),
                            "child_order": int(global_page_order * 10000 + sub_order),
                            "bab": bab_title,
                            "ref_cursor": None,
                            "ref_key": None,
                            "ref_url": None,
                            "headings": headings_list,
                            "sub_order": int(sub_order),
                            "page_order": int(global_page_order),
                            "page_chunk_count": len(chunks),
                            "page_id": page_identifier,
                            "child_id": child_id,
                            "child_path": child_path,
                            "parent_section_id": parent_section_id,
                            "interpretations": [],
                            "ai_subject": None,
                            "ai_translate": None,
                            "ai_propositions": None,
                            "ai_persons": None,
                            "ai_main_message": None,
                            "ai_keywords": None,
                            "ai_central_concepts": None,
                            "footnotes": [
                                {
                                    "text": footnote_text,
                                    "main_type": "source_footnote",
                                    "order": footnote_index,
                                    "refrence_ids": None,
                                }
                                for footnote_index, footnote_text in enumerate(footnotes, 1)
                            ],
                            "lang": detected_lang,
                            "embedding": None,
                        }
                        chunk_records.append(record)

                    self.append_jsonl_records(volume_output_file, chunk_records)
                    self.database.mark_page_status(
                        source_book_id,
                        export_volume_number,
                        page_number,
                        global_page_order,
                        "completed",
                        len(chunk_records),
                    )
                    total_chunks_saved += len(chunk_records)
                    total_pages_saved += 1
                    logger.info(
                        "Saved book=%s volume=%s page=%s chunks=%s (section=%s)",
                        book_id,
                        export_volume_number,
                        page_number,
                        len(chunk_records),
                        section_title,
                    )

                except Exception as error:
                    logger.exception("Failed to process page: %s", page_url)
                    encountered_errors.append({"url": page_url, "error": str(error)})
                    self.database.mark_page_status(
                        source_book_id,
                        export_volume_number,
                        page_number,
                        global_page_order,
                        "error",
                        0,
                        str(error),
                    )

        volume_document_id = f"{book_id}_v{volume_mappings[0][1]}" if volume_mappings else f"{book_id}_v0"
        volume_number_value = volume_mappings[0][1] if volume_mappings else 0

        book_data = {
            "id": volume_document_id,
            "main_type": book_category or "عمومی",
            "book_id": int(book_id) if str(book_id).isdigit() else book_id,
            "author": author or None,
            "meta_info": {
                "ref_books": None,
                "keywords": [],
            },
            "book_title": title or f"کتاب {source_book_id}",
            "volume_number": int(volume_number_value),
            "section_count": total_sections_count,
            "page_count": total_pages_saved,
            "child_order": 1,
            "ref_key": "school_faghahat",
            "ref_url": initial_info_url,
            "headings": all_book_headings_trees,
        }
        self.write_atomic_json(book_directory / "book.json", book_data)

        manifest_data = {
            **book_data,
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
            "is_single_volume": is_single_volume_book,
            "headings_count": total_sections_count,
            "pages_saved_this_run": total_pages_saved,
            "chunks_saved_this_run": total_chunks_saved,
            "output": str(book_directory),
            "errors": len(encountered_errors),
        }


Crawler = BookCrawler
