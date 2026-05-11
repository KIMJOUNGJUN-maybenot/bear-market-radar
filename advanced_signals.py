

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup


DATA_DIR = Path("data")
SEC_CACHE_DIR = DATA_DIR / "sec_cache"
EPS_HISTORY_PATH = DATA_DIR / "eps_estimates_history.csv"
NVDA_GUIDANCE_HISTORY_PATH = DATA_DIR / "nvda_guidance_history.csv"
EPS_SYMBOLS_PATH = Path("eps_symbols.csv")

SEC_DATA = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

HYPERSCALERS = {
    "MSFT": "0000789019",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "AMZN": "0001018724",
    "ORCL": "0001341439",
}

# 회사마다 태그명이 조금 다를 수 있어 후보를 여러 개 둡니다.
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "CapitalExpenditures",
]

DEFAULT_EPS_SYMBOLS = [
    "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META",
    "AVGO", "AMD", "TSM", "ASML", "ORCL", "MU", "QCOM", "CRM", "NOW",
]


def clip(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, float(x))))


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


def make_item(
    name: str,
    value: str,
    risk: float,
    trend_z: float,
    asof: str,
    weight: float,
    change: str = "자동수집",
) -> dict[str, Any]:
    risk = clip(risk, 0, 100)
    trend_z = clip(trend_z, -3, 3)
    return {
        "name": name,
        "value": str(value),
        "risk": risk,
        "trend_z": trend_z,
        "arrow": trend_arrow(trend_z),
        "status": status_emoji(risk),
        "change": str(change),
        "asof": str(asof),
        "weight": float(weight),
    }


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} 환경변수가 필요합니다.")
    return value


def sanitize_error(text: object) -> str:
    """오류 메시지에서 API key가 노출되지 않도록 마스킹합니다."""
    raw = str(text)
    patterns = [
        r"(serviceKey=)[^&\s)]+",
        r"(apikey=)[^&\s)]+",
        r"(api_key=)[^&\s)]+",
    ]
    for pat in patterns:
        raw = re.sub(pat, r"\1***", raw, flags=re.IGNORECASE)
    return raw


def today_kst() -> str:
    return datetime.now().astimezone().date().isoformat()


# ---------------------------------------------------------------------------
# SEC helpers
# ---------------------------------------------------------------------------


def sec_headers() -> dict[str, str]:
    ua = require_env("SEC_USER_AGENT")
    return {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate",
    }


def sec_get_json(url: str, cache_name: str | None = None, max_age_hours: int = 18) -> dict[str, Any]:
    """SEC JSON 요청. GitHub Actions 반복 실행 부담을 줄이기 위해 짧은 캐시를 둡니다."""
    if cache_name:
        SEC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = SEC_CACHE_DIR / cache_name
        if cache_path.exists():
            age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
            if age_hours <= max_age_hours:
                return json.loads(cache_path.read_text(encoding="utf-8"))

    # SEC 자동접근은 과도하게 호출하지 않도록 최소 딜레이를 둡니다.
    time.sleep(0.15)
    r = requests.get(url, headers=sec_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()

    if cache_name:
        cache_path.write_text(json.dumps(data), encoding="utf-8")

    return data


def sec_get_text(url: str) -> str:
    time.sleep(0.15)
    r = requests.get(url, headers=sec_headers(), timeout=30)
    r.raise_for_status()
    return r.text


def companyfacts(cik: str) -> dict[str, Any]:
    cik10 = str(cik).zfill(10)
    return sec_get_json(
        f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik10}.json",
        cache_name=f"companyfacts_{cik10}.json",
        max_age_hours=18,
    )


