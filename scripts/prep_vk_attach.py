"""prep_vk_attach.py — копирует медиа сегодняшнего поста в ~/Documents/vk_attach/.

Запускается локально на маке (через launchd ежедневно в 11:00 МСК — за час до
preview_bot cron), потому что бот в GH Actions не имеет доступа к локальной
файловой системе юзера.

Шаги:
    1. git pull --ff-only в marketing-v3 (sync с remote)
    2. Парсим plans/monthly_plan_<YYYY-MM>.md, берём ВСЕ entries на сегодня
    3. Для каждой записи где channels включают 'vk' и есть image:/video::
       - cp <repo_root>/<media-path> ~/Documents/vk_attach/<slug>.<ext>
    4. (опционально) Чистим vk_attach от файлов которые НЕ для сегодня
       (юзер должен был вручную удалить после публикации; cleanup на всякий)

Idempotent: можно запускать повторно — overwrites OK, no harm.
Quiet by default — пишет только actionable события (skips silent).
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # marketing-v3/
VK_ATTACH = Path.home() / "Documents" / "vk_attach"

# git pull (sync с remote перед чтением плана)
def git_pull():
    try:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only", "--quiet"],
            check=True, capture_output=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"WARN: git pull failed: {exc.stderr.decode()[:200]}\n")
    except subprocess.TimeoutExpired:
        sys.stderr.write("WARN: git pull timed out\n")


def main() -> int:
    git_pull()

    sys.path.insert(0, str(REPO_ROOT))
    from src.plan_reader import parse_plan, get_today_entries

    today = dt.date.today()
    plan_path = REPO_ROOT / "plans" / f"monthly_plan_{today:%Y-%m}.md"
    if not plan_path.exists():
        print(f"INFO: no plan for {today:%Y-%m} — nothing to prep")
        return 0

    plan = parse_plan(plan_path)
    entries = get_today_entries(plan, today)
    if not entries:
        print(f"INFO: no entries for {today.isoformat()}")
        return 0

    VK_ATTACH.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in entries:
        if "vk" not in entry.channels:
            continue
        if entry.status in ("skipped", "approved", "expired", "published"):
            continue
        if not entry.media:
            continue
        media = entry.media[0]
        src = REPO_ROOT / media.path
        if not src.exists():
            sys.stderr.write(f"WARN: media not found: {src}\n")
            continue
        ext = src.suffix
        dst = VK_ATTACH / f"{entry.slug}{ext}"
        shutil.copy2(src, dst)
        print(f"copied: {dst.name} ({src.stat().st_size // 1024} KB)")
        copied += 1

    print(f"\nDONE: {copied} файлов готовы в {VK_ATTACH}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
