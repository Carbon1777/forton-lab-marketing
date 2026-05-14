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


# ============================================================
# Phase 1.5 fixtures — PTB Application / CallbackQuery mocks
# ============================================================

import datetime as _dt_15
from unittest.mock import AsyncMock as _AsyncMock_15, MagicMock as _MagicMock_15


@pytest.fixture
def mock_owner_id() -> int:
    """Synthetic Telegram chat_id used as owner across handler tests."""
    return 12345


@pytest.fixture
def sample_plan_text_30days() -> str:
    """Valid 30-day plan со status=draft (covers 5 ISO weeks of June 2026)."""
    return (FIXTURES_DIR / "sample_plan_2026-06.md").read_text(encoding="utf-8")


@pytest.fixture
def mock_query(mock_owner_id):
    """Construct a minimal CallbackQuery for handler tests.

    Defaults to data='approve:deadbeef' — tests override via `mock_query.data = ...`.
    All TG-side I/O methods are AsyncMock (PTB 21.x is async-only).

    NOTE: PTB ``telegram.Message`` is a frozen dataclass-style object that
    forbids attribute assignment, so ``query.message`` is a plain ``MagicMock``
    (not a real Message instance) — handlers only touch ``query.message.reply_text``
    which is provided as an AsyncMock.
    """
    from telegram import CallbackQuery, User

    user = User(id=mock_owner_id, first_name="Forton", is_bot=False)
    msg = _MagicMock_15()
    msg.reply_text = _AsyncMock_15()
    msg.message_id = 999

    q = _MagicMock_15(spec=CallbackQuery)
    q.from_user = user
    q.message = msg
    q.id = "test_callback_id"
    q.data = "approve:deadbeef"
    q.answer = _AsyncMock_15(return_value=True)
    q.edit_message_text = _AsyncMock_15()
    q.edit_message_reply_markup = _AsyncMock_15()
    return q


@pytest.fixture
def mock_ctx(mock_owner_id, tmp_path):
    """ContextTypes-like object with bot_data + stop_running mock.

    bot_data['repo_root'] points to tmp_path so handlers writing files
    don't pollute the real working tree. Handlers needing a plan file
    should create one under tmp_path / 'plans' / f'monthly_plan_{...}.md'.
    """
    ctx = _MagicMock_15()
    ctx.application = _MagicMock_15()
    ctx.application.bot_data = {
        "owner_chat_id": mock_owner_id,
        "repo_root": tmp_path,
    }
    ctx.application.stop_running = _MagicMock_15()
    return ctx


@pytest.fixture
def tmp_repo_with_draft_plan(tmp_repo, sample_plan_text_30days):
    """Extends tmp_repo by writing sample plan as plans/monthly_plan_2026-06.md.
    Used by approve/reject/sha-verify tests."""
    plan = tmp_repo / "plans" / "monthly_plan_2026-06.md"
    plan.write_text(sample_plan_text_30days, encoding="utf-8")
    return tmp_repo, plan


# ============================================================
# Phase 2 fixtures — preview_bot / daily_post_generator / dzen_verify
# ============================================================

from unittest.mock import AsyncMock as _AsyncMock_p2, MagicMock as _MagicMock_p2, patch as _patch_p2


@pytest.fixture
def tmp_drafts_dir(tmp_path):
    """Path to drafts/ inside tmp_path (created on first use)."""
    d = tmp_path / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mock_bot_send():
    """Patch PTB Bot.send_photo / send_video / send_message / send_media_group /
    edit_message_reply_markup as AsyncMock instances. Returns dict for assertions.

    Each send_* mock returns MagicMock(message_id=N) where N differs per type
    (photo=1001, video=1002, message=1003, media_group=[(1004,)]) so tests can
    verify which API was called via message_id.
    """
    with _patch_p2("telegram.Bot.send_photo", new_callable=_AsyncMock_p2) as mp, \
         _patch_p2("telegram.Bot.send_video", new_callable=_AsyncMock_p2) as mv, \
         _patch_p2("telegram.Bot.send_message", new_callable=_AsyncMock_p2) as mm, \
         _patch_p2("telegram.Bot.send_media_group", new_callable=_AsyncMock_p2) as mg, \
         _patch_p2("telegram.Bot.edit_message_reply_markup", new_callable=_AsyncMock_p2) as me:
        mp.return_value = _MagicMock_p2(message_id=1001)
        mv.return_value = _MagicMock_p2(message_id=1002)
        mm.return_value = _MagicMock_p2(message_id=1003)
        mg.return_value = [_MagicMock_p2(message_id=1004), _MagicMock_p2(message_id=1005)]
        me.return_value = _MagicMock_p2(message_id=1003)
        yield {
            "photo": mp,
            "video": mv,
            "message": mm,
            "media_group": mg,
            "edit_reply_markup": me,
        }


