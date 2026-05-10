"""Forton Lab — brand-safety lint.

Reads marketing-v3/.lint/forbidden_words.txt and matches text against the
three-category stop-list (names, tech stack, marketing fluff). Used by
monthly_plan_generator.py (Phase 1) and weekly post regenerators (Phase 2)
BEFORE saving any draft — hard-fail on violation.

CRITICAL (Brain learnings.md 2026-05-09 + Phase 0 Plan 02 SUMMARY):
lint_post_file() MUST parse via python-frontmatter and match ONLY
post.content. Matching raw file text false-positives on `source:` field
containing AI-generated filenames like "ChatGPT Image....png".

Public API:
    Violation                     — frozen dataclass with violation details
    load_forbidden_words(path?)   — parse stop-list file (skips #/blank lines)
    categorize_words(path?)       — {entry: category} where category in
                                    {"name", "stack", "marketing"}
    lint(text, words?)            — list[Violation] sorted by position
    lint_post_file(post_path, ?)  — frontmatter-aware lint of a queue/*.md file

Match strategy (per category):
    marketing  → substring, case-insensitive (catches roots: "уникальн",
                 "революцион" inside fully-inflected forms)
    name       → whole-word boundary (no partial inside "алексеевич")
    stack      → whole-word boundary; latin-with-dot uses custom char-class
                 boundary so "next.js" matches but "Next.js!" stays a hit
                 and "Next" alone does NOT match.
    multi-token (any cat with space) → outer word-boundary on phrase
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import frontmatter

# --- Module-level constants -------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORDS_FILE = REPO_ROOT / ".lint" / "forbidden_words.txt"


# --- Public dataclass -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Violation:
    """A single brand-safety violation found in a text.

    Attributes:
        word: stop-list entry that matched (lowercase, as written in the file)
        matched_text: actual surface form found in the text (preserves case)
        position: 0-based character offset of match start in the input text
        line: 1-based line number where the match starts
        snippet: ±30 chars of context with **markdown bold** around match;
                 newlines collapsed to spaces for single-line UI display
    """

    word: str
    matched_text: str
    position: int
    line: int
    snippet: str


# --- File loading -----------------------------------------------------------


def load_forbidden_words(words_file: Path | str = DEFAULT_WORDS_FILE) -> list[str]:
    """Read the stop-list file, return entries in file order (lowercased).

    Skips blank lines and lines starting with `#` (comments / category headers).
    """
    path = Path(words_file)
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        out.append(line.lower())
    return out


# --- Categorisation ---------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*#.*===.*$")


def _classify_header(header: str) -> str:
    """Map a `# === ... ===` line to one of {"name", "stack", "marketing"}.

    Priority order:
      1. keyword «имена авторов» → "name"
      2. keyword «стек» / «технологическ» → "stack"
      3. keyword «лексика» / «маркетинг» → "marketing"
      4. fallback by parenthesised hint: "substring" → "marketing",
         "whole-word" → "stack" (covers files renamed in future phases).
    """
    h = header.lower()
    if "имена" in h or "авторов" in h:
        return "name"
    if "стек" in h or "технологическ" in h:
        return "stack"
    if "лексика" in h or "маркетинг" in h:
        return "marketing"
    if "substring" in h:
        return "marketing"
    if "whole-word" in h:
        return "stack"
    return "stack"  # safest default — never silently treat unknown as substring


def categorize_words(
    words_file: Path | str = DEFAULT_WORDS_FILE,
) -> dict[str, str]:
    """Return {entry: category} where category in {"name", "stack", "marketing"}.

    Category is the most-recent `# === ... ===` header above the entry.
    Entries before any header are classified as "stack" (safest default).
    """
    path = Path(words_file)
    out: dict[str, str] = {}
    current = "stack"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if _HEADER_RE.match(line):
                current = _classify_header(line)
            continue
        out[line.lower()] = current
    return out


# --- Match builders ---------------------------------------------------------

_CYRILLIC_ONLY_RE = re.compile(r"^[а-яёА-ЯЁ]+$")


def _build_pattern(word: str, category: str) -> re.Pattern[str]:
    """Build the regex pattern used to find `word` in text per category rules."""
    esc = re.escape(word)

    if category == "marketing":
        # Substring, case-insensitive — catches morphological inflections of
        # roots like "уникальн", "революцион" and exact phrases like
        # "лучший на рынке".
        return re.compile(esc, re.IGNORECASE)

    # name / stack
    if " " in word:
        # Multi-token phrase: boundary at outer edges only.
        return re.compile(rf"(?<!\w){esc}(?!\w)", re.IGNORECASE)

    if _CYRILLIC_ONLY_RE.fullmatch(word):
        # Pure cyrillic — Python `\b` works against `\w` (Unicode letters).
        return re.compile(rf"\b{esc}\b", re.IGNORECASE)

    # Latin word, possibly containing '.' or '-' (e.g. next.js, gpt-4).
    # Build boundary class dynamically:
    #   - if `word` itself contains '.' (next.js, gpt-4 has '-'), include
    #     that char in the boundary class so 'Next.js!' matches but
    #     'Next.jsx' does not. Otherwise plain `\b` semantics for "Supabase."
    #     (trailing period is punctuation, not part of the word).
    boundary_chars = "a-zA-Z0-9"
    if "." in word:
        boundary_chars += r"\."
    if "-" in word:
        boundary_chars += r"\-"
    return re.compile(
        rf"(?<![{boundary_chars}]){esc}(?![{boundary_chars}])",
        re.IGNORECASE,
    )


# --- Snippet ----------------------------------------------------------------


def _make_snippet(text: str, start: int, end: int, ctx: int = 30) -> str:
    """Return ±ctx chars around match with **bold** around match itself."""
    a = max(0, start - ctx)
    b = min(len(text), end + ctx)
    prefix = text[a:start]
    match = text[start:end]
    suffix = text[end:b]
    return f"{prefix}**{match}**{suffix}".replace("\n", " ")


# --- Public lint API --------------------------------------------------------


def _categorize_for_lint(words: list[str] | None) -> dict[str, str]:
    """Return {word: category} for the given words list or default stop-list.

    For an explicit `words` list (no file → no headers), defaults every entry
    to "stack" (whole-word, safest). Callers wanting substring matching for
    custom lists should pass words through `categorize_words` themselves.
    """
    if words is None:
        return categorize_words()
    return {w.lower(): "stack" for w in words}


def lint(text: str, words: list[str] | None = None) -> list[Violation]:
    """Return all brand-safety violations found in `text`.

    Empty/whitespace text returns []. All matches are returned (not just the
    first), sorted by character position for predictable editor navigation.
    """
    if not text or not text.strip():
        return []

    cats = _categorize_for_lint(words)
    violations: list[Violation] = []

    for word, category in cats.items():
        pattern = _build_pattern(word, category)
        for m in pattern.finditer(text):
            start, end = m.start(), m.end()
            violations.append(
                Violation(
                    word=word,
                    matched_text=text[start:end],
                    position=start,
                    line=text.count("\n", 0, start) + 1,
                    snippet=_make_snippet(text, start, end),
                )
            )

    violations.sort(key=lambda v: v.position)
    return violations


def lint_post_file(
    post_path: Path | str,
    words: list[str] | None = None,
) -> list[Violation]:
    """Lint the BODY of a queue/*.md (or any frontmatter-prefixed .md file).

    CRITICAL (T-1-06 / Brain learnings 2026-05-09):
    Parses the file via python-frontmatter and lints ONLY `post.content`.
    NEVER reads raw bytes; NEVER matches against the filename or frontmatter.
    Without this rule, `source: ChatGPT Image....png` in a frontmatter field
    causes a false-positive on the stack word "chatgpt".
    """
    post = frontmatter.load(str(post_path))
    return lint(post.content, words=words)
