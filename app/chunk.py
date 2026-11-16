from typing import List, Optional

from app.config import settings

try:
    import tiktoken

    TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:
    TOKEN_ENCODER = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if TOKEN_ENCODER is not None:
        return len(TOKEN_ENCODER.encode(text))
    return max(1, len(text.split()))


def split_text_by_token_limit(text: str, token_limit: int) -> List[str]:
    if not text:
        return []

    if TOKEN_ENCODER is not None:
        token_ids = TOKEN_ENCODER.encode(text)
        chunks: List[str] = []
        for index in range(0, len(token_ids), token_limit):
            token_slice = token_ids[index : index + token_limit]
            decoded_text = TOKEN_ENCODER.decode(token_slice, errors="replace")
            if decoded_text.strip():
                chunks.append(decoded_text.strip())
        return chunks

    words = text.split()
    chunks = []
    for index in range(0, len(words), token_limit):
        word_slice = words[index : index + token_limit]
        chunks.append(" ".join(word_slice))
    return chunks


def chunk_page_content(paragraphs: List[str]) -> List[str]:
    normalized_units: List[str] = []
    for paragraph in paragraphs:
        if count_tokens(paragraph) > settings.CHUNK_MAX_TOKENS:
            normalized_units.extend(split_text_by_token_limit(paragraph, settings.CHUNK_MAX_TOKENS))
        else:
            normalized_units.append(paragraph)

    result_chunks: List[str] = []
    current_chunk_units: List[str] = []
    current_chunk_size = 0

    for unit in normalized_units:
        unit_token_count = count_tokens(unit)

        if current_chunk_units and (current_chunk_size + unit_token_count > settings.CHUNK_TARGET_TOKENS):
            result_chunks.append("\n\n".join(current_chunk_units))

            carry_over_units: List[str] = []
            carry_over_size = 0
            for previous_unit in reversed(current_chunk_units):
                previous_size = count_tokens(previous_unit)
                if carry_over_size + previous_size > settings.CHUNK_OVERLAP_TOKENS:
                    break
                carry_over_units.insert(0, previous_unit)
                carry_over_size += previous_size

            current_chunk_units = carry_over_units
            current_chunk_size = carry_over_size

        current_chunk_units.append(unit)
        current_chunk_size += unit_token_count

        if current_chunk_size >= settings.CHUNK_MAX_TOKENS:
            result_chunks.append("\n\n".join(current_chunk_units))
            current_chunk_units = []
            current_chunk_size = 0

    if current_chunk_units:
        result_chunks.append("\n\n".join(current_chunk_units))

    return [chunk.strip() for chunk in result_chunks if chunk.strip()]


# Aliases for backward compatibility
chunk_page = chunk_page_content
count = count_tokens
split = split_text_by_token_limit
