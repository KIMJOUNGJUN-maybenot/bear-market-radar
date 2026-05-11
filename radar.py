# radar.py
# 매일 9시에 실행되어 하락장 레이더 점수를 계산하고 Telegram으로 발송하는 메인 파일입니다.

from __future__ import annotations

import html
import io
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from advanced_signals import fetch_advanced_items


FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

HISTORY_PATH = Path("data/history.csv")
ADVANCED_CSV_PATH = Path("advanced_signals.csv")


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}가 없습니다.")
    return value


def sigmoid(x: float) -> float:
    x = max(min(float(x), 8.0), -8.0)
    return 1.0 / (1.0 + math.exp(-x))


def status_emoji(risk: float) -> str:
    if risk >= 70:
        return "🔴"
    if risk >= 55:
        return "🟠"
    if risk >= 35:
        return "🟡"
    return "🟢"


def trend_arrow(trend_z: float) -> str:
    if trend_z > 0.35:
        return "▲"
    if trend_z < -0.35:
        return "▼"
    return "→"


def kst_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Seoul"))


def sanitize_error(text: object) -> str:
    """Telegram 메시지에 API key가 노출되지 않도록 오류 문자열을 정리합니다."""
    raw = str(text)
    import re

    patterns = [
        r"(serviceKey=)[^&\s)]+",
        r"(apikey=)[^&\s)]+",
        r"(api_key=)[^&\s)]+",
        r"(TELEGRAM_BOT_TOKEN=)[^&\s)]+",
    ]
    for pat in patterns:
        raw = re.sub(pat, r"\1***", raw, flags=re.IGNORECASE)
    return raw


# ---------------------------------------------------------------------------
# Data fetchers: FRED
# ---------------------------------------------------------------------------


