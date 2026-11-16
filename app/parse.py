import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from app.config import settings

NAVIGATION_KEYWORDS = {
    "اول",
    "صفحه‌اول",
    "قبلی",
    "صفحه‌قبلی",
    "بعدی",
    "صفحه‌بعدی",
    "آخر",
    "صفحه‌آخر",
    "فهرست",
    "شناسنامه",
    "فرمت pdf",
}


def clean_text(raw_text: Optional[str]) -> str:
    if not raw_text:
        return ""
    return " ".join(raw_text.split())


def extract_book_info(html_content: str, source_url: str = "") -> Tuple[str, str]:
    soup = BeautifulSoup(html_content, "lxml")
    book_title = ""
    author_name = ""

    book_title_elements = soup.select(".Book_Title")
    for title_element in book_title_elements:
        combined_text = clean_text(title_element.get_text(" ", strip=True))
        if not book_title:
            title_match = re.search(
                r"نام کتاب\s*:\s*([^ج\n\r]+?)(?:\s*جلد|\s*صفحه|\s*نویسنده|$)",
                combined_text,
            )
            if title_match:
                book_title = clean_text(title_match.group(1))

        if not author_name:
            author_match = re.search(
                r"نویسنده\s*:\s*(.+?)(?:\s*جلد|\s*صفحه|\s*نام کتاب|$)",
                combined_text,
            )
            if author_match:
                candidate_author = clean_text(author_match.group(1))
                if candidate_author not in ("---", "-", ""):
                    author_name = candidate_author

    if not book_title:
        h1_element = soup.find("h1")
        raw_heading = clean_text(h1_element.get_text()) if h1_element else ""
        if raw_heading:
            sanitized_heading = re.sub(
                r"(?:جلد|صفحه)\s*:\s*\d+",
                "",
                raw_heading,
            )
            book_title = clean_text(sanitized_heading)

    if not book_title and soup.title and soup.title.string:
        title_tag_content = soup.title.string
        title_segments = [segment.strip() for segment in title_tag_content.split("|")]
        if title_segments:
            first_segment = title_segments[0]
            sanitized_segment = re.sub(
                r"(?:،\s*)?ج\s*\d+(?:،\s*)?ص\s*\d+",
                "",
                first_segment,
            )
            book_title = clean_text(sanitized_segment)

        if not author_name and len(title_segments) > 1:
            second_segment = clean_text(title_segments[1])
            if "کتابخانه" not in second_segment and "فقاهت" not in second_segment:
                author_name = second_segment

    if not author_name:
        for author_selector in [".author", ".book-author", "#author"]:
            author_element = soup.select_one(author_selector)
            if author_element:
                candidate = clean_text(author_element.get_text())
                if candidate:
                    author_name = candidate
                    break

    if not author_name:
        page_text = clean_text(soup.get_text(" ", strip=True))
        author_fallback_match = re.search(
            r"(?:مؤلف|نویسنده|تألیف|تالیف)\s*[:：]?\s*([^،؛|\n\r]{3,80})",
            page_text,
        )
        if author_fallback_match:
            candidate = clean_text(author_fallback_match.group(1))
            if candidate not in ("---", "-", ""):
                author_name = candidate

    return book_title, author_name


def extract_category(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "lxml")
    for anchor in soup.find_all("a"):
        raw_href = unquote(anchor.get("href", ""))
        parsed_url = urlparse(raw_href)
        path_segments = [segment for segment in parsed_url.path.strip("/").split("/") if segment]
        if len(path_segments) >= 2 and path_segments[-1] == "all" and path_segments[0] != "all":
            category_candidate = path_segments[0].replace("_", " ").strip()
            if category_candidate:
                return category_candidate

    return "عمومی"


def extract_volume_navigation(html_content: str) -> Dict[str, Optional[int]]:
    soup = BeautifulSoup(html_content, "lxml")
    navigation_info: Dict[str, Optional[int]] = {
        "first_page": None,
        "last_page": None,
        "toc_page": None,
    }

    for anchor in soup.find_all("a"):
        anchor_text = clean_text(anchor.get_text()).lower()
        href_value = anchor.get("href", "")
        match = re.search(r"/(\d+)/(\d+)/(\d+)/?$", href_value)
        if not match:
            continue

        page_number = int(match.group(3))

        if "اول" in anchor_text:
            if navigation_info["first_page"] is None or page_number < navigation_info["first_page"]:
                navigation_info["first_page"] = page_number
        elif "آخر" in anchor_text or "اخر" in anchor_text:
            if navigation_info["last_page"] is None or page_number > navigation_info["last_page"]:
                navigation_info["last_page"] = page_number
        elif "فهرست" in anchor_text:
            navigation_info["toc_page"] = page_number

    return navigation_info