@pytest.fixture
def mock_anthropic_regen():
    """Patch daily_post_generator.generate to return canned regen output.

    Used by Plan 02 tests for regen_one(); returns (text, in_tokens, out_tokens).
    Tests can override via `mock_anthropic_regen.return_value = (custom, 800, 400)`.

    NOTE: imports daily_post_generator lazily — module not yet exists in Wave 0,
    Plan 02 creates it. Fixture only resolves at test-call time.
    """
    with _patch_p2("src.daily_post_generator.generate", create=True) as gen:
        gen.return_value = ("Новый текст после правки. centryweb.ru", 800, 400)
        yield gen


@pytest.fixture
def long_caption_draft(tmp_path):
    """Fixture for T-2-08 — body 1500+ chars > 1024 caption limit triggers split fallback.

    Returns tuple (draft_path, image_path) — frontmatter draft with image: field
    and a fake 100-byte PNG.
    """
    import frontmatter as _fm
    body = "Слово недели «как бы». " * 60   # ≈ 1380 chars
    if len(body) < 1100:
        body += " centryweb.ru " * 5
    assert len(body) > 1024, f"body too short for T-2-08 fixture: {len(body)} chars"

    image_path = tmp_path / "assets" / "long_caption_test.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    draft_path = tmp_path / "drafts" / "diktum-long-caption.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    post = _fm.Post(
        content=body,
        slug="diktum-long-caption",
        channels=["tg", "vk"],
        product="diktum",
        rubric="words_of_week",
        media=[{"path": "assets/long_caption_test.png", "sha256": "deadbeef", "role": "image"}],
        image="assets/long_caption_test.png",
        generated_at="2026-06-15T09:00:00Z",
        status="draft",
        daily_regen_count=0,
        plan_date="2026-06-15",
    )
    draft_path.write_text(_fm.dumps(post), encoding="utf-8")
    return draft_path, image_path


@pytest.fixture
def short_caption_draft(tmp_path):
    """Counterpart fixture: body < 1024 chars triggers normal send_photo with caption."""
    import frontmatter as _fm
    body = "Утренняя подборка кафе. centryweb.ru"
    image_path = tmp_path / "assets" / "short_caption_test.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    draft_path = tmp_path / "drafts" / "centry-short-caption.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    post = _fm.Post(
        content=body,
        slug="centry-short-caption",
        channels=["tg", "vk"],
        product="centry",
        rubric="city_picks",
        media=[{"path": "assets/short_caption_test.png", "sha256": "cafe1234", "role": "image"}],
        image="assets/short_caption_test.png",
        generated_at="2026-06-15T09:00:00Z",
        status="draft",
        daily_regen_count=0,
        plan_date="2026-06-15",
    )
    draft_path.write_text(_fm.dumps(post), encoding="utf-8")
    return draft_path, image_path


@pytest.fixture
def multi_entry_plan(fixtures_dir, tmp_path):
    """Reads sample_plan_2026-06-multi.md (3 entries on 2026-06-15 + 1+1).

    Writes copy to tmp_path/plans/monthly_plan_2026-06.md. Returns (plan_path, fixture_text).
    """
    fixture_text = (fixtures_dir / "sample_plan_2026-06-multi.md").read_text(encoding="utf-8")
    plan_path = tmp_path / "plans" / "monthly_plan_2026-06.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(fixture_text, encoding="utf-8")
    return plan_path, fixture_text


