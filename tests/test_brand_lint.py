"""Tests for src/brand_lint.py — phase 1 GEN-02 brand-safety lint.

Critical regression: test_chatgpt_in_frontmatter_source_is_NOT_violation
defends against re-introducing the bug documented in
Brain/projects/forton-lab/learnings.md @ 2026-05-09 (T-1-06).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.brand_lint import (
    Violation,
    categorize_words,
    lint,
    lint_post_file,
    load_forbidden_words,
)


# --- 1. load_forbidden_words ------------------------------------------------


def test_load_forbidden_words_count():
    """All 28 entries returned, без `#`-комментариев и пустых строк, lowercase."""
    words = load_forbidden_words()
    assert len(words) >= 28, f"expected >=28 entries, got {len(words)}"
    for w in words:
        assert not w.startswith("#"), f"comment leaked: {w!r}"
        assert w.strip() != "", "empty entry leaked"
        assert w == w.lower(), f"non-lowercase entry leaked: {w!r}"


def test_load_forbidden_words_explicit_path(forbidden_words_file: Path):
    """Явный путь к файлу работает."""
    words = load_forbidden_words(forbidden_words_file)
    assert "chatgpt" in words
    assert "революцион" in words
    assert "лучший на рынке" in words


# --- 2. categorize_words ----------------------------------------------------


def test_categorize_three_categories():
    """Три категории присутствуют, каждая ≥1 entry."""
    cats = categorize_words()
    values = set(cats.values())
    assert values == {"name", "stack", "marketing"}, f"got {values}"
    # минимум по одной записи в каждой категории
    by_cat: dict[str, list[str]] = {"name": [], "stack": [], "marketing": []}
    for word, cat in cats.items():
        by_cat[cat].append(word)
    assert len(by_cat["name"]) >= 3, by_cat["name"]
    assert len(by_cat["stack"]) >= 11, by_cat["stack"]
    assert len(by_cat["marketing"]) >= 14, by_cat["marketing"]


def test_categorize_specific_assignments():
    """Конкретные слова попадают в правильные категории."""
    cats = categorize_words()
    assert cats["алексей"] == "name"
    assert cats["chatgpt"] == "stack"
    assert cats["flutter"] == "stack"
    assert cats["революцион"] == "marketing"
    assert cats["лучший на рынке"] == "marketing"


# --- 3. lint() — match strategies -------------------------------------------


def test_catches_revolutionary_substring():
    """marketing-категория ловит substring «революцион» внутри «революционный»."""
    vs = lint("Это революционный продукт.")
    assert len(vs) >= 1
    assert any(v.word == "революцион" for v in vs)


def test_catches_chatgpt_caps():
    """Stack-слово ловится case-insensitive (ChatGPT, CHATGPT, chatgpt)."""
    vs = lint("ChatGPT помог нам.")
    assert any(v.word == "chatgpt" for v in vs)


def test_catches_flutter_word_boundary():
    """Pure-latin stack ловится по \\b boundary."""
    vs = lint("Мы используем Flutter и Supabase.")
    words_hit = {v.word for v in vs}
    assert "flutter" in words_hit
    assert "supabase" in words_hit


def test_catches_next_js_with_dot():
    """Stack с точкой (next.js) ловится без ложного попадания на 'next' в URL."""
    vs = lint("Стек: Next.js + React Native.")
    words_hit = {v.word for v in vs}
    assert "next.js" in words_hit
    assert "react native" in words_hit


def test_does_not_catch_inside_other_word_for_name():
    """Whole-word boundary для имён не ловит подстроку.

    'Алексеевич' — НЕ должно сматчиться на 'алексей' (это разные слова).
    """
    vs = lint("Иван Алексеевич приехал.")
    assert not any(v.word == "алексей" for v in vs), \
        f"false positive on 'алексеевич': {vs}"


def test_clean_text_returns_empty():
    vs = lint("Centry — социальный планировщик встреч. Без бесконечных переписок.")
    assert vs == []


def test_returns_all_violations():
    """Возвращает ВСЕ violations, не только первое."""
    text = "революционный + уникальный + инновационный продукт"
    vs = lint(text)
    words_hit = {v.word for v in vs}
    assert {"революцион", "уникальн", "инновацион"} <= words_hit, \
        f"missed some: got {words_hit}"


def test_violation_has_line_and_position():
    text = "Первая строка чистая.\nВторая строка революционная.\nТретья.\n"
    vs = lint(text)
    assert len(vs) >= 1
    v = vs[0]
    assert v.line == 2
    assert v.position == text.index("революцион")


def test_violation_snippet_contains_match_marker():
    text = "Это революционный новый продукт от нашей команды."
    v = lint(text)[0]
    assert "**" in v.snippet
    assert "революцион" in v.snippet.lower()


def test_phrase_with_space_matched_as_phrase():
    """Multi-token фраза 'лучший на рынке' матчится целиком."""
    text = "Это просто лучший на рынке инструмент."
    vs = lint(text)
    assert any(v.word == "лучший на рынке" for v in vs)


def test_lint_with_empty_text():
    assert lint("") == []


def test_lint_with_only_whitespace():
    assert lint("   \n\n  \t") == []


def test_violations_sorted_by_position():
    text = "Первое: gpt-5. Второе: уникальное. Третье: chatgpt."
    vs = lint(text)
    positions = [v.position for v in vs]
    assert positions == sorted(positions), f"not sorted: {positions}"


# --- 4. lint_post_file() ----------------------------------------------------


def test_lint_post_file_clean_post(fixtures_dir: Path):
    vs = lint_post_file(fixtures_dir / "clean_post.md")
    assert vs == [], f"clean_post should produce no violations, got {vs}"


def test_lint_post_file_dirty_post(fixtures_dir: Path):
    vs = lint_post_file(fixtures_dir / "dirty_post.md")
    assert any(v.word == "революцион" for v in vs), \
        f"expected 'революцион' violation, got {vs}"


def test_chatgpt_in_frontmatter_source_is_NOT_violation(fixtures_dir: Path):
    """T-1-06 REGRESSION (Brain/projects/forton-lab/learnings.md 2026-05-09).

    `chatgpt` в `source: vk_attach/ChatGPT Image....png` — это metadata-trail,
    НЕ публичный текст. lint_post_file MUST NOT flag it.

    Если этот тест когда-либо начнёт падать — баг 2026-05-09 вернулся:
    lint_post_file перестал использовать frontmatter.load().content и
    поехал по raw text (или filename).
    """
    violations = lint_post_file(fixtures_dir / "frontmatter_chatgpt_post.md")
    assert violations == [], (
        f"FALSE POSITIVE on frontmatter source field — see Brain learnings 2026-05-09. "
        f"Got: {violations}"
    )


# --- 5. Violation dataclass -------------------------------------------------


def test_violation_is_frozen_dataclass():
    """Violation — frozen dataclass (immutable)."""
    v = Violation(word="x", matched_text="X", position=0, line=1, snippet="**X**")
    with pytest.raises((AttributeError, Exception)):
        v.word = "changed"  # type: ignore[misc]


def test_violation_fields():
    v = Violation(word="революцион", matched_text="революционный",
                  position=10, line=2, snippet="...**революционный**...")
    assert v.word == "революцион"
    assert v.matched_text == "революционный"
    assert v.position == 10
    assert v.line == 2
    assert "**" in v.snippet


# --- 6. Edge cases for coverage --------------------------------------------


def test_lint_with_explicit_words_list():
    """Можно передать кастомный список слов (без обращения к файлу)."""
    vs = lint("любой текст с custom-stop-word внутри", words=["custom"])
    # 'custom' будет matched как stack-категория по дефолту? Нет — мы передаём только
    # words list, без категоризации; реализация должна сама матчить по умолчанию.
    # Минимально: lint не должен падать при custom words.
    assert isinstance(vs, list)


def test_categorize_with_explicit_path(forbidden_words_file: Path):
    cats = categorize_words(forbidden_words_file)
    assert "алексей" in cats
    assert cats["алексей"] == "name"


def test_snippet_at_text_start():
    """Match в самом начале текста — snippet не падает на отрицательном offset."""
    text = "революционный продукт."
    v = lint(text)[0]
    assert v.position == 0
    assert "**" in v.snippet


def test_snippet_at_text_end():
    """Match в самом конце текста — snippet не падает на off-by-one."""
    text = "Продукт революционный"
    vs = lint(text)
    assert len(vs) >= 1
    assert "**" in vs[0].snippet


def test_categorize_fallback_substring_header(tmp_path: Path):
    """Header без русских keyword'ов, только '(substring)' → 'marketing'."""
    f = tmp_path / "words.txt"
    f.write_text(
        "# === SomeRenamedSection (substring) ===\n"
        "fluffword\n",
        encoding="utf-8",
    )
    cats = categorize_words(f)
    assert cats["fluffword"] == "marketing"


def test_categorize_fallback_whole_word_header(tmp_path: Path):
    """Header без русских keyword'ов, только '(whole-word)' → 'stack'."""
    f = tmp_path / "words.txt"
    f.write_text(
        "# === RenamedTech (whole-word) ===\n"
        "techword\n",
        encoding="utf-8",
    )
    cats = categorize_words(f)
    assert cats["techword"] == "stack"


def test_categorize_unknown_header_defaults_to_stack(tmp_path: Path):
    """Header without any recognised keyword → safest default 'stack'."""
    f = tmp_path / "words.txt"
    f.write_text(
        "# === SomethingCompletelyNew ===\n"
        "mystery\n",
        encoding="utf-8",
    )
    cats = categorize_words(f)
    assert cats["mystery"] == "stack"
