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
    key: str                       # "diktum" / "lapulya" / "listvia"
    display: str                   # "Diktum" / "Лапуля" / "Листвия"
    appmetrica_app_id: str         # Diktum "6301663"
    onboarding_steps: list[tuple[str, str]]
    reg_source: str                # "diktum" (остальные — без Supabase-RPC)
    # raw имя экрана (AppMetrica) → человекочитаемое русское. Незамаппленные
    # экраны рендерятся как есть (raw), чтобы новый экран не пропал из отчёта.
    screen_names: dict[str, str] = field(default_factory=dict)
    # Событие AppMetrica, по которому считаем экраны. Diktum шлёт
    # "screen_view" (paramsLevel2 = строковое имя экрана). Лапуля/Листвия шлют
    # "screen_entered" (paramsLevel2 = int screen_id; screen_names мапит "<id>").
    screen_event_label: str = "screen_view"
    # Событие нативного запроса оценки (In-App Review). Заполнено только у тех
    # приложений, где механизм уже внедрён в код (шлют review_prompt_triggered).
    # None → в отчёте секция «пока не внедрён» без запроса к AppMetrica.
    review_event: str | None = None


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
class AppMetricaReviewPrompts:
    """Нативный запрос оценки (событие review_prompt_triggered).

    available=False → механизм в приложении ещё НЕ внедрён (рендер: «не внедрён»,
    без запроса к AppMetrica). available=True + devices/events None → мягкая
    деградация (ошибка запроса). devices=0 → внедрён, но показов пока не было.
    """
    available: bool
    devices: int | None = None
    events: int | None = None
    # Разбивка devices по параметру store события (App Store / Google Play /
    # RuStore / …). Пусто, если параметр ещё не отдаётся — рендер покажет только
    # суммарную строку (мягкая деградация разбивки).
    by_store: list[tuple[str, int]] = field(default_factory=list)
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

    # Блок 1 — разбивка установок по магазинам из AppMetrica (ym:ts:appInstaller).
    # Надёжный источник стор-блока: SDK пишет installer на каждой установке.
    # rows: [(человекочитаемый_магазин, число), ...] в порядке приоритета.
    am_installs_by_store: list[tuple[str, int]] = field(default_factory=list)
    am_store_error: str | None = None

    # Блок 1 (опциональная сверка) — прямые store-снапшоты ASC/Play/RuStore.
    # БОЛЬШЕ НЕ источник витрины (часто врут нулём при сбое доступа) — держим
    # только ради рейтингов и возможной ручной сверки. Рендер берёт магазины из
    # am_installs_by_store.
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

    # Блок 6б — нативный запрос оценки (In-App Review). По умолчанию «не внедрён»
    # (available=False) — так рендерятся приложения без review_event.
    review_prompts: "AppMetricaReviewPrompts" = field(
        default_factory=lambda: AppMetricaReviewPrompts(available=False)
    )

    # Блок 8 — WoW (прошлая неделя установок из снапшота)
    prev_am_installs_total: int | None = None


# Diktum воронка онбординга.
_DIKTUM_ONBOARDING: list[tuple[str, str]] = [
    ("app_open", "открыли приложение"),
    ("signup_submitted", "начали регистрацию"),
    ("signup_succeeded", "зарегистрировались"),
    ("onboarding_completed", "прошли онбординг"),
    ("record_started", "начали запись"),
    ("analysis_succeeded", "получили анализ"),
]

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

# --- Новые продукты студии (wire-события и int screen_id из их
# analytics_service.dart). Экраны шлются как screen_entered + int screen_id,
# поэтому screen_event_label="screen_entered", а screen_names мапит "<id>". ---

# Лапуля воронка активации (без сервера/аккаунта — ключевой порог = первый питомец).
_LAPULYA_ONBOARDING: list[tuple[str, str]] = [
    ("app_opened_first", "открыли приложение"),
    ("onboarding_started", "начали онбординг"),
    ("first_pet_created", "добавили первого питомца"),
    ("notif_permission_granted", "разрешили уведомления"),
    ("schedule_generated", "построили план"),
    ("home_first_shown", "дошли до ленты дел"),
]

# Лапуля: int screen_id → человекочитаемое (LapulyaScreen 1..13).
_LAPULYA_SCREEN_NAMES: dict[str, str] = {
    "1": "лента дел", "2": "питомцы", "3": "карточка питомца", "4": "ещё",
    "5": "онбординг приветствие", "6": "онбординг вид", "7": "онбординг имя",
    "8": "онбординг пол", "9": "онбординг дата рождения",
    "10": "онбординг детали", "11": "онбординг история",
    "12": "онбординг уведомления", "13": "онбординг готово",
}

# Листвия воронка активации (offline care-first, без сервера/регистрации; после
# пивота 2026-06-18 распознавание по фото убрано → ключевой порог = первое
# растение из каталога + первый отмеченный уход).
_LISTVIA_ONBOARDING: list[tuple[str, str]] = [
    ("app_opened_first", "открыли приложение"),
    ("onboarding_started", "начали онбординг"),
    ("onboarding_completed", "прошли онбординг"),
    ("first_plant_added", "добавили первое растение"),
    ("first_care_checkoff", "отметили первый уход"),
]

# Листвия: int screen_id (как строка) → человекочитаемое (ListviaScreen 1..20).
_LISTVIA_SCREEN_NAMES: dict[str, str] = {
    "1": "главная", "2": "скан", "3": "результат скана",
    "4": "диагностика болезни", "5": "результат диагностики",
    "6": "коллекция", "7": "карточка растения", "8": "замер света",
    "9": "калькулятор горшка", "10": "журнал растения", "11": "дача",
    "12": "добавление в дачу", "13": "карточка культуры", "14": "профиль",
    "15": "каталог", "16": "карточка вида", "17": "онбординг",
    "18": "подготовка данных", "19": "Pro подписка", "20": "симптомы болезни",
}


PRODUCTS: list[ProductSpec] = [
    ProductSpec(
        key="diktum",
        display="Diktum",
        appmetrica_app_id="6301663",
        onboarding_steps=_DIKTUM_ONBOARDING,
        reg_source="diktum",
        screen_names=_DIKTUM_SCREEN_NAMES,
        review_event="review_prompt_triggered",  # In-App Review внедрён (1.9.9+39)
    ),
    ProductSpec(
        key="lapulya",
        display="Лапуля",
        appmetrica_app_id="6307939",
        onboarding_steps=_LAPULYA_ONBOARDING,
        reg_source="lapulya",  # on-device, без сервера → reg-блок «данные собираются»
        screen_names=_LAPULYA_SCREEN_NAMES,
        screen_event_label="screen_entered",
    ),
    ProductSpec(
        key="listvia",
        display="Листвия",
        appmetrica_app_id="6316003",
        onboarding_steps=_LISTVIA_ONBOARDING,
        reg_source="listvia",  # offline (drift), без Supabase → reg «данные собираются»
        screen_names=_LISTVIA_SCREEN_NAMES,
        screen_event_label="screen_entered",
    ),
]
