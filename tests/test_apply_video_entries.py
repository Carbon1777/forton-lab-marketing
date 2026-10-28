from pathlib import Path

from src.apply_video_entries import apply
from src.plan_reader import parse_plan

PLAN = "---\nmonth: 2026-11\ngenerated_at: 2026-11-01T07:00:00Z\n---\n\n# План\n"
ENTRY = """## 2026-11-02

```yaml
slug: demo-video
channels:
- tg
product: listvia
rubric: promo_video
verbatim: true
media:
- path: assets/video/demo.mp4
  sha256: abc
  role: video
status: draft
publish_at_msk: '20:00'
```

Текст.
"""


def _setup(tmp_path: Path):
    plans = tmp_path / "plans"
    entries = plans / "video_entries"
    entries.mkdir(parents=True)
    (plans / "monthly_plan_2026-11.md").write_text(PLAN, encoding="utf-8")
    (entries / "2026-11-02_demo-video.md").write_text(ENTRY, encoding="utf-8")
    (entries / "2026-12-02_other.md").write_text(ENTRY.replace("demo-video", "other"), encoding="utf-8")
    return plans, entries


def test_apply_adds_only_month_entries_and_is_idempotent(tmp_path):
    plans, entries = _setup(tmp_path)
    assert apply("2026-11", plans, entries) == ["demo-video"]
    assert apply("2026-11", plans, entries) == []
    plan = parse_plan(plans / "monthly_plan_2026-11.md")
    assert [e.slug for e in plan.entries] == ["demo-video"]
