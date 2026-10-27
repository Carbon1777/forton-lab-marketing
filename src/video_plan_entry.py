"""Добавляет видео-запись в месячный план (plans/monthly_plan_YYYY-MM.md).

Пример (ролик Листвии №1, публикация в 20:00 МСК во все каналы)::

    python -m src.video_plan_entry \\
        --video "~/Documents/Forton Lab/media/Listvia/Promo/listvia_promo_01_poliv.mp4" \\
        --slug listvia-promo-01-poliv --date 2026-10-30 --product listvia \\
        --descriptions "~/Documents/Forton Lab/media/Listvia/Promo/описания.txt" --number 1 \\
        --youtube-title "Ты поливаешь растения неправильно?" \\
        --channels tg,vk,dzen,yt,ig,fb,rutube --publish-at 20:00

Что делает: копирует видео в assets/video/<slug>.mp4, проверяет размер (≤19 МБ
для TG/Дзена), вертикаль 9:16 и длительность, считает sha256, прогоняет текст
через brand-lint и дописывает секцию ``## YYYY-MM-DD`` в план месяца.
План месяца должен уже существовать. Запись создаётся со статусом ``draft``:
preview_bot пришлёт превью в 12:00 МСК даты публикации.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from src.brand_lint import lint
from src.plan_reader import parse_plan_text, sha256_of_file

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = REPO_ROOT / "plans"
VIDEO_DIR = REPO_ROOT / "assets" / "video"
TG_DZEN_LIMIT = 19 * 1024 * 1024
DEFAULT_CHANNELS = ["tg", "vk", "dzen", "yt", "ig", "fb", "rutube"]

_BLOCK_RE = re.compile(r"^={10,}\n(\d+) · (\S+)\n={10,}\n", re.M)


def read_description_block(path: Path, number: int) -> str:
    """Блок №number из «описания.txt» (между заголовками ``NN · файл``)."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    parts = _BLOCK_RE.split(text)
    # parts = [preamble, num1, file1, body1, num2, file2, body2, ...]
    for i in range(1, len(parts) - 2, 3):
        if int(parts[i]) == number:
            body = parts[i + 2]
            body = re.split(r"\n={10,}\n", body)[0]   # отрезать «ПРИМЕЧАНИЕ»
            return body.strip()
    raise ValueError(f"блок №{number} не найден в {path}")


def probe_video(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    st = data["streams"][0]
    return {"width": int(st["width"]), "height": int(st["height"]),
            "duration": float(data["format"]["duration"])}


def validate_video(path: Path) -> list[str]:
    """Возвращает список проблем (пусто = ок)."""
    problems: list[str] = []
    size = path.stat().st_size
    if size > TG_DZEN_LIMIT:
        problems.append(f"{path.name}: {size / 1048576:.1f} МБ > 19 МБ (лимит Дзена/TG) — пережать ffmpeg")
    meta = probe_video(path)
    if meta["height"] <= meta["width"]:
        problems.append(f"{path.name}: {meta['width']}x{meta['height']} — нужна вертикаль 9:16")
    if meta["duration"] > 90:
        problems.append(f"{path.name}: {meta['duration']:.0f} c > 90 c (лимит Reels через API)")
    return problems


def build_entry_text(*, date: dt.date, slug: str, channels: list[str], product: str,
                     rubric: str, video_rel: str, sha256: str, body: str,
                     publish_at: str | None, youtube_title: str | None,
                     youtube_tags: list[str] | None) -> str:
    meta: dict = {
        "slug": slug,
        "channels": channels,
        "product": product,
        "rubric": rubric,
        "verbatim": True,
        "media": [{"path": video_rel, "sha256": sha256, "role": "video"}],
        "status": "draft",
    }
    if publish_at:
        meta["publish_at_msk"] = publish_at
    if youtube_title:
        meta["youtube_title"] = youtube_title[:100]
    if youtube_tags:
        meta["youtube_tags"] = youtube_tags
    block = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    return f"\n## {date.isoformat()}\n\n```yaml\n{block}\n```\n\n{body.strip()}\n"


def add_entry(args: argparse.Namespace) -> Path:
    date = dt.date.fromisoformat(args.date)
    plan_path = PLANS_DIR / f"monthly_plan_{date.strftime('%Y-%m')}.md"
    if not plan_path.exists():
        raise SystemExit(f"план месяца не найден: {plan_path.name} — сначала нужен план этого месяца")
    plan_text = plan_path.read_text(encoding="utf-8")
    if re.search(rf"^slug: {re.escape(args.slug)}\s*$", plan_text, re.M):
        raise SystemExit(f"slug {args.slug!r} уже есть в {plan_path.name}")

    src_video = Path(args.video).expanduser().resolve()
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEO_DIR / f"{args.slug}.mp4"
    if src_video != dest.resolve():
        shutil.copyfile(src_video, dest)
    problems = validate_video(dest)
    if problems:
        dest.unlink(missing_ok=True)
        raise SystemExit("видео не прошло проверку:\n  " + "\n  ".join(problems))

    if args.descriptions:
        body = read_description_block(Path(args.descriptions), args.number)
    else:
        body = Path(args.text_file).expanduser().read_text(encoding="utf-8").strip()
    violations = lint(body)
    if violations:
        raise SystemExit(f"brand-lint: {violations}")

    # YouTube через API берёт заголовок из frontmatter, а не из имени файла; без явного
    # youtube_title им стал бы slug (draft.title = slug) — поэтому по умолчанию берём
    # первое предложение текста.
    yt_title = args.youtube_title or re.split(r"(?<=[.!?…])\s|\n", body.strip(), maxsplit=1)[0]
    entry = build_entry_text(
        date=date, slug=args.slug, channels=args.channels.split(","),
        product=args.product, rubric=args.rubric,
        video_rel=str(dest.relative_to(REPO_ROOT)), sha256=sha256_of_file(dest), body=body,
        publish_at=args.publish_at, youtube_title=yt_title,
        youtube_tags=[t for t in (args.youtube_tags or "").split(",") if t],
    )
    new_text = plan_text.rstrip() + "\n" + entry
    parse_plan_text(new_text, plan_path)          # не ломаем план: валидируем до записи
    plan_path.write_text(new_text, encoding="utf-8")
    return plan_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="video_plan_entry", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (дата публикации)")
    ap.add_argument("--product", required=True, help="listvia | lucea | lapulya | diktum | forton-lab")
    ap.add_argument("--rubric", default="promo_video")
    ap.add_argument("--channels", default=",".join(DEFAULT_CHANNELS))
    ap.add_argument("--publish-at", default="20:00", help="HH:MM МСК; пусто = сразу после одобрения")
    ap.add_argument("--youtube-title")
    ap.add_argument("--youtube-tags", help="через запятую")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--descriptions", help="путь к описания.txt (вместе с --number)")
    src.add_argument("--text-file", help="файл с текстом поста")
    ap.add_argument("--number", type=int, help="номер блока в описания.txt")
    args = ap.parse_args(argv)
    if args.descriptions and not args.number:
        ap.error("--descriptions требует --number")
    if args.publish_at == "":
        args.publish_at = None
    path = add_entry(args)
    print(f"✓ запись {args.slug} добавлена в {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
