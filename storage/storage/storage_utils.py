import hashlib
import re


def clean_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9_]", "_", text)
    return text


def generate_cache_key(command: str, params: str) -> str:
    raw = f"{command}:{params}"
    return hashlib.sha256(raw.encode()).hexdigest()


def build_storage_path(command: str, cache_key: str, extension: str) -> str:
    command = clean_text(command)
    return f"telegram-results/{command}/{cache_key}.{extension}"
