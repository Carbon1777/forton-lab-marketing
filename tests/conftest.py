"""Pytest fixtures shared across Phase 1 tests."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent  # marketing-v3/
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def repo_root() -> Path:
    """Real marketing-v3/ root — used to resolve .lint/forbidden_words.txt."""
    return REPO_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def forbidden_words_file() -> Path:
    """Path to real Phase 0 Plan 02 forbidden_words.txt (28 entries)."""
    p = REPO_ROOT / ".lint" / "forbidden_words.txt"
    assert p.exists(), f"missing Phase 0 deliverable: {p}"
    return p


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Materialise a minimal marketing-v3 layout in tmp_path:
        .lint/forbidden_words.txt (copied from real)
        plans/
        .metrics/api_spend.json (fresh)
        assets/  media/
    """
    (tmp_path / ".lint").mkdir()
    shutil.copy(REPO_ROOT / ".lint" / "forbidden_words.txt",
                tmp_path / ".lint" / "forbidden_words.txt")
    (tmp_path / "plans").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "media").mkdir()
    metrics = tmp_path / ".metrics"
    metrics.mkdir()
    (metrics / "api_spend.json").write_text(
        json.dumps({"_schema_version": 1, "_updated": None}, indent=2)
    )
    return tmp_path


@pytest.fixture
def sample_plan_text() -> str:
    """Valid 3-day mini-plan in canonical sectional Markdown format (variant b)."""
    return (FIXTURES_DIR / "valid_plan.md").read_text(encoding="utf-8")


@pytest.fixture
def broken_plan_text() -> str:
    """Plan with malformed YAML in one section."""
    return (FIXTURES_DIR / "broken_yaml_plan.md").read_text(encoding="utf-8")


@pytest.fixture
def mock_anthropic_client():
    """MagicMock that mimics anthropic.Anthropic() return value.
    Use: client.messages.create.return_value = <fake_msg>.
    Test must set the return_value before calling generator.
    """
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(type="text", text="<set me in test>")]
    fake_msg.stop_reason = "end_turn"
    fake_msg.usage = MagicMock(input_tokens=1000, output_tokens=500)

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg
    return fake_client, fake_msg