def fred_series(series_id: str, start: str = "2018-01-01") -> pd.Series:
    api_key = require_env("FRED_API_KEY")
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
    }
    r = requests.get(FRED_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    if "observations" not in data:
        raise RuntimeError(f"FRED 응답에 observations가 없습니다: {data}")

    df = pd.DataFrame(data["observations"])
    values = pd.to_numeric(df["value"].replace({".": np.nan}), errors="coerce")
    s = pd.Series(values.values, index=pd.to_datetime(df["date"]))
    return s.dropna().sort_index()


# ---------------------------------------------------------------------------
# Data fetchers: Manufacturing activity proxy
# ---------------------------------------------------------------------------


def fred_manufacturing_activity_proxy() -> pd.Series:
    """Public proxy for the removed ISM PMI series.

    Important:
    - Exact ISM Manufacturing PMI data is not available in current FRED/FRED-MD.
    - FRED-MD removed the ISM/NAPM series from current vintages after ISM asked
      FRED to remove those series.
    - To keep this bot fully automatic without a paid/licensed ISM feed, we use
      FRED's Industrial Production: Manufacturing (NAICS), IPMAN, transformed to
      YoY percent change. Lower YoY growth means higher recession/bear-market risk.

    This is NOT the same as ISM PMI. It is a public manufacturing-cycle proxy.
    """
    raw = fred_series("IPMAN")
    yoy = raw.pct_change(12).dropna() * 100.0

    if len(yoy) < 24:
        raise RuntimeError("IPMAN YoY 제조업 프록시 계산 데이터가 부족합니다.")

    return yoy.sort_index()


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------


def make_item(
    name: str,
    s: pd.Series,
    higher_is_risk: bool,
    weight: float,
    window: int,
    value_format: str,
    change_kind: str,
) -> dict[str, Any]:
    s = s.dropna().sort_index()

    if len(s) < window + 10:
        raise RuntimeError(f"{name}: 데이터가 부족합니다. 현재 {len(s)}개")

    last = float(s.iloc[-1])
    prev = float(s.iloc[-1 - window])

    # 최근 3년 정도 범위에서 현재 레벨이 어느 분위인지 계산합니다.
    hist = s.tail(min(756, len(s)))
    level_pct = float(hist.rank(pct=True).iloc[-1])
    level_risk = level_pct if higher_is_risk else 1.0 - level_pct

    if change_kind == "pct":
        change_value = (last / prev - 1.0) * 100.0
        deltas = s.pct_change(window).dropna() * 100.0
        change_text = f"{change_value:+.2f}%/{window}관측치"

    elif change_kind == "bps":
        change_value = (last - prev) * 100.0
        deltas = s.diff(window).dropna() * 100.0
        change_text = f"{change_value:+.0f}bp/{window}관측치"

    elif change_kind == "pp":
        change_value = last - prev
        deltas = s.diff(window).dropna()
        change_text = f"{change_value:+.2f}p/{window}관측치"

    else:
        change_value = last - prev
        deltas = s.diff(window).dropna()
        change_text = f"{change_value:+.2f}/{window}관측치"

    sd = float(deltas.tail(min(756, len(deltas))).std()) if len(deltas) > 10 else 0.0

    if sd == 0.0 or np.isnan(sd):
        trend_z = 0.0
    else:
        trend_z = change_value / sd

    if not higher_is_risk:
        trend_z *= -1.0

    # 레벨 70%, 최근 변화 30% 반영
    trend_risk = sigmoid(trend_z)
    risk = 100.0 * (0.70 * level_risk + 0.30 * trend_risk)

    return {
        "name": name,
        "value": value_format.format(last),
        "risk": float(risk),
        "trend_z": float(trend_z),
        "arrow": trend_arrow(trend_z),
        "status": status_emoji(risk),
        "change": change_text,
        "asof": s.index[-1].strftime("%Y-%m-%d"),
        "weight": float(weight),
    }


def load_advanced_signals_csv(path: Path = ADVANCED_CSV_PATH) -> list[dict[str, Any]]:
    """
    자동수집 실패 시 임시로 수동 입력값을 넣는 백업 CSV입니다.
    컬럼: name,value,risk,trend_z,asof,weight
    """
    if not path.exists():
        return []

    df = pd.read_csv(path)
    if df.empty:
        return []

    required = {"name", "value", "risk", "trend_z", "asof", "weight"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"advanced_signals.csv 누락 컬럼: {sorted(missing)}")

    items = []
    for _, row in df.iterrows():
        if pd.isna(row["name"]):
            continue
        risk = float(row["risk"])
        trend_z = float(row["trend_z"])
        if not 0 <= risk <= 100:
            raise RuntimeError(f"{row['name']}: risk는 0~100이어야 합니다.")

        items.append(
            {
                "name": str(row["name"]),
                "value": str(row["value"]),
                "risk": risk,
                "trend_z": trend_z,
                "arrow": trend_arrow(trend_z),
                "status": status_emoji(risk),
                "change": "CSV 백업값",
                "asof": str(row["asof"]),
                "weight": float(row["weight"]),
            }
        )

    return items


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 이름의 지표가 중복될 때 정리합니다.

    원칙:
    - 자동수집 성공값(weight > 0)은 CSV 백업값보다 우선합니다.
    - 자동수집 실패로 만들어진 placeholder(weight == 0)는 CSV 백업값(weight > 0)이 있으면 교체합니다.
      예: FMP 권한 부족으로 Forward EPS가 제외됐지만 advanced_signals.csv에 수동값이 있으면 수동값 사용.
    """
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            continue

        if name not in by_name:
            by_name[name] = item
            order.append(name)
            continue

        old = by_name[name]
        old_w = float(old.get("weight", 0))
        new_w = float(item.get("weight", 0))

        # 기존 값이 placeholder이고 새 값이 실제 가중치가 있으면 새 값으로 교체합니다.
        if old_w <= 0 < new_w:
            by_name[name] = item

    return [by_name[name] for name in order]


def compute_score(items: list[dict[str, Any]]) -> tuple[float, float, float]:
    total_weight = sum(float(x["weight"]) for x in items)
    if total_weight <= 0:
        raise RuntimeError("계산 가능한 지표가 없습니다.")

    score = sum(float(x["weight"]) * float(x["risk"]) for x in items) / total_weight
    direction_z = sum(float(x["weight"]) * float(x["trend_z"]) for x in items) / total_weight
    return float(score), float(direction_z), float(total_weight)


# ---------------------------------------------------------------------------
# History and trend
# ---------------------------------------------------------------------------


def load_history_delta(score: float, coverage: float) -> tuple[float | None, float | None, str]:
    if not HISTORY_PATH.exists():
        return None, None, "첫 실행"

    df = pd.read_csv(HISTORY_PATH)
    if df.empty or "score" not in df.columns:
        return None, None, "첫 실행"

    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    if "coverage" in df.columns:
        df["coverage"] = pd.to_numeric(df["coverage"], errors="coerce")
    else:
        df["coverage"] = coverage

    df = df.dropna(subset=["score"])
    if df.empty:
        return None, None, "첫 실행"

    last_coverage = float(df.iloc[-1].get("coverage", coverage))
    if abs(last_coverage - coverage) > 0.5:
        return None, None, "커버리지 변경으로 추이 보류"

    delta_1 = score - float(df.iloc[-1]["score"])

    if len(df) >= 5:
        delta_n = score - float(df.iloc[-5]["score"])
        label = "최근 5회 변화"
    elif len(df) >= 2:
        delta_n = score - float(df.iloc[0]["score"])
        label = f"최근 {len(df) + 1}회 변화"
    else:
        delta_n = None
        label = "추이 데이터 부족"

    return float(delta_1), None if delta_n is None else float(delta_n), label


def save_history(score: float, direction_z: float, coverage: float) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = kst_now().isoformat(timespec="seconds")

    row = pd.DataFrame(
        [
            {
                "run_at": now,
                "score": round(score, 2),
                "direction_z": round(direction_z, 4),
                "coverage": round(coverage, 2),
            }
        ]
    )

    if HISTORY_PATH.exists():
        old = pd.read_csv(HISTORY_PATH)
        out = pd.concat([old, row], ignore_index=True)
    else:
        out = row

    out.to_csv(HISTORY_PATH, index=False)


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------


def grade_text(score: float) -> str:
    if score >= 70:
        return "🔴 강한 하락장 신호"
    if score >= 55:
        return "🟠 위험 / 하락장 접근"
    if score >= 35:
        return "🟡 경계 / 리스크 누적"
    return "🟢 정상 / 하락장과 거리 있음"


def direction_text(delta_n: float | None, direction_z: float) -> str:
    if delta_n is not None:
        if delta_n >= 5:
            return "하락장에 가까워지는 중"
        if delta_n <= -5:
            return "하락장에서 멀어지는 중"

    if direction_z > 0.35:
        return "하락장에 가까워지는 중"
    if direction_z < -0.35:
        return "하락장에서 멀어지는 중"
    return "중립 또는 횡보"


def format_delta(x: float | None) -> str:
    if x is None:
        return "N/A"
    return f"{x:+.1f}점"


def render_message(
    items: list[dict[str, Any]],
    errors: list[str],
    score: float,
    direction_z: float,
    coverage: float,
    delta_1: float | None,
    delta_n: float | None,
    delta_label: str,
) -> str:
    now = kst_now().strftime("%Y-%m-%d %H:%M")

    # weight=0 지표는 FMP 권한 부족처럼 "수집 실패/자동 제외"된 항목입니다.
    # 이런 항목은 종합점수와 방향성에 들어가지 않고, 별도 섹션에만 표시합니다.
    active_items = [x for x in items if float(x.get("weight", 0)) > 0]
    excluded_items = [x for x in items if float(x.get("weight", 0)) <= 0]

    movers = sorted(active_items, key=lambda x: abs(float(x["trend_z"])), reverse=True)[:3]
    direction = direction_text(delta_n, direction_z)

    lines = [
        f"📉 <b>하락장 레이더</b> | {now} KST",
        "",
        f"<b>종합점수:</b> {score:.0f}/100",
        f"<b>판정:</b> {grade_text(score)}",
        f"<b>방향성:</b> {direction}",
        f"<b>전회 변화:</b> {format_delta(delta_1)}",
        f"<b>{html.escape(delta_label)}:</b> {format_delta(delta_n)}",
        f"<b>데이터 커버리지:</b> {coverage:.0f}/100",
        "<b>주의:</b> 커버리지가 100 미만이면 제외 지표는 종합점수에 반영되지 않습니다.",
        "",
        "<b>핵심 변화 Top 3</b>",
    ]

    if movers:
        for i, x in enumerate(movers, 1):
            lines.append(
                f"{i}) {x['arrow']} {html.escape(str(x['name']))}: "
                f"{html.escape(str(x['value']))} | {html.escape(str(x['change']))} | 위험 {float(x['risk']):.0f}"
            )
    else:
        lines.append("수집된 활성 지표가 없습니다.")

    lines += ["", "<b>지표별 체크</b>"]

    for x in sorted(active_items, key=lambda y: float(y["weight"]), reverse=True):
        lines.append(
            f"{x['status']} {x['arrow']} <b>{html.escape(str(x['name']))}</b>: "
            f"{html.escape(str(x['value']))} | {html.escape(str(x['change']))} "
            f"| 위험 {float(x['risk']):.0f} | 기준 {html.escape(str(x['asof']))}"
        )

    missing = max(0.0, 100.0 - coverage)
    if missing > 0 or excluded_items:
        lines += [
            "",
            f"⚠️ <b>미연결/제외 지표 비중:</b> {missing:.0f}/100",
            "아래 제외 지표는 종합점수·방향성 계산에 포함되지 않습니다.",
        ]

    if excluded_items:
        lines += ["", "<b>제외 지표</b>"]
        for x in excluded_items:
            lines.append(
                f"- <b>{html.escape(str(x['name']))}</b>: "
                f"{html.escape(str(x['value']))} | {html.escape(str(x['change']))} "
                f"| 기준 {html.escape(str(x['asof']))}"
            )

    if errors:
        lines += ["", "<b>데이터 오류/누락</b>"]
        for e in errors[:8]:
            lines.append(f"- {html.escape(sanitize_error(e))}")
        if len(errors) > 8:
            lines.append(f"- 외 {len(errors) - 8}개")

    # 한 줄 결론: 제외 지표는 고위험 지표 판단에서도 제외합니다.
    high_risk_names = [str(x["name"]) for x in active_items if float(x["risk"]) >= 60]
    if score >= 55 and direction == "하락장에 가까워지는 중":
        conclusion = "복수 핵심 지표가 악화되어 하락장 접근 신호가 강해지고 있습니다."
    elif direction == "하락장에서 멀어지는 중":
        conclusion = "최근 변화 기준으로는 하락장 압력이 완화되고 있습니다."
    elif score >= 35:
        conclusion = "경계 구간입니다. 크레딧, 달러, 고용·제조업, 이익 리비전 방향을 계속 확인해야 합니다."
    else:
        conclusion = "현재 수집 지표 기준으로는 하락장과 거리가 있습니다."

    if high_risk_names:
        conclusion += " 고위험 지표: " + ", ".join(high_risk_names[:4]) + "."

    if excluded_items:
        conclusion += " 단, 제외 지표가 있어 커버리지 기준으로 해석해야 합니다."

    lines += ["", f"<b>한 줄 결론:</b> {html.escape(conclusion)}"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def telegram_chat_ids() -> list[str]:
    """Return one or more Telegram chat IDs.

    Preferred secret for multiple recipients:
        TELEGRAM_CHAT_IDS="123456789,-987654321,@my_channel"

    Backward-compatible single-recipient secret:
        TELEGRAM_CHAT_ID="123456789"
    """
    raw = os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID") or ""
    ids = [x.strip() for x in re.split(r"[,;\n]+", raw) if x.strip()]

    if not ids:
        raise RuntimeError("TELEGRAM_CHAT_IDS 또는 TELEGRAM_CHAT_ID가 없습니다.")

    # 중복 제거, 순서 유지
    seen: set[str] = set()
    unique: list[str] = []
    for chat_id in ids:
        if chat_id not in seen:
            seen.add(chat_id)
            unique.append(chat_id)
    return unique


def _send_telegram_chunk_to_chat(text: str, chat_id: str) -> None:
    token = require_env("TELEGRAM_BOT_TOKEN")

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def _split_telegram_message(text: str, max_len: int = 3800) -> list[str]:
    # Telegram sendMessage는 4096자 제한이 있으므로, HTML 태그 여유를 두고 3800자로 나눕니다.
    if len(text) <= max_len:
        return [text]

    lines = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in lines:
        add = len(line) + 1
        if current and size + add > max_len:
            chunks.append("\n".join(current))
            current = [line]
            size = add
        else:
            current.append(line)
            size += add

    if current:
        chunks.append("\n".join(current))

    if len(chunks) > 1:
        return [f"<b>하락장 레이더 {i}/{len(chunks)}</b>\n{chunk}" for i, chunk in enumerate(chunks, 1)]
    return chunks


def send_telegram(text: str) -> None:
    chunks = _split_telegram_message(text)
    chat_ids = telegram_chat_ids()

    successes = 0
    failures: list[str] = []

    for chat_id in chat_ids:
        try:
            for chunk in chunks:
                _send_telegram_chunk_to_chat(chunk, chat_id)
            successes += 1
        except Exception as e:
            failures.append(f"{chat_id}: {sanitize_error(e)}")

    if failures:
        print("Telegram send failures:")
        for failure in failures:
            print("- " + failure)

    if successes == 0:
        raise RuntimeError("Telegram 전송 실패: " + " | ".join(failures))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    items: list[dict[str, Any]] = []
    errors: list[str] = []

    specs = [
        # name, fetcher, higher_is_risk, weight, window, value_format, change_kind
        ("HY Spread", lambda: fred_series("BAMLH0A0HYM2"), True, 15, 20, "{:.2f}%", "bps"),
        ("미국 10Y TIPS 실질금리(DFII10)", lambda: fred_series("DFII10"), True, 8, 20, "{:.2f}%", "bps"),
        ("광의 달러지수(DTWEXBGS)", lambda: fred_series("DTWEXBGS"), True, 7, 20, "{:.2f}", "pct"),
        ("원/달러", lambda: fred_series("DEXKOUS"), True, 7, 20, "{:,.0f}원", "pct"),
        ("제조업 경기 프록시(IPMAN YoY)", fred_manufacturing_activity_proxy, False, 4, 3, "{:+.2f}%", "pp"),
        ("미국 실업률", lambda: fred_series("UNRATE"), True, 4, 3, "{:.1f}%", "pp"),
    ]

    for name, fetcher, higher_is_risk, weight, window, value_format, change_kind in specs:
        try:
            s = fetcher()
            items.append(
                make_item(
                    name=name,
                    s=s,
                    higher_is_risk=higher_is_risk,
                    weight=weight,
                    window=window,
                    value_format=value_format,
                    change_kind=change_kind,
                )
            )
        except Exception as e:
            errors.append(sanitize_error(f"{name}: {e}"))

    # 자동 고급 지표: CAPEX, NVDA guidance, 반도체 수출, Forward EPS, EPS Revision
    try:
        advanced_items, advanced_errors = fetch_advanced_items()
        items.extend(advanced_items)
        errors.extend(advanced_errors)
    except Exception as e:
        errors.append(sanitize_error(f"advanced_signals.py: {e}"))

    # 자동수집 실패 시 수동 CSV 값으로 백업 가능. 같은 이름이면 자동수집값을 우선합니다.
    try:
        items.extend(load_advanced_signals_csv())
    except Exception as e:
        errors.append(sanitize_error(f"advanced_signals.csv: {e}"))

    items = dedupe_items(items)

    if not items:
        raise RuntimeError("수집된 지표가 없습니다.")

    score, direction_z, coverage = compute_score(items)
    delta_1, delta_n, delta_label = load_history_delta(score, coverage)

    message = render_message(
        items=items,
        errors=errors,
        score=score,
        direction_z=direction_z,
        coverage=coverage,
        delta_1=delta_1,
        delta_n=delta_n,
        delta_label=delta_label,
    )

    send_telegram(message)
    save_history(score, direction_z, coverage)


if __name__ == "__main__":
    main()
