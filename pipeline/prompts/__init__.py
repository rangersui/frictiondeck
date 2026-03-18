"""Prompt loader — all LLM-facing text lives in .md files.

Usage:
    from pipeline.prompts import load
    prompt = load("nli_verify.md")
    prompt = load("server_instructions.md")
"""

from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str, **kwargs) -> str:
    """Load prompt template and fill placeholders."""
    text = (_DIR / name).read_text(encoding="utf-8")
    if kwargs:
        text = text.format(**kwargs)
    return text
