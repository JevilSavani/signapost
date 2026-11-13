"""Content sanitization and prompt injection defense utilities."""

import re

SUSPICIOUS_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"system\s*:\s*",
    r"you\s+are\s+now\s+in\s+developer\s+mode",
    r"output\s+the\s+system\s+prompt",
    r"<\|\s*im_start\s*\|>",
    r"<\|\s*endoftext\s*\|>",
]


def sanitize_untrusted_web_content(text: str) -> str:
    """Sanitize scraped web content to neutralize prompt injection vectors."""
    cleaned = text or ""
    for pattern in SUSPICIOUS_PATTERNS:
        cleaned = re.sub(pattern, "[BLOCKED_INJECTION_PATTERN]", cleaned, flags=re.IGNORECASE)
    # Collapse multiple whitespaces and control characters
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned[:1000]  # Bounded length to prevent token-exhaustion DOS
