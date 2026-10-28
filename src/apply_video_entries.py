"""Дописывает заранее заготовленные видео-записи в план месяца.

Записи лежат в ``plans/video_entries/<YYYY-MM-DD>_<slug>.md`` (готовые секции
``## дата`` + yaml + текст; создаются ``video_plan_entry --stage``). Генератор
плана падает, если файл месяца уже существует, поэтому в план записи попадают
только после генерации: шаг в monthly_plan.yml вызывает этот модуль.
Идемпотентно: slug, уже стоящий в плане, пропускается.

    python -m src.apply_video_entries --month 2026-11
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from src.plan_reader import parse_plan_text

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = REPO_ROOT / "plans"
ENTRIES_DIR = PLANS_DIR / "video_entries"


def apply(month: str, plans_dir: Path = PLANS_DIR, entries_dir: Path = ENTRIES_DIR) -> list[str]:
    plan_path = plans_dir / f"monthly_plan_{month}.md"
    if not plan_path.exists():
        raise SystemExit(f"план {plan_path.name} не найден")
    text = plan_path.read_text(encoding="utf-8")
    added: list[str] = []
    for f in sorted(entries_dir.glob(f"{month}-*.md")):
        entry = f.read_text(encoding="utf-8")
        m = re.search(r"^slug: (\S+)\s*$", entry, re.M)
        if not m:
            raise SystemExit(f"{f.name}: нет slug")
        slug = m.group(1)
        if re.search(rf"^slug: {re.escape(slug)}\s*$", text, re.M):
            continue
        text = text.rstrip() + "\n\n" + entry.strip() + "\n"
        added.append(slug)
    if added:
        parse_plan_text(text, plan_path)      # не ломаем план: валидируем до записи
        plan_path.write_text(text, encoding="utf-8")
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apply_video_entries", description=__doc__)
    ap.add_argument("--month", required=True, help="YYYY-MM")
    args = ap.parse_args(argv)
    added = apply(args.month)
    print(f"✓ добавлено {len(added)}: {', '.join(added) or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
