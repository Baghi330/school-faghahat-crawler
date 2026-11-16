from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    BASE_URL: str = "https://lib.eshia.ir"
    START_URL: str = "https://lib.eshia.ir/"
    USER_AGENT: str = "ResearchCrawler/1.0 (academic research crawler)"
    REQUEST_TIMEOUT: int = 30
    REQUEST_DELAY: float = 1.0
    MAX_RETRIES: int = 4
    MAX_DISCOVERY_PAGES: int = 500
    MAX_PAGES_PER_VOLUME: int = 20000
    IS_RESPECT_ROBOTS: bool = Field(
        default=True,
        validation_alias=AliasChoices("IS_RESPECT_ROBOTS", "RESPECT_ROBOTS"),
    )

    CONTENT_SELECTORS: str = "#book-page-content, .book-page-show, #contents_cover, #contents"
    TOC_SELECTORS: str = ".toc, #toc, .book-index, .index-content"
    REMOVE_SELECTORS: str = ".sticky-menue, .tools, .Book_Title, .VolumeSelector, script, style, form, input, button, select, iframe, header, footer, nav, .pagination, .advertisement, .ads"

    CHUNK_TARGET_TOKENS: int = 450
    CHUNK_MAX_TOKENS: int = 650
    CHUNK_OVERLAP_TOKENS: int = 60

    SQLITE_PATH: str = "data/crawler.sqlite3"
    OUTPUT_DIR: str = "output"
    IS_CRAWL_ALL_ENABLED: bool = Field(
        default=False,
        validation_alias=AliasChoices("IS_CRAWL_ALL_ENABLED", "CRAWL_ALL_ENABLED"),
    )
    LOG_LEVEL: str = "INFO"


settings = Settings()