def _usd_facts_for_tag(facts: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    concept = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not concept:
        return []

    units = concept.get("units", {})
    candidates = []
    for unit_name, rows in units.items():
        if unit_name.upper() == "USD":
            candidates.extend(rows)
    return candidates


def _rows_to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    out = []
    for x in rows:
        try:
            value = float(x.get("val"))
        except Exception:
            continue

        start = pd.to_datetime(x.get("start"), errors="coerce")
        end = pd.to_datetime(x.get("end"), errors="coerce")
        filed = pd.to_datetime(x.get("filed"), errors="coerce")

        if pd.isna(end) or pd.isna(filed):
            continue

        out.append(
            {
                "start": start,
                "end": end,
                "filed": filed,
                "val": abs(value),
                "fy": x.get("fy"),
                "fp": x.get("fp"),
                "form": x.get("form"),
                "frame": x.get("frame"),
                "accn": x.get("accn"),
            }
        )

    return pd.DataFrame(out)


def _direct_quarterly_series(df: pd.DataFrame) -> pd.Series:
    """기간이 70~120일인 실제 분기 데이터가 있으면 우선 사용합니다."""
    if df.empty or "start" not in df.columns:
        return pd.Series(dtype=float)

    tmp = df.copy()
    tmp = tmp.dropna(subset=["start", "end", "filed", "val"])
    tmp["days"] = (tmp["end"] - tmp["start"]).dt.days
    tmp = tmp[(tmp["days"] >= 70) & (tmp["days"] <= 125)]
    tmp = tmp[tmp["form"].isin(["10-Q", "10-K", "20-F", "40-F"])]

    if tmp.empty:
        return pd.Series(dtype=float)

    tmp["cal_q"] = tmp["end"].dt.to_period("Q")
    tmp = tmp.sort_values(["cal_q", "filed", "end"]).drop_duplicates("cal_q", keep="last")
    s = pd.Series(tmp["val"].values, index=tmp["cal_q"])
    return s.sort_index()


def _ytd_to_quarterly_series(df: pd.DataFrame) -> pd.Series:
    """10-Q YTD 현금흐름 값을 Q1/Q2/Q3/FY로 분기화합니다."""
    if df.empty:
        return pd.Series(dtype=float)

    tmp = df.copy()
    tmp = tmp[tmp["form"].isin(["10-Q", "10-K", "20-F", "40-F"])]
    tmp = tmp[tmp["fp"].isin(["Q1", "Q2", "Q3", "FY"])]
    tmp = tmp.dropna(subset=["fy", "fp", "end", "filed", "val"])

    if tmp.empty:
        return pd.Series(dtype=float)

    tmp["fy"] = pd.to_numeric(tmp["fy"], errors="coerce")
    tmp = tmp.dropna(subset=["fy"])
    tmp["fy"] = tmp["fy"].astype(int)
    tmp = tmp.sort_values(["fy", "fp", "filed", "end"]).drop_duplicates(["fy", "fp"], keep="last")

    records = []
    fp_order = ["Q1", "Q2", "Q3", "FY"]

    for fy, g in tmp.groupby("fy"):
        vals = {row["fp"]: float(row["val"]) for _, row in g.iterrows()}
        ends = {row["fp"]: row["end"] for _, row in g.iterrows()}

        prior_ytd = 0.0
        for fp in fp_order:
            if fp not in vals:
                continue
            q_val = vals[fp] - prior_ytd
            prior_ytd = vals[fp]
            if q_val <= 0:
                q_val = abs(q_val)
            end = ends[fp]
            if pd.isna(end):
                continue
            records.append({"cal_q": pd.Timestamp(end).to_period("Q"), "val": q_val})

    if not records:
        return pd.Series(dtype=float)

    qdf = pd.DataFrame(records)
    s = qdf.groupby("cal_q")["val"].last().sort_index()
    return s


def fact_quarterly_series(facts: dict[str, Any], tag: str) -> pd.Series:
    rows = _usd_facts_for_tag(facts, tag)
    df = _rows_to_dataframe(rows)
    if df.empty:
        return pd.Series(dtype=float)

    s = _direct_quarterly_series(df)
    if len(s) >= 8:
        return s

    s2 = _ytd_to_quarterly_series(df)
    return s2 if len(s2) > len(s) else s


def company_capex_series(cik: str) -> pd.Series:
    facts = companyfacts(cik)
    best = pd.Series(dtype=float)
    best_tag = None

    for tag in CAPEX_TAGS:
        s = fact_quarterly_series(facts, tag)
        if len(s) > len(best):
            best = s
            best_tag = tag

    if len(best) < 6:
        raise RuntimeError(f"CIK {cik}: CAPEX 태그 데이터를 충분히 찾지 못했습니다.")

    best.name = best_tag
    return best


def _aggregate_capex_item(series_by_ticker: list[tuple[str, pd.Series]], source_label: str) -> dict[str, Any]:
    """회사별 CAPEX TTM YoY를 먼저 계산한 뒤 TTM 규모로 가중평균합니다.

    회사들의 회계연도/분기 마감일이 서로 달라 공통 분기에 맞춰 합산하면 데이터가
    부족해질 수 있습니다. 그래서 각 회사별로 TTM YoY를 계산하고, 최신 TTM 규모로
    가중평균해 하이퍼스케일러 CAPEX 지표를 만듭니다.
    """
    records: list[dict[str, Any]] = []
    failed: list[str] = []

    for ticker, s in series_by_ticker:
        try:
            s = s.dropna().sort_index().astype(float)
            if len(s) < 8:
                failed.append(f"{ticker}: 분기 CAPEX {len(s)}개")
                continue

            ttm = s.rolling(4).sum().dropna()
            yoy = (ttm / ttm.shift(4) - 1).replace([np.inf, -np.inf], np.nan).dropna()

            if len(yoy) < 2:
                failed.append(f"{ticker}: YoY 계산 데이터 부족")
                continue

            records.append(
                {
                    "ticker": ticker,
                    "ttm_latest": float(ttm.iloc[-1]),
                    "last_yoy": float(yoy.iloc[-1]),
                    "prev_yoy": float(yoy.iloc[-2]),
                    "asof": str(yoy.index[-1]),
                }
            )
        except Exception as e:
            failed.append(f"{ticker}: {sanitize_error(e)}")

    if len(records) < 3:
        raise RuntimeError(f"{source_label} CAPEX 계산 가능 회사 부족: " + "; ".join(failed[:6]))

    weights = np.array([max(r["ttm_latest"], 1.0) for r in records], dtype=float)
    last_yoy = float(np.average([r["last_yoy"] for r in records], weights=weights))
    prev_yoy = float(np.average([r["prev_yoy"] for r in records], weights=weights))
    delta = last_yoy - prev_yoy
    total_ttm = float(sum(r["ttm_latest"] for r in records))

    risk = 50 - last_yoy * 90 + max(0.0, -delta) * 140
    trend_z = -delta * 10

    value = f"가중 TTM YoY {last_yoy:+.1%}, TTM ${total_ttm / 1e9:.1f}B ({len(records)}/{len(HYPERSCALERS)}개사)"
    change = f"{source_label}, 전분기 YoY 변화 {delta:+.1%}p"
    if failed:
        change += f"; 일부 제외 {len(failed)}개"

    return make_item(
        name="하이퍼스케일러 CAPEX",
        value=value,
        risk=risk,
        trend_z=trend_z,
        asof=max(r["asof"] for r in records),
        weight=15,
        change=change,
    )


def hyperscaler_capex_item_sec() -> dict[str, Any]:
    series_by_ticker: list[tuple[str, pd.Series]] = []
    failed: list[str] = []

    for ticker, cik in HYPERSCALERS.items():
        try:
            s = company_capex_series(cik)
            series_by_ticker.append((ticker, s))
        except Exception as e:
            failed.append(f"{ticker}: {sanitize_error(e)}")

    if len(series_by_ticker) < 3:
        raise RuntimeError("SEC CAPEX 수집 실패: " + "; ".join(failed[:6]))

    return _aggregate_capex_item(series_by_ticker, source_label="SEC companyfacts")

def hyperscaler_capex_item() -> dict[str, Any]:
    """하이퍼스케일러 CAPEX.

    FMP cash-flow-statement의 quarterly capitalExpenditure를 우선 사용합니다.
    FMP가 안 되면 SEC companyfacts 방식으로 fallback합니다.
    """
    fmp_error = None
    try:
        return hyperscaler_capex_item_fmp()
    except Exception as e:
        fmp_error = e

    try:
        return hyperscaler_capex_item_sec()
    except Exception as sec_error:
        raise RuntimeError(
            "FMP CAPEX 실패(" + sanitize_error(fmp_error) + "); "
            "SEC CAPEX 실패(" + sanitize_error(sec_error) + ")"
        )


# ---------------------------------------------------------------------------
# NVIDIA guidance
# ---------------------------------------------------------------------------


def html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def parse_revenue_guidance(text: str) -> float | None:
    # Outlook 주변 문장만 먼저 봅니다. 없으면 전체를 봅니다.
    lower = text.lower()
    idx_candidates = [lower.find("outlook"), lower.find("business outlook")]
    idx_candidates = [x for x in idx_candidates if x >= 0]
    if idx_candidates:
        idx = min(idx_candidates)
        window = text[idx: idx + 7000]
    else:
        window = text[:15000]

    patterns = [
        r"revenue\s+is\s+expected\s+to\s+be\s+(?:approximately\s+)?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
        r"revenue[^.]{0,140}?expected[^.]{0,140}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
        r"expected\s+revenue[^.]{0,140}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
    ]

    for pat in patterns:
        m = re.search(pat, window, flags=re.IGNORECASE)
        if m:
            num = float(m.group(1))
            unit = m.group(2).lower()
            return num * 1e9 if unit == "billion" else num * 1e6

    return None


def latest_nvda_guidance_from_sec() -> dict[str, Any]:
    cik = "0001045810"
    sub = sec_get_json(f"{SEC_DATA}/submissions/CIK{cik}.json", cache_name="nvda_submissions.json", max_age_hours=8)
    recent = sub.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])

    candidates = [
        (form, acc, date)
        for form, acc, date in zip(forms, accs, dates)
        if form == "8-K"
    ][:25]

    for _, acc, filing_date in candidates:
        acc_no_dash = acc.replace("-", "")
        index_url = f"{SEC_ARCHIVES}/1045810/{acc_no_dash}/index.json"

        try:
            index = sec_get_json(index_url, cache_name=f"nvda_{acc_no_dash}_index.json", max_age_hours=48)
        except Exception:
            continue

        files = index.get("directory", {}).get("item", [])
        html_files = [f.get("name", "") for f in files if f.get("name", "").lower().endswith((".htm", ".html"))]

        preferred = (
            [n for n in html_files if "ex99" in n.lower()]
            + [n for n in html_files if "8-k" in n.lower()]
            + html_files
        )

        seen = set()
        for name in preferred:
            if not name or name in seen:
                continue
            seen.add(name)
            url = f"{SEC_ARCHIVES}/1045810/{acc_no_dash}/{name}"
            try:
                text = html_to_text(sec_get_text(url))
                guide = parse_revenue_guidance(text)
            except Exception:
                guide = None

            if guide:
                return {
                    "filing_date": filing_date,
                    "accession": acc,
                    "revenue_guidance": guide,
                    "source_file": name,
                }

    raise RuntimeError("NVIDIA 최신 8-K에서 Revenue Outlook 문구를 찾지 못했습니다.")


