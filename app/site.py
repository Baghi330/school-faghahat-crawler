import asyncio
import logging
import re
import urllib.robotparser
from collections import deque
from typing import Dict, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BOOK_PATH_PATTERN = re.compile(r"^/(\d+)(?:/(\d+))?(?:/(\d+))?/?$")


class SiteClient:
    def __init__(self) -> None:
        self.http_client = httpx.AsyncClient(
            timeout=settings.REQUEST_TIMEOUT,
            headers={"User-Agent": settings.USER_AGENT},
            follow_redirects=True,
        )
        self.last_request_timestamp: float = 0.0
        self.robot_parser: Optional[urllib.robotparser.RobotFileParser] = None
        self.has_loaded_robots_txt: bool = False

    async def close(self) -> None:
        await self.http_client.aclose()

    async def fetch_html(self, url: str) -> str:
        if settings.IS_RESPECT_ROBOTS and not self.has_loaded_robots_txt:
            await self._load_robots_txt()

        if self.robot_parser and not self.robot_parser.can_fetch(settings.USER_AGENT, url):
            raise PermissionError(f"Robots.txt prohibits fetching URL: {url}")

        for attempt_index in range(settings.MAX_RETRIES):
            try:
                time_since_last_request = asyncio.get_running_loop().time() - self.last_request_timestamp
                required_delay = settings.REQUEST_DELAY - time_since_last_request
                if required_delay > 0:
                    await asyncio.sleep(required_delay)

                response = await self.http_client.get(url)
                self.last_request_timestamp = asyncio.get_running_loop().time()

                if response.status_code in (429, 500, 502, 503, 504):
                    backoff_seconds = 2 ** attempt_index
                    logger.warning(
                        "Received status %s for %s. Retrying in %s seconds...",
                        response.status_code,
                        url,
                        backoff_seconds,
                    )
                    await asyncio.sleep(backoff_seconds)
                    continue

                response.raise_for_status()
                return response.text

            except httpx.HTTPError as http_error:
                if attempt_index + 1 == settings.MAX_RETRIES:
                    logger.error("Request failed after %s retries: %s", settings.MAX_RETRIES, url)
                    raise
                backoff_seconds = 2 ** attempt_index
                logger.warning("HTTP error on %s: %s. Retrying in %s seconds...", url, http_error, backoff_seconds)
                await asyncio.sleep(backoff_seconds)

        raise RuntimeError(f"Request permanently failed for URL: {url}")

    async def _load_robots_txt(self) -> None:
        self.has_loaded_robots_txt = True
        robots_url = urljoin(settings.BASE_URL, "/robots.txt")
        try:
            response = await self.http_client.get(robots_url)
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            self.robot_parser = parser
        except Exception as error:
            logger.warning("Could not fetch robots.txt (%s). Continuing without it.", error)
            self.robot_parser = None

    async def discover_books(self) -> Dict[str, str]:
        all_books_url = urljoin(settings.BASE_URL, "/all")
        discovery_queue: deque = deque([all_books_url, settings.START_URL])
        seen_urls = set()
        discovered_books: Dict[str, str] = {}
        base_network_location = urlparse(settings.BASE_URL).netloc

        from bs4 import BeautifulSoup

        processed_count = 0
        while discovery_queue and processed_count < settings.MAX_DISCOVERY_PAGES:
            current_url = discovery_queue.popleft()
            if current_url in seen_urls:
                continue
            seen_urls.add(current_url)
            processed_count += 1

            try:
                page_html = await self.fetch_html(current_url)
            except Exception as error:
                logger.debug("Failed to fetch discovery page %s: %s", current_url, error)
                continue

            soup = BeautifulSoup(page_html, "lxml")
            for anchor_element in soup.select("a[href]"):
                relative_or_absolute_href = anchor_element.get("href", "").strip()
                absolute_url = urljoin(current_url, relative_or_absolute_href)
                parsed_url = urlparse(absolute_url)

                if parsed_url.netloc and parsed_url.netloc != base_network_location:
                    continue

                path_match = BOOK_PATH_PATTERN.match(parsed_url.path)
                if path_match:
                    source_id, volume, page = path_match.groups()
                    if source_id and (not volume or (volume == "1" and page == "0")):
                        standard_book_url = urljoin(settings.BASE_URL, f"/{source_id}/1/0")
                        discovered_books.setdefault(source_id, standard_book_url)
                elif (
                    absolute_url.startswith(settings.BASE_URL)
                    and absolute_url not in seen_urls
                    and len(discovery_queue) < 5000
                ):
                    discovery_queue.append(absolute_url)

        return discovered_books

    CATEGORY_ALIASES = {
        "تاریخ": "تاریخ_و_جغرافیا/تاریخ",
        "دفاع_مقدس": "تاریخ_و_جغرافیا/دفاع_مقدس",
        "سفرنامه_ها": "تاریخ_و_جغرافیا/سفرنامه_ها",
        "سفرنامه‌ها": "تاریخ_و_جغرافیا/سفرنامه_ها",
        "زندگینامه": "تاریخ_و_جغرافیا/زندگینامه",
        "جغرافیا": "تاریخ_و_جغرافیا/جغرافیا",
    }

    async def discover_category_books(
        self, category_or_url: str, sub_category: Optional[str] = None
    ) -> Dict[str, str]:
        cleaned_input = unquote(category_or_url.strip())
        normalized_check = cleaned_input.replace(" ", "_")
        if normalized_check in self.CATEGORY_ALIASES and not sub_category:
            cleaned_input = self.CATEGORY_ALIASES[normalized_check]

        if sub_category:
            cleaned_sub = unquote(sub_category.strip()).replace(" ", "_")
            if cleaned_input.startswith("http://") or cleaned_input.startswith("https://"):
                target_url = f"{cleaned_input.rstrip('/')}/{cleaned_sub}"
            else:
                cleaned_main = cleaned_input.strip("/").replace(" ", "_")
                target_url = f"{settings.BASE_URL.rstrip('/')}/{cleaned_main}/{cleaned_sub}"
        elif cleaned_input.startswith("http://") or cleaned_input.startswith("https://"):
            target_url = cleaned_input
        elif "/" in cleaned_input:
            cleaned_path = cleaned_input.strip("/").replace(" ", "_")
            target_url = f"{settings.BASE_URL.rstrip('/')}/{cleaned_path}"
        else:
            normalized_name = cleaned_input.replace(" ", "_")
            target_url = f"{settings.BASE_URL.rstrip('/')}/{normalized_name}/all"

        encoded_target_url = quote(target_url, safe=":/?#[]@!$&'()*+,;=")
        logger.info("Fetching category page: %s", target_url)

        page_html = await self.fetch_html(encoded_target_url)

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page_html, "lxml")
        category_books: Dict[str, str] = {}

        for anchor_element in soup.select("a[href]"):
            raw_href = anchor_element.get("href", "").strip()
            parsed_path = urlparse(raw_href).path
            path_match = BOOK_PATH_PATTERN.match(parsed_path)
            if path_match:
                source_id, volume, page = path_match.groups()
                if source_id and (not volume or (volume == "1" and page == "0")):
                    standard_book_url = f"{settings.BASE_URL.rstrip('/')}/{source_id}/1/0"
                    category_books.setdefault(source_id, standard_book_url)

        if not category_books and target_url.endswith("/all"):
            fallback_url = target_url[:-4]
            if fallback_url != settings.BASE_URL.rstrip("/"):
                logger.info("Retrying category discovery without /all: %s", fallback_url)
                encoded_fallback_url = quote(fallback_url, safe=":/?#[]@!$&'()*+,;=")
                fallback_html = await self.fetch_html(encoded_fallback_url)
                fallback_soup = BeautifulSoup(fallback_html, "lxml")
                for anchor_element in fallback_soup.select("a[href]"):
                    raw_href = anchor_element.get("href", "").strip()
                    parsed_path = urlparse(raw_href).path
                    path_match = BOOK_PATH_PATTERN.match(parsed_path)
                    if path_match:
                        source_id, volume, page = path_match.groups()
                        if source_id and (not volume or (volume == "1" and page == "0")):
                            standard_book_url = f"{settings.BASE_URL.rstrip('/')}/{source_id}/1/0"
                            category_books.setdefault(source_id, standard_book_url)

        return category_books

    @staticmethod
    def build_page_url(source_book_id: str, volume_number: int, page_number: int) -> str:
        return urljoin(settings.BASE_URL, f"/{source_book_id}/{volume_number}/{page_number}")

    # Aliases for compatibility
    async def get(self, url: str) -> str:
        return await self.fetch_html(url)

    async def discover(self) -> Dict[str, str]:
        return await self.discover_books()

    @staticmethod
    def page_url(source: str, volume: int, page: int) -> str:
        return SiteClient.build_page_url(source, volume, page)


Site = SiteClient
