from __future__ import annotations

from pathlib import Path


def test_explanation_doc_exists_and_covers_core_topics():
    doc = Path("explanation.md")
    assert doc.exists(), "explanation.md should exist at repo root"
    text = doc.read_text(encoding="utf-8")

    required_fragments = [
        "PlaywrightRuntime",
        "InMemoryRuntime",
        "OWASP Juice Shop",
        "ANTHROPIC_API_KEY",
        "claude-opus-4-7",
        "run_agent_steps",
        "blackbox_service",
    ]
    for fragment in required_fragments:
        assert fragment in text

