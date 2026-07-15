from pathlib import Path

def test_docs_warn_experimental():
    text = Path("docs/TOKEN_CDP.md").read_text(encoding="utf-8")
    assert "paste" in text.lower()
    assert "not proven" in text.lower() or "not guaranteed" in text.lower() or "NOT guaranteed" in text