def append_history(path: Path, row: dict[str, Any], keys: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([row])

    if path.exists():
        old = pd.read_csv(path)
        out = pd.concat([old, new], ignore_index=True)
    else:
        out = new

    out = out.drop_duplicates(keys, keep="last")
    out.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# FMP helpers: Forward EPS / EPS Revision Ratio / NVDA revenue consensus
# ---------------------------------------------------------------------------


def fmp_key() -> str:
    return require_env("FMP_API_KEY")


def _fmp_request(url: str, params: dict[str, Any]) -> Any:
    params = dict(params)
    params["apikey"] = fmp_key()
    r = requests.get(url, params=params, timeout=30)

    # FMP 무료/저가 플랜에서 analyst-estimates가 403으로 막히는 경우가 흔합니다.
    # 긴 URL과 종목별 반복 오류를 Telegram에 뿌리지 않도록 짧게 정리합니다.
    if r.status_code in (401, 403):
        endpoint = url.replace("https://financialmodelingprep.com", "")
        raise RuntimeError(f"FMP 권한 오류 {r.status_code}: {endpoint}")

    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        msg = data.get("Error Message") or data.get("error") or data.get("message")
        if msg:
            raise RuntimeError(str(msg))
    return data


def fmp_analyst_estimates(symbol: str) -> list[dict[str, Any]]:
    # FMP는 stable endpoint와 legacy v3 endpoint가 모두 쓰입니다.
    # 일부 계정/플랜에서는 한쪽만 응답하므로 4가지 조합을 순차 시도합니다.
    urls = [
        ("https://financialmodelingprep.com/stable/analyst-estimates", {"symbol": symbol, "period": "quarter", "limit": 20}),
        ("https://financialmodelingprep.com/stable/analyst-estimates", {"symbol": symbol, "period": "annual", "limit": 20}),
        (f"https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}", {"period": "quarter", "limit": 20}),
        (f"https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}", {"period": "annual", "limit": 20}),
    ]

    last_error = None
    for url, params in urls:
        try:
            data = _fmp_request(url, params)
            if isinstance(data, list) and data:
                return data
        except Exception as e:
            last_error = e

    if last_error:
        raise RuntimeError(f"FMP analyst estimates 실패: {last_error}")
    return []


def fmp_quote(symbol: str) -> dict[str, Any] | None:
    urls = [
        ("https://financialmodelingprep.com/stable/quote", {"symbol": symbol}),
        (f"https://financialmodelingprep.com/api/v3/quote/{symbol}", {}),
    ]

    for url, params in urls:
        try:
            data = _fmp_request(url, params)
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data:
                return data
        except Exception:
            continue
    return None


def fmp_cash_flow_quarterly(symbol: str) -> pd.Series:
    """FMP cash-flow-statement에서 quarterly capitalExpenditure를 가져옵니다."""
    urls = [
        (f"https://financialmodelingprep.com/api/v3/cash-flow-statement/{symbol}", {"period": "quarter", "limit": 24}),
        ("https://financialmodelingprep.com/stable/cash-flow-statement", {"symbol": symbol, "period": "quarter", "limit": 24}),
    ]

    last_error = None
    for url, params in urls:
        try:
            data = _fmp_request(url, params)
            if not isinstance(data, list) or not data:
                continue

            records = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                dt = pd.to_datetime(row.get("date") or row.get("fiscalDateEnding"), errors="coerce")
                if pd.isna(dt):
                    continue
                capex = pick_number(
                    row,
                    [
                        "capitalExpenditure",
                        "capitalExpenditures",
                        "paymentsToAcquirePropertyPlantAndEquipment",
                        "paymentsToAcquireProductiveAssets",
                    ],
                )
                if capex is None:
                    continue
                records.append({"period": dt.to_period("Q"), "capex": abs(float(capex))})

            if records:
                df = pd.DataFrame(records).drop_duplicates("period", keep="first")
                return pd.Series(df["capex"].values, index=df["period"]).sort_index()
        except Exception as e:
            last_error = e

    raise RuntimeError(f"{symbol} FMP cash-flow-statement CAPEX 없음: {sanitize_error(last_error)}")


def hyperscaler_capex_item_fmp() -> dict[str, Any]:
    series_by_ticker: list[tuple[str, pd.Series]] = []
    failed: list[str] = []

    for ticker in HYPERSCALERS.keys():
        try:
            s = fmp_cash_flow_quarterly(ticker)
            series_by_ticker.append((ticker, s))
        except Exception as e:
            failed.append(f"{ticker}: {sanitize_error(e)}")
        time.sleep(0.05)

    if len(series_by_ticker) < 3:
        raise RuntimeError("FMP CAPEX 수집 부족: " + "; ".join(failed[:6]))

    return _aggregate_capex_item(series_by_ticker, source_label="FMP cashflow")

def parse_number(x: Any) -> float | None:
    if x in (None, "", "None", "null"):
        return None
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def pick_number(row: dict[str, Any], candidates: list[str]) -> float | None:
    lower_map = {k.lower(): k for k in row.keys()}
    for c in candidates:
        key = c if c in row else lower_map.get(c.lower())
        if key is None:
            continue
        val = parse_number(row.get(key))
        if val is not None:
            return val
    return None


def row_date(row: dict[str, Any]) -> pd.Timestamp | None:
    for key in ["date", "fiscalDateEnding", "period", "calendarYear"]:
        if key in row:
            dt = pd.to_datetime(row.get(key), errors="coerce")
            if not pd.isna(dt):
                return dt
    return None


def preferred_estimate_row(rows: list[dict[str, Any]], need: str = "eps") -> dict[str, Any] | None:
    if not rows:
        return None

    if need == "revenue":
        candidates = ["estimatedRevenueAvg", "revenueAvg", "revenueAverage", "estimatedRevenue"]
    else:
        candidates = ["estimatedEpsAvg", "epsAvg", "epsAverage", "estimatedEps"]

    valid = []
    now = pd.Timestamp.utcnow().tz_localize(None)

    for row in rows:
        val = pick_number(row, candidates)
        if val is None:
            continue

        dt = row_date(row)
        period_text = " ".join(str(row.get(k, "")) for k in ["period", "fiscalPeriod", "quarter"]).lower()
        is_quarter = any(x in period_text for x in ["quarter", "q1", "q2", "q3", "q4"])
        valid.append((is_quarter, dt, row))

    if not valid:
        return None

    # 미래 또는 최근 날짜인 quarterly row를 우선합니다.
    def sort_key(x):
        is_quarter, dt, _ = x
        if dt is None or pd.isna(dt):
            days = 99999
        else:
            days = abs((dt - now).days)
        return (0 if is_quarter else 1, days)

    return sorted(valid, key=sort_key)[0][2]


def fmp_revenue_estimate(symbol: str) -> float | None:
    rows = fmp_analyst_estimates(symbol)
    row = preferred_estimate_row(rows, need="revenue")
    if not row:
        return None
    return pick_number(row, ["estimatedRevenueAvg", "revenueAvg", "revenueAverage", "estimatedRevenue"])


def fmp_forward_eps(symbol: str) -> dict[str, Any] | None:
    rows = fmp_analyst_estimates(symbol)
    row = preferred_estimate_row(rows, need="eps")
    if not row:
        return None

    eps = pick_number(row, ["estimatedEpsAvg", "epsAvg", "epsAverage", "estimatedEps"])
    revenue = pick_number(row, ["estimatedRevenueAvg", "revenueAvg", "revenueAverage", "estimatedRevenue"])
    if eps is None:
        return None

    return {"symbol": symbol, "eps": eps, "revenue": revenue}


def fmp_market_cap(symbol: str) -> float:
    q = fmp_quote(symbol)
    if not q:
        return 1.0
    mc = pick_number(q, ["marketCap", "mktCap", "marketCapitalization"])
    return mc if mc and mc > 0 else 1.0


def nvda_guidance_item() -> dict[str, Any]:
    latest = latest_nvda_guidance_from_sec()

    append_history(
        NVDA_GUIDANCE_HISTORY_PATH,
        {
            "filing_date": latest["filing_date"],
            "accession": latest["accession"],
            "revenue_guidance": latest["revenue_guidance"],
            "source_file": latest.get("source_file"),
        },
        keys=["filing_date", "accession"],
    )

    hist = pd.read_csv(NVDA_GUIDANCE_HISTORY_PATH).sort_values("filing_date")
    latest_guide = float(hist.iloc[-1]["revenue_guidance"])

    qoq = None
    if len(hist) >= 2:
        prev_guide = float(hist.iloc[-2]["revenue_guidance"])
        if prev_guide > 0:
            qoq = latest_guide / prev_guide - 1

    consensus_gap = None
    try:
        consensus = fmp_revenue_estimate("NVDA")
        if consensus and consensus > 0:
            consensus_gap = latest_guide / consensus - 1
    except Exception:
        consensus = None

    if consensus_gap is not None:
        risk = 50 - consensus_gap * 600
        trend_z = -consensus_gap * 12
        compare_text = f"컨센서스 대비 {consensus_gap:+.1%}"
    elif qoq is not None:
        risk = 50 - qoq * 120
        trend_z = -qoq * 6
        compare_text = f"직전 가이던스 대비 {qoq:+.1%}"
    else:
        risk = 50
        trend_z = 0
        compare_text = "비교 기준 수집 중"

    value = f"Revenue guide ${latest_guide / 1e9:.1f}B"

    return make_item(
        name="Nvidia 가이던스",
        value=value,
        risk=risk,
        trend_z=trend_z,
        asof=str(latest["filing_date"]),
        weight=10,
        change=compare_text,
    )


# ---------------------------------------------------------------------------
# Korea semiconductor exports
# ---------------------------------------------------------------------------


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _first_present(d: dict[str, Any], names: list[str]) -> Any:
    lower = {str(k).lower(): k for k in d.keys()}
    for name in names:
        key = name if name in d else lower.get(name.lower())
        if key is not None:
            return d.get(key)
    return None


def _period_from_any(value: Any) -> pd.Period | None:
    if value is None:
        return None
    text = str(value)
    m = re.search(r"(20\d{2})\D?([01]\d)", text)
    if not m:
        return None
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Period(f"{m.group(1)}-{month:02d}", freq="M")


def _record_from_dict(d: dict[str, Any], hs_hint: str | None = None) -> dict[str, Any] | None:
    period = _period_from_any(
        _first_present(d, ["year", "bsYymm", "stdYymm", "trdYymm", "srchYymm", "tistYymm", "month", "yyyymm"])
    )

    amount = parse_number(
        _first_present(d, ["expDlr", "expUsd", "expAmt", "xportDlr", "xportUsd", "exportAmount", "expwgt"])
    )

    hs_code = _first_present(d, ["hsCode", "hsSgn", "itemCd", "cmdtCd", "hsCd"]) or hs_hint or ""

    if period is None or amount is None:
        return None

    return {"month": period, "hsCode": str(hs_code), "expDlr": float(amount)}


def parse_export_xml(text: str | bytes, hs_hint: str | None = None) -> list[dict[str, Any]]:
    root = ET.fromstring(text)
    rows = []

    for item in root.iter():
        if _local_tag(item.tag).lower() != "item":
            continue
        d = {_local_tag(child.tag): (child.text or "").strip() for child in list(item)}
        rec = _record_from_dict(d, hs_hint=hs_hint)
        if rec:
            rows.append(rec)

    return rows


def _walk_json_records(obj: Any) -> list[dict[str, Any]]:
    records = []
    if isinstance(obj, dict):
        rec = _record_from_dict(obj)
        if rec:
            records.append(rec)
        for v in obj.values():
            records.extend(_walk_json_records(v))
    elif isinstance(obj, list):
        for x in obj:
            records.extend(_walk_json_records(x))
    return records


def parse_export_payload(payload: bytes, hs_hint: str | None = None) -> list[dict[str, Any]]:
    text = payload.decode("utf-8", errors="ignore").strip()
    if not text:
        return []

    if text.startswith("{") or text.startswith("["):
        try:
            return _walk_json_records(json.loads(text))
        except Exception:
            return []

    try:
        return parse_export_xml(text, hs_hint=hs_hint)
    except Exception:
        return []


def get_with_service_key(url: str, params: dict[str, Any]) -> requests.Response:
    params = dict(params)
    key = str(params.pop("serviceKey"))

    # data.go.kr의 Encoding 키를 그대로 넣은 경우 이중 인코딩을 피합니다.
    if "%" in key:
        query = "serviceKey=" + key
        rest = urlencode(params)
        if rest:
            query += "&" + rest
        full_url = url + "?" + query
        return requests.get(full_url, timeout=30)

    params["serviceKey"] = key
    return requests.get(url, params=params, timeout=30)


def call_kcs_itemtrade(hs_code: str, start_ym: str, end_ym: str) -> list[dict[str, Any]]:
    key = require_env("DATA_GO_KR_SERVICE_KEY")

    # 관세청 기존 직접호출(openapi.customs.go.kr)은 GW 방식으로 대체/폐기 공지가 있었으므로
    # data.go.kr Gateway endpoint만 사용합니다.
    url = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
    param_sets = [
        {"serviceKey": key, "strtYymm": start_ym, "endYymm": end_ym, "hsSgn": hs_code, "numOfRows": "200", "pageNo": "1"},
        {"serviceKey": key, "strtYymm": start_ym, "endYymm": end_ym, "hsCode": hs_code, "numOfRows": "200", "pageNo": "1"},
    ]

    last_status = None
    last_body = ""
    last_error = None

    for params in param_sets:
        try:
            r = get_with_service_key(url, params)
            last_status = r.status_code
            last_body = r.content[:600].decode("utf-8", errors="ignore")
            r.raise_for_status()
            rows = parse_export_payload(r.content, hs_hint=hs_code)
            if rows:
                return rows
        except Exception as e:
            last_error = e

    msg = f"status={last_status}, body={last_body[:300]}"
    if last_error:
        msg += f", error={last_error}"
    raise RuntimeError("관세청 GW API 호출/파싱 실패: " + sanitize_error(msg))


def _month_chunks(start_period: pd.Period, end_period: pd.Period, max_months: int = 12) -> list[tuple[pd.Period, pd.Period]]:
    """관세청 GW API는 시작~종료 조회기간을 1년 이내로 제한합니다.
    YoY 계산에는 13개월 이상이 필요하므로 여러 번 나눠 호출합니다.
    """
    chunks: list[tuple[pd.Period, pd.Period]] = []
    cur = start_period
    while cur <= end_period:
        chunk_end = min(cur + (max_months - 1), end_period)
        chunks.append((cur, chunk_end))
        cur = chunk_end + 1
    return chunks


def korea_semiconductor_exports_item() -> dict[str, Any]:
    today = pd.Timestamp.today()
    end_period = (today - pd.DateOffset(months=1)).to_period("M")
    start_period = end_period - 25

    all_rows = []
    chunk_errors = []

    # 8541: 반도체 디바이스, 8542: 집적회로. 반도체 수출 프록시로 합산합니다.
    for hs in ["8541", "8542"]:
        for start_p, end_p in _month_chunks(start_period, end_period, max_months=12):
            start_ym = start_p.strftime("%Y%m")
            end_ym = end_p.strftime("%Y%m")
            try:
                rows = call_kcs_itemtrade(hs, start_ym, end_ym)
                all_rows.extend(rows)
            except Exception as e:
                chunk_errors.append(f"HS{hs} {start_ym}-{end_ym}: {sanitize_error(e)}")

    if not all_rows:
        detail = " | ".join(chunk_errors[:4]) if chunk_errors else "응답 데이터 없음"
        raise RuntimeError("반도체 수출 데이터를 가져오지 못했습니다: " + detail)

    df = pd.DataFrame(all_rows)
    s = df.groupby("month")["expDlr"].sum().sort_index()
    s = s[s > 0]

    if len(s) < 14:
        raise RuntimeError("반도체 수출 YoY 계산 데이터가 부족합니다.")

    last_m = s.index[-1]
    prev12_m = last_m - 12
    if prev12_m not in s.index:
        raise RuntimeError("전년동월 데이터가 없어 YoY 계산을 할 수 없습니다.")

    yoy = float(s.loc[last_m] / s.loc[prev12_m] - 1)

    prev_yoy = 0.0
    prev_m = last_m - 1
    prev_m_12 = prev_m - 12
    if prev_m in s.index and prev_m_12 in s.index:
        prev_yoy = float(s.loc[prev_m] / s.loc[prev_m_12] - 1)

    delta = yoy - prev_yoy

    risk = 55 - yoy * 120 + max(0.0, -delta) * 80
    trend_z = -delta * 8

    value = f"{last_m} YoY {yoy:+.1%}, ${s.loc[last_m] / 1e9:.2f}B"
    change = f"전월 YoY 대비 {delta:+.1%}p"

    return make_item(
        name="반도체 수출 YoY",
        value=value,
        risk=risk,
        trend_z=trend_z,
        asof=str(last_m),
        weight=10,
        change=change,
    )


# ---------------------------------------------------------------------------
# Forward EPS / EPS Revision Ratio
# ---------------------------------------------------------------------------


def load_eps_symbols() -> list[str]:
    env_symbols = os.getenv("EPS_SYMBOLS")
    if env_symbols:
        symbols = [x.strip().upper() for x in env_symbols.split(",") if x.strip()]
        if symbols:
            return symbols

    if EPS_SYMBOLS_PATH.exists():
        df = pd.read_csv(EPS_SYMBOLS_PATH)
        if "symbol" in df.columns:
            symbols = [str(x).strip().upper() for x in df["symbol"].dropna().tolist() if str(x).strip()]
            if symbols:
                return symbols

    return DEFAULT_EPS_SYMBOLS


def forward_eps_revision_items() -> list[dict[str, Any]]:
    watchlist = load_eps_symbols()
    today = datetime.now().date().isoformat()
    current_rows = []
    symbol_errors = []

    for symbol in watchlist:
        try:
            est = fmp_forward_eps(symbol)
            if not est:
                symbol_errors.append(f"{symbol}: analyst estimates 응답 없음")
                continue
            current_rows.append(
                {
                    "run_date": today,
                    "symbol": symbol,
                    "eps": est["eps"],
                    "revenue": est.get("revenue"),
                    "marketCap": fmp_market_cap(symbol),
                }
            )
            time.sleep(0.05)
        except Exception as e:
            symbol_errors.append(f"{symbol}: {sanitize_error(e)}")
            continue

    if not current_rows:
        # FMP analyst-estimates는 키/플랜에 따라 403으로 막힐 수 있습니다.
        # 봇 전체를 실패시키지 않고, 해당 20점 비중을 커버리지에서 제외합니다.
        detail = "; ".join(symbol_errors[:2]) if symbol_errors else "원인 로그 없음"
        short_reason = "FMP analyst-estimates 접근 불가"
        if "403" in detail or "권한 오류" in detail:
            short_reason = "FMP 플랜/권한 부족"

        return [
            make_item(
                name="Forward EPS",
                value=short_reason,
                risk=50,
                trend_z=0,
                asof=today,
                weight=0,
                change="자동수집 제외: FMP 권한 필요",
            ),
            make_item(
                name="EPS Revision Ratio",
                value=short_reason,
                risk=50,
                trend_z=0,
                asof=today,
                weight=0,
                change="자동수집 제외: FMP 권한 필요",
            ),
        ]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = pd.DataFrame(current_rows)

    if EPS_HISTORY_PATH.exists():
        hist = pd.read_csv(EPS_HISTORY_PATH)
    else:
        hist = pd.DataFrame(columns=["run_date", "symbol", "eps", "revenue", "marketCap"])

    previous = hist[hist["run_date"] < today].copy() if not hist.empty else pd.DataFrame()

    out = pd.concat([hist, current], ignore_index=True)
    out = out.drop_duplicates(["run_date", "symbol"], keep="last")
    out.to_csv(EPS_HISTORY_PATH, index=False)

    if previous.empty:
        return [
            make_item(
                name="Forward EPS",
                value=f"기준값 수집 시작 ({len(current)} symbols)",
                risk=50,
                trend_z=0,
                asof=today,
                weight=10,
                change="첫 실행/기준값 저장",
            ),
            make_item(
                name="EPS Revision Ratio",
                value=f"기준값 수집 시작 ({len(current)} symbols)",
                risk=50,
                trend_z=0,
                asof=today,
                weight=10,
                change="첫 실행/기준값 저장",
            ),
        ]

    prev_latest = (
        previous.sort_values("run_date")
        .groupby("symbol")
        .tail(1)
        .rename(columns={"eps": "eps_prev", "marketCap": "marketCap_prev"})
    )

    joined = current.merge(prev_latest[["symbol", "eps_prev", "marketCap_prev"]], on="symbol", how="inner")
    joined = joined[(joined["eps"] > 0) & (joined["eps_prev"] > 0)].copy()

    if joined.empty:
        raise RuntimeError("Forward EPS 비교 가능한 이전 데이터가 없습니다.")

    joined["pct"] = joined["eps"] / joined["eps_prev"] - 1
    weights = joined["marketCap"].astype(float).clip(lower=1.0)
    avg_change = float(np.average(joined["pct"], weights=weights))

    threshold = 0.0005
    up = joined[joined["pct"] > threshold]
    down = joined[joined["pct"] < -threshold]

    up_w = float(up["marketCap"].sum())
    down_w = float(down["marketCap"].sum())

    if up_w + down_w > 0:
        revision_ratio = (up_w - down_w) / (up_w + down_w)
    else:
        revision_ratio = 0.0

    eps_risk = 50 - avg_change * 1000
    eps_trend_z = -avg_change * 100

    rev_risk = 50 - revision_ratio * 40
    rev_trend_z = -revision_ratio * 2

    return [
        make_item(
            name="Forward EPS",
            value=f"Watchlist EPS 변화 {avg_change:+.2%}",
            risk=eps_risk,
            trend_z=eps_trend_z,
            asof=today,
            weight=10,
            change=f"전회 대비, {len(joined)} symbols",
        ),
        make_item(
            name="EPS Revision Ratio",
            value=f"{revision_ratio:+.2f} | 상향 {len(up)} / 하향 {len(down)}",
            risk=rev_risk,
            trend_z=rev_trend_z,
            asof=today,
            weight=10,
            change="전회 대비 EPS 상향/하향 비율",
        ),
    ]


def fetch_advanced_items() -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []

    fetchers = [
        hyperscaler_capex_item,
        nvda_guidance_item,
        korea_semiconductor_exports_item,
    ]

    for fn in fetchers:
        try:
            items.append(fn())
        except Exception as e:
            errors.append(f"{fn.__name__}: {sanitize_error(e)}")

    try:
        items.extend(forward_eps_revision_items())
    except Exception as e:
        errors.append(f"forward_eps_revision_items: {sanitize_error(e)}")

    return items, errors