# ============================================================
# Phase 11 fixtures — AI-talent pipeline (Plans 02-06)
# ============================================================
#
# Naming convention: Phase 11 fixtures end in `_for_<consumer>` to avoid
# clashing with the pre-existing Phase 1 `mock_anthropic_client` fixture
# (which returns a (client, msg) tuple). Phase 11 fixtures return a
# bare MagicMock client pre-wired with the expected response shape.


@pytest.fixture
def tmp_spend_file(tmp_path: Path) -> Path:
    """Empty v3-schema spend file for BOOT-01 tests.

    Schema mirrors the production file at .metrics/api_spend.json — same
    `_schema_version: 3`, same `caps.by_provider_monthly_usd` keys.
    """
    p = tmp_path / "api_spend.json"
    p.write_text(json.dumps({
        "_schema_version": 3,
        "_updated": "2026-05-11T00:00:00+00:00",
        "regen_limit_per_month": 3,
        "caps": {
            "monthly_abort_usd": 15.0,
            "daily_usd": 3.0,
            "by_provider_monthly_usd": {
                "anthropic": 5.0,
                "replicate": 4.0,
                "elevenlabs": 5.0,
                "ltx": 6.0,
            },
        },
    }, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def mock_character_yaml(tmp_path: Path) -> Path:
    """Synthetic character.yaml with lora.status + voice.status both `ready`.

    Mirrors the locked production layout after Phase 10-05 voice selection.
    Plans 02-06 read this to gate stages — fixture lets them run in tests
    without depending on the real ai_talent/character.yaml.
    """
    import yaml as _yaml
    p = tmp_path / "character.yaml"
    p.write_text(_yaml.safe_dump({
        "schema_version": 1,
        "character_id": "test-mascot",
        "phase_8": {"status": "approved", "character_card": "A test character."},
        "lora": {
            "status": "ready",
            "model": "carbon1777/forton-lab-character-v1",
            "version_sha256": "5d950b9d" + "0" * 56,
            "trigger_word": "OHWX_FORTONA",
        },
        "voice": {
            "status": "ready",
            "provider": "elevenlabs",
            "voice_id": "GN4wbsbejSnGSa1AzjH5",
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "centry": {"stability": 0.4, "similarity_boost": 0.75, "style": 0.0},
                "diktum": {"stability": 0.7, "similarity_boost": 0.75, "style": 0.0},
            },
        },
    }, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def mock_anthropic_client_for_script():
    """MagicMock matching anthropic.Anthropic for Plan 02 script_builder.

    Default response is a `tool_use` content block whose `input` matches the
    locked script.json shape (frames / voice_lines / cuts / hook / product /
    series_flag). Tests can override before invoking the generator:

        client.messages.create.return_value = my_custom_msg
    """
    client = MagicMock()
    tool_use_block = MagicMock(type="tool_use", input={
        "frames": [
            {"prompt": "OHWX_FORTONA neutral pose, soft light",
             "duration_sec": 4, "is_hero": False},
            {"prompt": "OHWX_FORTONA gentle smile, warm cinematic",
             "duration_sec": 5, "is_hero": True},
        ],
        "voice_lines": [{"text": "Привет.", "product": "centry"}],
        "cuts": [],
        "hook": {"text": "Хочешь жить?", "duration_sec": 3.0},
        "product": "centry",
        "series_flag": False,
    })
    msg = MagicMock(
        content=[tool_use_block],
        stop_reason="tool_use",
        usage=MagicMock(input_tokens=2000, output_tokens=800),
    )
    client.messages.create.return_value = msg
    return client


# Minimal 1×1 PNG bytes — valid container, deterministic, used for fixtures
# that need to return "PNG-shaped" bytes without depending on Pillow at
# fixture-collection time.
_PNG_1X1: bytes = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc"
    b"\x0f\x00\x00\x01\x01\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


@pytest.fixture
def mock_replicate_client_for_frames():
    """MagicMock matching replicate.Client for Plan 03 frame_renderer.

    `.run(...)` returns an iterable of file-like outputs (matches the real
    replicate SDK ≥1.0 streaming API). Each item exposes `.read() -> bytes`
    so the renderer can write the PNG directly to disk without a network
    round-trip.
    """
    client = MagicMock()
    output_item = MagicMock()
    output_item.read.return_value = _PNG_1X1
    client.run.return_value = [output_item]
    return client


@pytest.fixture
def mock_elevenlabs_client_for_synthesis():
    """MagicMock matching elevenlabs.client.ElevenLabs for Plan 05 voice_synth.

    Returns a client with `.text_to_speech.convert(...)` yielding chunks of
    audio bytes. Tests can patch in a `convert_with_timestamps` variant if
    Q-ELEVEN-TS resolves to "Option A".
    """
    client = MagicMock()
    # Default: convert() returns an iterator of mp3 byte chunks
    client.text_to_speech.convert.return_value = iter([b"ID3" + b"\x00" * 64])
    return client


@pytest.fixture
def mock_ltx_response():
    """Synthetic MP4 byte payload (just header bytes, not playable) for LTX tests."""
    # Minimal ftyp box header — recognised as MP4 by ffprobe for shape tests
    return (
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2mp41"
        + b"\x00" * 256
    )


@pytest.fixture
def sample_brief_md(tmp_path: Path) -> Path:
    """Materialise a minimal valid brief.md for Plan 02 + Plan 06 smoke tests."""
    brief = tmp_path / "brief.md"
    brief.write_text(
        "---\n"
        "product: centry\n"
        "topic: \"Утренний кофе в выходной\"\n"
        "hook: \"Подобрали кафе\"\n"
        "cta: \"Ищи на centryweb.ru\"\n"
        "series_flag: false\n"
        "series: null\n"
        "episode: null\n"
        "ltx_density: B\n"
        "duration_target_sec: 30\n"
        "---\n"
        "\n"
        "Тёплый утренний свет, городские кафе, мягкая интонация.\n",
        encoding="utf-8",
    )
    return brief


# ============================================================
# Phase 5 fixtures — store_metrics test infrastructure
# ============================================================

from unittest.mock import patch as _patch_p5


@pytest.fixture
def fixture_store_metrics_dir() -> Path:
    """Path to tests/fixtures/store_metrics/ (23 files for Phase 5)."""
    p = FIXTURES_DIR / "store_metrics"
    assert p.exists(), f"missing Phase 5 fixtures dir: {p}"
    return p


@pytest.fixture
def mock_anthropic_haiku():
    """MagicMock for anthropic.Anthropic() returning canned Haiku output.

    Returns: (fake_client, fake_msg). Tests mutate fake_msg.content[0].text
    to test different scenarios.
    """
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(type="text", text='{"insights":["test insight 1","test insight 2"]}')]
    fake_msg.stop_reason = "end_turn"
    fake_msg.usage = MagicMock(input_tokens=300, output_tokens=80)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg
    return fake_client, fake_msg


@pytest.fixture
def mock_gcs_bucket():
    """Patch google.cloud.storage.Client for GPlay GCS installs CSV.

    Returns: the patched mock. Use mock.return_value.bucket().blob().download_as_bytes
    to inject test data. Default blob.exists() = True, default content = UTF-16 BOM + header.
    """
    with _patch_p5("src.store_metrics._play.storage.Client", create=True) as m:
        client = MagicMock()
        bucket = MagicMock()
        blob = MagicMock()
        blob.exists.return_value = True
        blob.download_as_bytes.return_value = b'\xff\xfeDate,Package Name,Country,Daily Device Installs\n'
        bucket.blob.return_value = blob
        client.bucket.return_value = bucket
        m.return_value = client
        yield m


@pytest.fixture
def mock_play_service():
    """Patch googleapiclient.discovery.build('androidpublisher', ...).

    Returns: the patched mock; default response has 2 reviews.
    """
    with _patch_p5("src.store_metrics._play.build", create=True) as m:
        service = MagicMock()
        reviews_list = MagicMock()
        reviews_list.execute.return_value = {
            "reviews": [
                {"comments": [{"userComment": {"starRating": 5, "text": "Хорошо", "reviewerLanguage": "ru"}}]},
                {"comments": [{"userComment": {"starRating": 4, "text": "Норм", "reviewerLanguage": "ru"}}]},
            ],
        }
        service.reviews().list.return_value = reviews_list
        m.return_value = service
        yield m
