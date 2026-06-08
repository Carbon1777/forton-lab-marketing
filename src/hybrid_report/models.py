"""Контракты гибридного per-app отчёта.

Это интерфейсы, которые потребляют gather (Task 5), render (Task 4) и cli
(Task 6). Определяются первыми как источник истины. ProductSpec — единственный
источник списка продуктов (PRODUCTS). Все dataclass — frozen.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from src.store_metrics.models import StoreSnapshot


@dataclass(frozen=True)
class ProductSpec:
    """Per-product параметры — источник истины для loop по продуктам.

    onboarding_steps — упорядоченный список (event_label, человекочитаемая_фраза)
    для воронки онбординга AppMetrica (метрика ym:ce:devices, фильтр по
    ym:ce:eventLabel). reg_source выбирает supabase_src в gather.
    """
    key: str                       # "centry" / "diktum"
    display: str                   # "Centry" / "Diktum"
    appmetrica_app_id: str         # Centry "6301660" / Diktum "6301663"
    onboarding_steps: list[tuple[str, str]]
    reg_source: str                # "centry" / "diktum"
    # raw имя экрана (AppMetrica) → человекочитаемое русское. Незамаппленные
    # экраны рендерятся как есть (raw), чтобы новый экран не пропал из отчёта.
    screen_names: dict[str, str] = field(default_factory=dict)
    # Событие AppMetrica, по которому считаем экраны. Centry/Diktum шлют
    # "screen_view" (paramsLevel2 = строковое имя экрана). Lucea/Unia/Лапуля шлют
    # "screen_entered" (paramsLevel2 = int screen_id; screen_names мапит "<id>").
    screen_event_label: str = "screen_view"


@dataclass(frozen=True)
class AppMetricaActivity:
    """Активность за период: ym:s:sessions / ym:s:users / ym:s:avgSessionDuration."""
    sessions: int | None
    active_users: int | None
    avg_session_sec: float | None


@dataclass(frozen=True)
class FunnelStep:
    """Один шаг воронки онбординга — человекочитаемая фраза + число устройств."""
    label: str
    devices: int


@dataclass(frozen=True)
class AppMetricaFunnel:
    """Воронка онбординга: шаги в порядке onboarding_steps (включая нулевые).

    Шаг максимального отвала вычисляется в render (Task 4), модель — только данные.
    error задаётся при мягкой деградации (любая ошибка любого шага).
    """
    steps: list[FunnelStep]
    error: str | None = None


@dataclass(frozen=True)
class ScreenStat:
    """Один экран: имя + заходы (+ ср. время если достанется, иначе None)."""
    name: str
    views: int
    avg_sec: float | None = None


@dataclass(frozen=True)
class AppMetricaScreens:
    """Топ-экраны по заходам. error — мягкая деградация."""
    screens: list[ScreenStat]
    error: str | None = None


@dataclass(frozen=True)
class RegActivation:
    """Регистрации → активация (Supabase). None — источник недоступен."""
    registrations: int | None
    activations: int | None


@dataclass(frozen=True)
class ProductReport:
    """Агрегат на ОДНО TG-сообщение (один продукт за одну неделю).

    Поля error per источник позволяют рендеру деградировать секцию мягко
    («данные собираются»), не роняя остальное сообщение.
    """
    spec: ProductSpec
    week_start: dt.date
    week_end: dt.date

    # Блок 1 — store-снапшоты (тип из store_metrics)
    store_snaps: list[StoreSnapshot] = field(default_factory=list)
    store_error: str | None = None

    # Блок 2 — AppMetrica installs (источник из *_funnel.appmetrica)
    am_installs_total: int | None = None
    am_installs_organic: int | None = None
    am_installs_ads: int | None = None
    am_ads_publisher: str | None = None      # имя рекламного источника («VK Ads»)
    am_installs_error: str | None = None

    # Блоки 3–5 — AppMetrica активность / воронка / экраны
    activity: AppMetricaActivity = field(
        default_factory=lambda: AppMetricaActivity(None, None, None)
    )
    funnel: AppMetricaFunnel = field(
        default_factory=lambda: AppMetricaFunnel(steps=[])
    )
    screens: AppMetricaScreens = field(
        default_factory=lambda: AppMetricaScreens(screens=[])
    )

    # Блок 6 — регистрации → активация (Supabase)
    reg: RegActivation = field(
        default_factory=lambda: RegActivation(None, None)
    )

    # Блок 8 — WoW (прошлая неделя установок из снапшота)
    prev_am_installs_total: int | None = None


# Centry воронка онбординга (события с 2026-05-26; данные тонкие ~14 устр/нед — норма).
_CENTRY_ONBOARDING: list[tuple[str, str]] = [
    ("app_open", "открыли приложение"),
    ("intro_shown", "посмотрели интро"),
    ("intro_dismissed_tap", "нажали далее на интро"),
    ("agreement_shown", "увидели соглашение"),
    ("agreement_accepted", "приняли соглашение"),
    ("permissions_shown", "увидели разрешения"),
    ("email_form_shown", "увидели форму email"),
    ("email_submitted", "отправили email"),
    ("email_confirmed", "подтвердили email"),
    ("nickname_submitted", "завершили регистрацию"),
]

# Diktum воронка онбординга.
_DIKTUM_ONBOARDING: list[tuple[str, str]] = [
    ("app_open", "открыли приложение"),
    ("signup_submitted", "начали регистрацию"),
    ("signup_succeeded", "зарегистрировались"),
    ("onboarding_completed", "прошли онбординг"),
    ("record_started", "начали запись"),
    ("analysis_succeeded", "получили анализ"),
]

# Centry: raw имя экрана AppMetrica → человекочитаемое русское.
_CENTRY_SCREEN_NAMES: dict[str, str] = {
    "welcome": "приветствие",
    "intro": "интро",
    "agreement": "соглашение",
    "permissions": "разрешения",
    "auth": "вход",
    "otp_verify": "подтверждение кода",
    "nickname": "выбор никнейма",
    "activity_feed": "лента активности",
    "plans": "планы",
    "plan_details": "детали плана",
    "places": "места",
    "profile": "профиль",
    "friends": "друзья",
    "leaderboard": "рейтинг участников",
    "private_chats_list": "список чатов",
    "private_chat": "личный чат",
}

# Diktum: ключи — go_router-пути.
_DIKTUM_SCREEN_NAMES: dict[str, str] = {
    "/auth": "вход",
    "register": "регистрация",
    "forgot-password": "восстановление пароля",
    "/onboarding-survey": "онбординг-опрос",
    "/permission-gate": "запрос разрешений",
    "/analysis/:id": "результат анализа",
    "/legal/terms": "условия использования",
    "/legal/privacy": "политика конфиденциальности",
    "/legal/child-safety": "безопасность детей",
    "/market": "магазин (тарифы)",
    "/home": "главная",
    "/record": "запись",
    "/history": "история",
    "/profile": "профиль",
    "/settings": "настройки",
}


PRODUCTS: list[ProductSpec] = [
    ProductSpec(
        key="centry",
        display="Centry",
        appmetrica_app_id="6301660",
        onboarding_steps=_CENTRY_ONBOARDING,
        reg_source="centry",
        screen_names=_CENTRY_SCREEN_NAMES,
    ),
    ProductSpec(
        key="diktum",
        display="Diktum",
        appmetrica_app_id="6301663",
        onboarding_steps=_DIKTUM_ONBOARDING,
        reg_source="diktum",
        screen_names=_DIKTUM_SCREEN_NAMES,
    ),
]
