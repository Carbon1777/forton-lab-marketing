"""Markdown digest rendering — human-readable отчёт для TG-канала «Планировщик».

Формат:
    📊 Forton Lab — неделя <date>

    🎯 ИТОГО
       installs: +<N> (vs <prev> неделей раньше = <Δ%> <arrow>)
       4-неделя тренд: <a> → <b> → <c> → <d> <trend_arrow>

    📱 CENTRY (Δ <product%> WoW)
       App Store     +<i>  <Δ%> <arrow>   ⭐ <rating> <rating_arrow>
       Google Play   +<i>  <Δ%> <arrow>   ⭐ <rating>
       RuStore       +<i>  <Δ%> <arrow>   ⭐ <rating>
       🌍 <geo>

    📱 DIKTUM (Δ ...)
       ...

    🚨 Алерты:
       • <store/product> <Δ%> (<a> → <b>) — <hypothesis>

    💡 Что делать на этой неделе:
       <bullet hypotheses by AI-нет — пока ручной placeholder>
"""
from __future__ import annotations

from .models import ALERT_PCT, ProductReport, StoreSnapshot, WeeklyReport, WeekDelta

_STORE_NAME: dict[str, str] = {
    "app_store": "App Store",
    "google_play": "Google Play",
    "rustore": "RuStore",
}

_PRODUCT_EMOJI: dict[str, str] = {
    "centry": "📱 CENTRY",
    "diktum": "📱 DIKTUM",
}


def _fmt_int(n: int | None, sign: bool = True) -> str:
    if n is None:
        return "—"
    if sign and n >= 0:
        return f"+{n}"
    return str(n)


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "  —"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _fmt_rating(snap: StoreSnapshot) -> str:
    if snap.rating is None:
        return "⭐ —"
    return f"⭐ {snap.rating:.1f}"


def _product_total_installs(snaps: list[StoreSnapshot]) -> int | None:
    """Sum installs across stores; None если все None."""
    vals = [s.installs for s in snaps if s.installs is not None]
    return sum(vals) if vals else None


def _render_product_block(report: ProductReport) -> list[str]:
    out: list[str] = []
    total_curr = _product_total_installs(report.snapshots)
    total_prev = _product_total_installs(report.prev_snapshots)
    delta = WeekDelta.compute(total_curr, total_prev)
    header = (
        f"{_PRODUCT_EMOJI[report.product]} "
        f"(Δ {_fmt_pct(delta.delta_pct)} {delta.arrow})"
    )
    out.append(f"<b>{header}</b>")

    # Per-store rows
    prev_by_store = {s.store: s for s in report.prev_snapshots}
    for snap in sorted(report.snapshots, key=lambda s: s.store):
        prev_snap = prev_by_store.get(snap.store)
        prev_installs = prev_snap.installs if prev_snap else None
        store_delta = WeekDelta.compute(snap.installs, prev_installs)
        line = (
            f"   {_STORE_NAME[snap.store]:<12}  "
            f"{_fmt_int(snap.installs):>5}  "
            f"{_fmt_pct(store_delta.delta_pct):>6} {store_delta.arrow}   "
            f"{_fmt_rating(snap)}"
        )
        if snap.error:
            line = f"   {_STORE_NAME[snap.store]:<12}  <i>{snap.error}</i>"
        out.append(line)

    # Geo line — top country if any
    geos = [
        (s.top_country, s.top_country_share, _STORE_NAME[s.store])
        for s in report.snapshots
        if s.top_country
    ]
    if geos:
        # Use first non-None as headline; в будущем — aggregate
        country, share, store = geos[0]
        share_pct = f"{share * 100:.0f}%" if share else ""
        out.append(f"   🌍 {country} {share_pct} ({store})")

    return out