def extract_available_volumes(html_content: str) -> List[int]:
    soup = BeautifulSoup(html_content, "lxml")
    discovered_volumes: Set[int] = set()

    for selector in soup.select("select.VolumeSelector, select[name='volume']"):
        for option in selector.find_all("option"):
            raw_value = option.get("value", "").strip()
            if raw_value.isdigit():
                discovered_volumes.add(int(raw_value))

    if not discovered_volumes:
        for anchor in soup.find_all("a"):
            href_value = anchor.get("href", "")
            match = re.search(r"/\d+/(\d+)/\d+/?$", href_value)
            if match:
                discovered_volumes.add(int(match.group(1)))

    if not discovered_volumes:
        return [1]

    valid_volumes = [volume for volume in sorted(discovered_volumes) if volume > 0]
    return valid_volumes or [1]


def extract_toc_links(html_content: str, base_url: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html_content, "lxml")
    table_of_contents_items: List[Tuple[str, str]] = []
    seen_urls: Set[str] = set()

    for selector in settings.TOC_SELECTORS.split(","):
        cleaned_selector = selector.strip()
        if not cleaned_selector:
            continue
        for container in soup.select(cleaned_selector):
            for anchor in container.select("a[href]"):
                title = clean_text(anchor.get_text())
                href = urljoin(base_url, anchor.get("href", "").strip())
                if title and href not in seen_urls and not is_navigation_button(title):
                    seen_urls.add(href)
                    table_of_contents_items.append((title, href))

    return table_of_contents_items


def is_navigation_button(text: str) -> bool:
    normalized_text = clean_text(text).lower()
    return any(keyword in normalized_text for keyword in NAVIGATION_KEYWORDS)


def extract_page_content(html_content: str) -> Tuple[str, List[str], List[str]]:
    soup = BeautifulSoup(html_content, "lxml")

    for remove_selector in settings.REMOVE_SELECTORS.split(","):
        cleaned_selector = remove_selector.strip()
        if cleaned_selector:
            for removable_element in soup.select(cleaned_selector):
                removable_element.decompose()

    content_container = None
    for content_selector in settings.CONTENT_SELECTORS.split(","):
        cleaned_selector = content_selector.strip()
        if not cleaned_selector:
            continue
        candidate_element = soup.select_one(cleaned_selector)
        if candidate_element and len(candidate_element.get_text(strip=True)) > 20:
            content_container = candidate_element
            break

    if content_container is None:
        possible_candidates = soup.select("article, main, div")
        content_container = max(
            possible_candidates,
            key=lambda element: len(element.get_text(strip=True)),
            default=soup.body or soup,
        )

    for extra_tag in content_container.select("script, style, button, form, input, select"):
        extra_tag.decompose()

    heading_element = content_container.find(["h1", "h2", "h3", "h4"])
    page_title = clean_text(heading_element.get_text()) if heading_element else ""

    paragraphs_list: List[str] = []
    for p_element in content_container.find_all("p"):
        paragraph_text = clean_text(p_element.get_text())
        if len(paragraph_text) >= 15 and not is_navigation_button(paragraph_text):
            if not paragraphs_list or paragraph_text != paragraphs_list[-1]:
                paragraphs_list.append(paragraph_text)

    if not paragraphs_list:
        raw_lines = content_container.get_text("\n", strip=True).splitlines()
        for line in raw_lines:
            cleaned_line = clean_text(line)
            if len(cleaned_line) >= 15 and not is_navigation_button(cleaned_line):
                if not paragraphs_list or cleaned_line != paragraphs_list[-1]:
                    paragraphs_list.append(cleaned_line)

    footnotes_list: List[str] = []
    for footnote_element in content_container.select(".footnote, .footnotes, .pfootnote"):
        footnote_text = clean_text(footnote_element.get_text())
        if footnote_text and footnote_text not in footnotes_list:
            footnotes_list.append(footnote_text)

    return page_title, paragraphs_list, footnotes_list