def _render_trend(report: ProductReport) -> str | None:
    if not report.trend_4w:
        return None
    pts = [p.installs for p in report.trend_4w if p.installs is not None]
    if len(pts) < 2:
        return None
    arrow = "⬆️" if pts[-1] > pts[0] else ("⬇️" if pts[-1] < pts[0] else "→")
    series = " → ".join(str(p) if p is not None else "—" for p in [p.installs for p in report.trend_4w])
    return f"   4-нед. тренд: {series} {arrow}"


def _gather_alerts(report: WeeklyReport) -> list[str]:
    """Build alert lines: any per-store delta >= ALERT_PCT triggers."""
    alerts: list[str] = list(report.overall_alerts)
    for prod in report.products:
        prev_by_store = {s.store: s for s in prod.prev_snapshots}
        for snap in prod.snapshots:
            if snap.error:
                alerts.append(
                    f"• {_STORE_NAME[snap.store]} {prod.product.upper()} "
                    f"— {snap.error}"
                )
                continue
            prev_snap = prev_by_store.get(snap.store)
            prev_installs = prev_snap.installs if prev_snap else None
            d = WeekDelta.compute(snap.installs, prev_installs)
            if d.delta_pct is None:
                continue
            if abs(d.delta_pct) >= ALERT_PCT:
                direction = "просел" if d.delta_pct < 0 else "вырос"
                alerts.append(
                    f"• {prod.product.upper()} {_STORE_NAME[snap.store]} "
                    f"{direction} {_fmt_pct(d.delta_pct)} "
                    f"({prev_installs} → {snap.installs})"
                )
        alerts.extend(f"• {prod.product.upper()}: {a}" for a in prod.alerts)
    return alerts


def render_digest(report: WeeklyReport) -> str:
    """Return HTML-formatted markdown digest для tg sendMessage с parse_mode=HTML."""
    lines: list[str] = []
    week_end = report.week_start + __import__("datetime").timedelta(days=6)
    lines.append(
        f"📊 <b>Forton Lab — неделя "
        f"{report.week_start.strftime('%d.%m')}-{week_end.strftime('%d.%m.%Y')}</b>"
    )
    lines.append("")

    # 🎯 ИТОГО — сумма по всем продуктам
    overall_curr = 0
    overall_prev = 0
    has_any = False
    for prod in report.products:
        c = _product_total_installs(prod.snapshots)
        p = _product_total_installs(prod.prev_snapshots)
        if c is not None:
            overall_curr += c
            has_any = True
        if p is not None:
            overall_prev += p
    if has_any:
        d = WeekDelta.compute(overall_curr, overall_prev if overall_prev else None)
        lines.append("<b>🎯 ИТОГО</b>")
        lines.append(
            f"   {_fmt_int(overall_curr)} установок "
            f"(было {overall_prev} = {_fmt_pct(d.delta_pct)} {d.arrow})"
        )
        lines.append("")

    # Per-product
    for prod in report.products:
        lines.extend(_render_product_block(prod))
        trend = _render_trend(prod)
        if trend:
            lines.append(trend)
        lines.append("")

    # Alerts
    alerts = _gather_alerts(report)
    if alerts:
        lines.append("<b>🚨 Алерты</b>")
        lines.extend(alerts)
        lines.append("")

    # Hypotheses (METRICS-09 / D-5-06) — Claude Haiku 4.5 insights.
    # Section rendered ONLY when non-empty; soft-fail in hypothesis.generate()
    # yields empty list → header is omitted entirely so the digest stays clean.
    if report.hypotheses:
        lines.append("<b>💡 Гипотезы недели</b>")
        for insight in report.hypotheses:
            lines.append(f"• {insight}")
        lines.append("")

    # Footer
    ts_msk = (report.generated_at + __import__("datetime").timedelta(hours=3)).strftime("%H:%M")
    lines.append(f"<i>Собрано {ts_msk} МСК автоматически. Алерт при |Δ| ≥ {ALERT_PCT:.0f}%</i>")
    return "\n".join(lines)