PARENT_SECTION_PATTERN = re.compile(
    r"^(?:فصل|بخش|باب|درس|المقصد|الفصل|الباب|المبحث|المدخل|كتاب|الكتاب|جزء|پيش‌گفتار|پیش‌گفتار|مقدمه)\b",
    re.IGNORECASE,
)


def extract_headings_tree(html_content: str) -> Tuple[List[Dict[str, Any]], int]:
    soup = BeautifulSoup(html_content, "lxml")
    table_element = soup.select_one(".fehresttable")
    if not table_element:
        return [], 0

    headings_tree: List[Dict[str, Any]] = []
    current_parent_section: Optional[Dict[str, Any]] = None
    section_order = 0

    for table_row in table_element.find_all("tr"):
        table_cells = table_row.find_all("td")
        if len(table_cells) < 2:
            continue

        title_text = clean_text(table_cells[0].get_text(" ", strip=True))
        if not title_text or is_navigation_button(title_text):
            continue

        raw_page_digits = "".join(character for character in table_cells[1].get_text(strip=True) if character.isdigit())
        page_number = int(raw_page_digits) if raw_page_digits else None

        section_order += 1

        is_parent_section = False
        if page_number is None:
            is_parent_section = True
        elif PARENT_SECTION_PATTERN.search(title_text) and not title_text.startswith("-"):
            is_parent_section = True

        if is_parent_section:
            current_parent_section = {
                "page_number": page_number,
                "level": 1,
                "title": title_text,
                "section_order": section_order,
                "children": [],
            }
            headings_tree.append(current_parent_section)
        else:
            child_node = {
                "page_number": page_number,
                "level": 2 if current_parent_section is not None else 1,
                "title": title_text,
                "section_order": section_order,
                "children": [],
            }
            if current_parent_section is not None:
                current_parent_section["children"].append(child_node)
            else:
                headings_tree.append(child_node)

    return headings_tree, section_order


def build_page_to_heading_mapping(headings_tree: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flattened_entries: List[Dict[str, Any]] = []

    for parent_item in headings_tree:
        parent_title = parent_item["title"]
        parent_page = parent_item.get("page_number")
        parent_order = parent_item.get("section_order", 1)

        if parent_page is not None:
            flattened_entries.append({
                "child_id": f"child_{parent_order}",
                "section": parent_title,
                "parent_section_id": f"sec_{parent_order}",
                "child_path": parent_title,
                "headings": [parent_title],
                "page_number": parent_page,
                "bab": parent_title if any(keyword in parent_title for keyword in ["باب", "درس"]) else None,
            })

        for child_item in parent_item.get("children", []):
            child_title = child_item["title"]
            child_page = child_item.get("page_number")
            child_order = child_item.get("section_order", 1)
            full_path = f"{parent_title} / {child_title}" if parent_title else child_title
            combined_headings = [parent_title, child_title] if parent_title else [child_title]

            bab_name = None
            if parent_title and any(keyword in parent_title for keyword in ["باب", "درس"]):
                bab_name = parent_title
            elif any(keyword in child_title for keyword in ["باب", "درس"]):
                bab_name = child_title

            if child_page is not None:
                flattened_entries.append({
                    "child_id": f"child_{child_order}",
                    "section": child_title,
                    "parent_section_id": f"sec_{parent_order}",
                    "child_path": full_path,
                    "headings": combined_headings,
                    "page_number": child_page,
                    "bab": bab_name,
                })

    flattened_entries.sort(key=lambda item: item["page_number"])
    return flattened_entries


def find_matching_heading_entry(
    page_number: int,
    sorted_page_mappings: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not sorted_page_mappings:
        return None

    matched_entry: Optional[Dict[str, Any]] = None
    for candidate in sorted_page_mappings:
        if candidate["page_number"] <= page_number:
            matched_entry = candidate
        else:
            break

    return matched_entry


# Aliases for backward compatibility
extract_table_of_contents_tree = extract_headings_tree
build_page_to_toc_mapping = build_page_to_heading_mapping
find_matching_toc_entry = find_matching_heading_entry


# Aliases for backward compatibility
book_info = extract_book_info
toc_links = extract_toc_links
page_content = extract_page_content
