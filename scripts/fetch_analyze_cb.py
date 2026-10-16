from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "cb_research_20260827"
RAW = OUT / "raw"
ANN_DIR = RAW / "announcements"
KLINE_DIR = RAW / "klines"
for directory in (OUT, RAW, ANN_DIR, KLINE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

SAMPLE_START = pd.Timestamp("2025-08-27")
SAMPLE_END = pd.Timestamp("2026-08-26")
ANNOUNCEMENT_BEGIN = "2024-07-01"
ANNOUNCEMENT_END = "2026-08-27"
PRICE_BEGIN = "20240701"
PRICE_END = "20260827"
BENCHMARK_CODE = "000985"  # 中证全指（宽基市场基准）
RNG = np.random.default_rng(20260827)


SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/139.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
)


def get_json(url: str, params: dict | None = None, retries: int = 4) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = SESSION.get(url, params=params, timeout=45)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # pragma: no cover - network recovery
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} attempts: {url}: {last_error}")


def load_or_fetch(path: Path, fetcher) -> dict | list:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    payload = fetcher()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def fetch_bond_list() -> list[dict]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_BOND_CB_LIST",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "pageNumber": 1,
        "pageSize": 500,
        "sortColumns": "PUBLIC_START_DATE,SECURITY_CODE",
        "sortTypes": "-1,1",
    }
    payload = get_json(url, params)
    if not payload.get("success") or not payload.get("result"):
        raise RuntimeError(f"Unexpected bond list payload: {payload}")
    return payload["result"]["data"]


def fetch_announcements(stock_code: str) -> list[dict]:
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    page = 1
    all_rows: list[dict] = []
    while True:
        params = {
            "sr": -1,
            "page_size": 100,
            "page_index": page,
            "ann_type": "A",
            "client_source": "web",
            "stock_list": stock_code,
            "f_node": 0,
            "s_node": 0,
            "begin_time": ANNOUNCEMENT_BEGIN,
            "end_time": ANNOUNCEMENT_END,
        }
        payload = get_json(url, params)
        block = payload.get("data") or {}
        rows = block.get("list") or []
        all_rows.extend(rows)
        total = int(block.get("total_hits") or len(all_rows))
        if not rows or len(all_rows) >= total:
            break
        page += 1
        if page > 30:
            raise RuntimeError(f"Announcement pagination guard hit for {stock_code}")
    # API can expose duplicate records through different categories.
    by_id: dict[str, dict] = {}
    for row in all_rows:
        by_id[row.get("art_code") or f"{row.get('notice_date')}|{row.get('title')}"] = row
    return list(by_id.values())


def normalize_title(title: str) -> str:
    return re.sub(r"[\s:：()（）【】\[\]]+", "", title or "")


def find_event_announcements(rows: list[dict], public_start: pd.Timestamp) -> dict:
    enriched = []
    for row in rows:
        title = row.get("title_ch") or row.get("title") or ""
        date = pd.to_datetime(row.get("notice_date"), errors="coerce")
        if pd.isna(date):
            continue
        enriched.append({**row, "_title": title, "_norm": normalize_title(title), "_date": date.normalize()})

    approval_candidates = []
    issue_candidates = []
    for row in enriched:
        title = row["_title"]
        norm = row["_norm"]
        date = row["_date"]
        is_cb = "可转换公司债券" in title or "可转债" in title
        approval_signal = (
            "同意注册批复" in norm
            or ("同意注册" in norm and ("证监会" in norm or "监督管理委员会" in norm))
            or ("同意" in norm and "发行" in norm and "注册的批复" in norm)
        )
        if is_cb and approval_signal and date <= public_start:
            approval_candidates.append(row)

        excluded_issue_terms = (
            "发行结果",
            "提示性公告",
            "中签",
            "配售结果",
            "中签率",
            "网上路演",
            "上市公告",
            "募集说明书",
            "保荐书",
            "法律意见书",
        )
        issue_signal = (
            is_cb
            and "发行公告" in norm
            and not any(term in norm for term in excluded_issue_terms)
            and date <= public_start
        )
        if issue_signal:
            issue_candidates.append(row)

    # Prefer the issuer's own approval announcement over the regulator's similarly dated item.
    def approval_rank(row: dict) -> tuple:
        norm = row["_norm"]
        issuer_specific = int("获得" in norm or "收到" in norm or "申请" in norm)
        return (row["_date"], issuer_specific, row.get("source_type") == "333")

    approval = max(approval_candidates, key=approval_rank) if approval_candidates else None

    # Prefer the latest original main announcement; later corrections/revisions are audit items,
    # not the first formal issue announcement event requested by the study.
    original_issue_candidates = [
        row
        for row in issue_candidates
        if not any(term in row["_norm"] for term in ("更正", "修订", "更新"))
    ]
    issue_pool = original_issue_candidates or issue_candidates
    issue = max(issue_pool, key=lambda row: row["_date"]) if issue_pool else None

    def slim(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {
            "date": row["_date"].strftime("%Y-%m-%d"),
            "title": row["_title"],
            "art_code": row.get("art_code"),
            "display_time": row.get("display_time"),
            "source_type": row.get("source_type"),
        }

    return {
        "approval": slim(approval),
        "issue": slim(issue),
        "approval_candidates": [slim(x) for x in sorted(approval_candidates, key=lambda x: x["_date"])],
        "issue_candidates": [slim(x) for x in sorted(issue_candidates, key=lambda x: x["_date"])],
    }


def secid(code: str) -> str:
    if code == BENCHMARK_CODE:
        return f"1.{code}"
    return f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"


def fetch_kline(code: str, adjusted: bool = True) -> dict:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid(code),
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 1 if adjusted else 0,
        "beg": PRICE_BEGIN,
        "end": PRICE_END,
        "lmt": 1000,
    }
    payload = get_json(url, params)
    if not payload.get("data") or not payload["data"].get("klines"):
        raise RuntimeError(f"No kline data for {code}: {payload}")
    return payload


def kline_frame(payload: dict) -> pd.DataFrame:
    cols = [
        "date",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "amplitude_pct",
        "daily_return_pct",
        "change",
        "turnover_pct",
    ]
    rows = [line.split(",") for line in payload["data"]["klines"]]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    for col in cols[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("date").set_index("date")


def previous_or_same(df: pd.DataFrame, event_date: pd.Timestamp) -> pd.Timestamp:
    eligible = df.index[df.index <= event_date]
    if len(eligible) == 0:
        raise KeyError(f"No trading day <= {event_date}")
    return eligible[-1]


def next_or_same(df: pd.DataFrame, event_date: pd.Timestamp) -> pd.Timestamp:
    eligible = df.index[df.index >= event_date]
    if len(eligible) == 0:
        raise KeyError(f"No trading day >= {event_date}")
    return eligible[0]


def previous_trading_day(df: pd.DataFrame, event_date: pd.Timestamp) -> pd.Timestamp:
    eligible = df.index[df.index < event_date]
    if len(eligible) == 0:
        raise KeyError(f"No trading day < {event_date}")
    return eligible[-1]


def next_trading_day(df: pd.DataFrame, event_date: pd.Timestamp) -> pd.Timestamp | None:
    eligible = df.index[df.index > event_date]
    return eligible[0] if len(eligible) else None


def close_return(df: pd.DataFrame, start_date: pd.Timestamp, end_date: pd.Timestamp) -> float:
    return float(df.loc[end_date, "close"] / df.loc[start_date, "close"] - 1.0)


def daily_metrics(df: pd.DataFrame, date: pd.Timestamp) -> dict:
    prev = previous_trading_day(df, date)
    row = df.loc[date]
    prev_close = float(df.loc[prev, "close"])
    return {
        "prev_trade_date": prev,
        "prev_close": prev_close,
        "open": float(row["open"]),
        "close": float(row["close"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "daily_return": float(row["close"] / prev_close - 1.0),
        "open_gap": float(row["open"] / prev_close - 1.0),
        "intraday_return": float(row["close"] / row["open"] - 1.0),
        "amplitude": float(row["amplitude_pct"] / 100.0),
        "turnover": float(row["turnover_pct"] / 100.0),
    }


def wilson_interval(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    z = stats.norm.ppf(1 - alpha / 2)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def bootstrap_mean_ci(values: np.ndarray, reps: int = 5000) -> tuple[float, float]:
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return (math.nan, math.nan)
    sampled = RNG.choice(values, size=(reps, len(values)), replace=True).mean(axis=1)
    return tuple(np.quantile(sampled, [0.025, 0.975]))


def one_sample_summary(series: pd.Series) -> dict:
    values = pd.to_numeric(series, errors="coerce").dropna().to_numpy(float)
    n = len(values)
    successes = int(np.sum(values > 0))
    low, high = wilson_interval(successes, n)
    boot_low, boot_high = bootstrap_mean_ci(values)
    t_p = float(stats.ttest_1samp(values, 0).pvalue) if n > 1 else math.nan
    try:
        w_p = float(stats.wilcoxon(values, alternative="two-sided").pvalue) if n > 1 else math.nan
    except ValueError:
        w_p = 1.0
    return {
        "n": n,
        "mean": float(np.mean(values)) if n else math.nan,
        "median": float(np.median(values)) if n else math.nan,
        "std": float(np.std(values, ddof=1)) if n > 1 else math.nan,
        "win_rate": successes / n if n else math.nan,
        "win_rate_ci_low": low,
        "win_rate_ci_high": high,
        "mean_ci_low": boot_low,
        "mean_ci_high": boot_high,
        "t_test_p": t_p,
        "wilcoxon_p": w_p,
    }


def bh_adjust(p_values: list[float]) -> list[float]:
    p = np.asarray(p_values, dtype=float)
    n = len(p)
    order = np.argsort(p)
    adjusted = np.empty(n, dtype=float)
    running = 1.0
    for rank_index in range(n - 1, -1, -1):
        idx = order[rank_index]
        rank = rank_index + 1
        running = min(running, p[idx] * n / rank)
        adjusted[idx] = running
    return adjusted.tolist()


def spearman_table(df: pd.DataFrame, pairs: list[tuple[str, str, str]]) -> pd.DataFrame:
    rows = []
    for label, xcol, ycol in pairs:
        pair = df[[xcol, ycol]].replace([np.inf, -np.inf], np.nan).dropna()
        rho, p = stats.spearmanr(pair[xcol], pair[ycol])
        rows.append({"test": label, "x": xcol, "y": ycol, "n": len(pair), "rho": rho, "p_value": p})
    result = pd.DataFrame(rows)
    result["q_value_bh"] = bh_adjust(result["p_value"].tolist())
    return result


def ols_hc3(df: pd.DataFrame, outcome: str, predictors: list[str], model_name: str) -> pd.DataFrame:
    data = df[[outcome] + predictors].replace([np.inf, -np.inf], np.nan).dropna().copy()
    y = data[outcome].to_numpy(float)
    X_raw = data[predictors].to_numpy(float)
    means = X_raw.mean(axis=0)
    stds = X_raw.std(axis=0, ddof=1)
    stds[stds == 0] = 1
    Xz = (X_raw - means) / stds
    X = np.column_stack([np.ones(len(data)), Xz])
    names = ["Intercept"] + [f"z({name})" for name in predictors]
    xtx_inv = np.linalg.pinv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    resid = y - X @ beta
    hat = np.sum((X @ xtx_inv) * X, axis=1)
    scaled = resid / np.clip(1 - hat, 1e-8, None)
    meat = X.T @ ((scaled[:, None] ** 2) * X)
    cov = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.diag(cov))
    dof = max(len(data) - X.shape[1], 1)
    tval = beta / se
    pval = 2 * stats.t.sf(np.abs(tval), dof)
    crit = stats.t.ppf(0.975, dof)
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else math.nan
    return pd.DataFrame(
        {
            "model": model_name,
            "outcome": outcome,
            "term": names,
            "coefficient": beta,
            "hc3_se": se,
            "t_value": tval,
            "p_value": pval,
            "ci_low": beta - crit * se,
            "ci_high": beta + crit * se,
            "n": len(data),
            "r_squared": r2,
        }
    )


def qcut_labels(series: pd.Series, q: int, prefix: str) -> pd.Series:
    labels = [f"{prefix}{i + 1}" for i in range(q)]
    return pd.qcut(series.rank(method="first"), q=q, labels=labels)


def grouped_summary(df: pd.DataFrame, group_col: str, outcomes: list[str]) -> pd.DataFrame:
    rows = []
    for group, subset in df.groupby(group_col, observed=True):
        for outcome in outcomes:
            stats_row = one_sample_summary(subset[outcome])
            rows.append(
                {
                    "group_variable": group_col,
                    "group": str(group),
                    "outcome": outcome,
                    "x_min": float(subset[group_col.replace("_quartile", "").replace("_tertile", "")].min())
                    if group_col.replace("_quartile", "").replace("_tertile", "") in subset
                    else math.nan,
                    "x_max": float(subset[group_col.replace("_quartile", "").replace("_tertile", "")].max())
                    if group_col.replace("_quartile", "").replace("_tertile", "") in subset
                    else math.nan,
                    **stats_row,
                }
            )
    return pd.DataFrame(rows)


def extreme_group_test(df: pd.DataFrame, group_col: str, low: str, high: str, outcome: str) -> dict:
    a = pd.to_numeric(df.loc[df[group_col] == low, outcome], errors="coerce").dropna().to_numpy(float)
    b = pd.to_numeric(df.loc[df[group_col] == high, outcome], errors="coerce").dropna().to_numpy(float)
    table = [[int(np.sum(a > 0)), int(np.sum(a <= 0))], [int(np.sum(b > 0)), int(np.sum(b <= 0))]]
    fisher = stats.fisher_exact(table, alternative="two-sided")
    mw = stats.mannwhitneyu(a, b, alternative="two-sided")
    return {
        "group_variable": group_col,
        "low_group": low,
        "high_group": high,
        "outcome": outcome,
        "n_low": len(a),
        "n_high": len(b),
        "mean_low": float(np.mean(a)),
        "mean_high": float(np.mean(b)),
        "win_rate_low": float(np.mean(a > 0)),
        "win_rate_high": float(np.mean(b > 0)),
        "fisher_odds_ratio": float(fisher.statistic),
        "fisher_p": float(fisher.pvalue),
        "mann_whitney_p": float(mw.pvalue),
    }


def main() -> None:
    all_bonds = load_or_fetch(RAW / "eastmoney_bond_list.json", fetch_bond_list)
    bonds = pd.DataFrame(all_bonds)
    bonds["PUBLIC_START_DATE"] = pd.to_datetime(bonds["PUBLIC_START_DATE"], errors="coerce")
    bonds["SECURITY_START_DATE"] = pd.to_datetime(bonds["SECURITY_START_DATE"], errors="coerce")
    bonds = bonds[
        bonds["PUBLIC_START_DATE"].between(SAMPLE_START, SAMPLE_END, inclusive="both")
    ].copy()
    bonds = bonds.sort_values(["PUBLIC_START_DATE", "SECURITY_CODE"]).reset_index(drop=True)
    print(f"Sample bonds: {len(bonds)}", flush=True)

    event_rows = []
    for idx, row in bonds.iterrows():
        stock = str(row["CONVERT_STOCK_CODE"]).zfill(6)
        ann_path = ANN_DIR / f"{stock}.json"
        announcements = load_or_fetch(ann_path, lambda s=stock: fetch_announcements(s))
        events = find_event_announcements(announcements, row["PUBLIC_START_DATE"])
        event_rows.append(
            {
                "stock_code": stock,
                "approval": events["approval"],
                "issue": events["issue"],
                "approval_candidate_count": len(events["approval_candidates"]),
                "issue_candidate_count": len(events["issue_candidates"]),
            }
        )
        if (idx + 1) % 10 == 0 or idx + 1 == len(bonds):
            print(f"Announcements {idx + 1}/{len(bonds)}", flush=True)

    (RAW / "matched_events.json").write_text(
        json.dumps(event_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    event_map = {row["stock_code"]: row for row in event_rows}
    missing_approval = [x["stock_code"] for x in event_rows if not x["approval"]]
    missing_issue = [x["stock_code"] for x in event_rows if not x["issue"]]
    print(f"Missing approval: {missing_approval}", flush=True)
    print(f"Missing issue: {missing_issue}", flush=True)

    benchmark_payload = load_or_fetch(
        KLINE_DIR / f"index_1_{BENCHMARK_CODE}.json",
        lambda: fetch_kline(BENCHMARK_CODE, adjusted=False),
    )
    benchmark = kline_frame(benchmark_payload)

    analyzed_rows = []
    for idx, row in bonds.iterrows():
        stock = str(row["CONVERT_STOCK_CODE"]).zfill(6)
        event = event_map[stock]
        if not event["approval"] or not event["issue"]:
            continue
        stock_payload = load_or_fetch(KLINE_DIR / f"{stock}.json", lambda s=stock: fetch_kline(s, True))
        prices = kline_frame(stock_payload)
        approval_notice = pd.Timestamp(event["approval"]["date"])
        issue_notice = pd.Timestamp(event["issue"]["date"])
        record_notice = pd.Timestamp(row["SECURITY_START_DATE"]).normalize()
        subscription_notice = pd.Timestamp(row["PUBLIC_START_DATE"]).normalize()

        approval_trade = previous_or_same(prices, approval_notice)
        issue_trade = next_or_same(prices, issue_notice)
        record_trade = next_or_same(prices, record_notice)
        subscription_trade = next_or_same(prices, subscription_notice)
        pre_issue_trade = previous_trading_day(prices, issue_trade)
        after_record_trade = next_trading_day(prices, record_trade)

        bench_approval = previous_or_same(benchmark, approval_trade)
        bench_issue = next_or_same(benchmark, issue_trade)
        bench_record = next_or_same(benchmark, record_trade)
        bench_subscription = next_or_same(benchmark, subscription_trade)

        stock_period_return = close_return(prices, approval_trade, issue_trade)
        bench_period_return = close_return(benchmark, bench_approval, bench_issue)
        period_log_excess = math.log1p(stock_period_return) - math.log1p(bench_period_return)
        gap_trading_days = int(((prices.index > approval_trade) & (prices.index <= issue_trade)).sum())
        gap_calendar_days = int((issue_notice - approval_notice).days)
        period_avg_daily_log_excess = period_log_excess / gap_trading_days if gap_trading_days else math.nan

        pre_issue_return = close_return(prices, approval_trade, pre_issue_trade)
        bench_pre_issue = previous_trading_day(benchmark, bench_issue)
        bench_pre_issue_return = close_return(benchmark, bench_approval, bench_pre_issue)

        issue_day = daily_metrics(prices, issue_trade)
        bench_issue_day = daily_metrics(benchmark, bench_issue)
        record_day = daily_metrics(prices, record_trade)
        bench_record_day = daily_metrics(benchmark, bench_record)
        subscription_day = daily_metrics(prices, subscription_trade)
        bench_subscription_day = daily_metrics(benchmark, bench_subscription)

        next_record_return = math.nan
        next_record_excess = math.nan
        if after_record_trade is not None:
            next_m = daily_metrics(prices, after_record_trade)
            bench_after = next_or_same(benchmark, after_record_trade)
            bench_next_m = daily_metrics(benchmark, bench_after)
            next_record_return = next_m["daily_return"]
            next_record_excess = next_record_return - bench_next_m["daily_return"]

        allotment = float(row["FIRST_PER_PREPLACING"])
        issue_close = float(prices.loc[issue_trade, "close"])
        record_close = float(prices.loc[record_trade, "close"])
        rights_pre_pct = allotment / issue_close
        rights_record_pct = allotment / record_close

        analyzed_rows.append(
            {
                "stock_code": stock,
                "stock_name": row["SECURITY_SHORT_NAME"],
                "bond_code": row["SECURITY_CODE"],
                "bond_name": row["SECURITY_NAME_ABBR"],
                "rating": row.get("RATING"),
                "issue_scale_100m": float(row["ACTUAL_ISSUE_SCALE"]),
                "allotment_yuan_per_share": allotment,
                "initial_conversion_price": float(row["INITIAL_TRANSFER_PRICE"]),
                "approval_notice_date": approval_notice,
                "approval_trade_date": approval_trade,
                "issue_announcement_date": issue_notice,
                "issue_announcement_trade_date": issue_trade,
                "record_date": record_trade,
                "subscription_date": subscription_trade,
                "gap_calendar_days": gap_calendar_days,
                "gap_trading_days": gap_trading_days,
                "approval_close_qfq": float(prices.loc[approval_trade, "close"]),
                "issue_close_qfq": issue_close,
                "record_close_qfq": record_close,
                "rights_content_pre_pct": rights_pre_pct,
                "rights_content_record_pct": rights_record_pct,
                "period_return": stock_period_return,
                "benchmark_period_return": bench_period_return,
                "period_excess_return": stock_period_return - bench_period_return,
                "period_log_excess_return": period_log_excess,
                "period_avg_daily_log_excess": period_avg_daily_log_excess,
                "pre_issue_return": pre_issue_return,
                "pre_issue_excess_return": pre_issue_return - bench_pre_issue_return,
                "issue_open_gap": issue_day["open_gap"],
                "issue_intraday_return": issue_day["intraday_return"],
                "issue_day_return": issue_day["daily_return"],
                "issue_day_excess_return": issue_day["daily_return"] - bench_issue_day["daily_return"],
                "record_open_gap": record_day["open_gap"],
                "record_intraday_return": record_day["intraday_return"],
                "record_day_return": record_day["daily_return"],
                "record_day_excess_return": record_day["daily_return"] - bench_record_day["daily_return"],
                "record_day_amplitude": record_day["amplitude"],
                "record_day_turnover": record_day["turnover"],
                "subscription_open_gap": subscription_day["open_gap"],
                "subscription_intraday_return": subscription_day["intraday_return"],
                "subscription_day_return": subscription_day["daily_return"],
                "subscription_day_excess_return": subscription_day["daily_return"]
                - bench_subscription_day["daily_return"],
                "next_record_day_return": next_record_return,
                "next_record_day_excess_return": next_record_excess,
                "approval_announcement_title": event["approval"]["title"],
                "approval_art_code": event["approval"]["art_code"],
                "issue_announcement_title": event["issue"]["title"],
                "issue_art_code": event["issue"]["art_code"],
                "approval_source_url": f"https://data.eastmoney.com/notices/detail/{stock}/{event['approval']['art_code']}.html",
                "issue_source_url": f"https://data.eastmoney.com/notices/detail/{stock}/{event['issue']['art_code']}.html",
            }
        )
        if (idx + 1) % 10 == 0 or idx + 1 == len(bonds):
            print(f"Prices/analyzed {idx + 1}/{len(bonds)}", flush=True)

    data = pd.DataFrame(analyzed_rows)
    for date_col in [
        "approval_notice_date",
        "approval_trade_date",
        "issue_announcement_date",
        "issue_announcement_trade_date",
        "record_date",
        "subscription_date",
    ]:
        data[date_col] = pd.to_datetime(data[date_col])

    data["log_issue_scale"] = np.log(data["issue_scale_100m"])
    data["log_gap_trading_days"] = np.log1p(data["gap_trading_days"])
    data["rights_content_pre_pct_quartile"] = qcut_labels(
        data["rights_content_pre_pct"], 4, "Q"
    ).astype(str)
    data["gap_trading_days_tertile"] = qcut_labels(data["gap_trading_days"], 3, "T").astype(str)

    summary_metrics = [
        "period_return",
        "period_excess_return",
        "pre_issue_return",
        "pre_issue_excess_return",
        "issue_open_gap",
        "issue_intraday_return",
        "issue_day_return",
        "issue_day_excess_return",
        "record_open_gap",
        "record_intraday_return",
        "record_day_return",
        "record_day_excess_return",
        "subscription_open_gap",
        "subscription_intraday_return",
        "subscription_day_return",
        "subscription_day_excess_return",
        "next_record_day_return",
        "next_record_day_excess_return",
    ]
    summaries = pd.DataFrame(
        [{"metric": metric, **one_sample_summary(data[metric])} for metric in summary_metrics]
    )

    correlation_pairs = [
        ("含权率 vs 两公告区间收益", "rights_content_pre_pct", "period_return"),
        ("含权率 vs 两公告区间超额", "rights_content_pre_pct", "period_excess_return"),
        ("含权率 vs 发行公告日收益", "rights_content_pre_pct", "issue_day_return"),
        ("含权率 vs 登记日收益", "rights_content_pre_pct", "record_day_return"),
        ("含权率 vs 登记日超额", "rights_content_pre_pct", "record_day_excess_return"),
        ("每股配售额 vs 两公告区间超额", "allotment_yuan_per_share", "period_excess_return"),
        ("每股配售额 vs 登记日超额", "allotment_yuan_per_share", "record_day_excess_return"),
        ("交易日跨度 vs 两公告区间收益", "gap_trading_days", "period_return"),
        ("交易日跨度 vs 两公告区间超额", "gap_trading_days", "period_excess_return"),
        ("交易日跨度 vs 日均超额", "gap_trading_days", "period_avg_daily_log_excess"),
        ("发行规模 vs 两公告区间超额", "issue_scale_100m", "period_excess_return"),
        ("发行规模 vs 登记日超额", "issue_scale_100m", "record_day_excess_return"),
    ]
    correlations = spearman_table(data, correlation_pairs)

    group_summaries = pd.concat(
        [
            grouped_summary(
                data,
                "rights_content_pre_pct_quartile",
                ["period_return", "period_excess_return", "record_day_return", "record_day_excess_return"],
            ),
            grouped_summary(
                data,
                "gap_trading_days_tertile",
                ["period_return", "period_excess_return", "period_avg_daily_log_excess", "record_day_return"],
            ),
        ],
        ignore_index=True,
    )
    extreme_tests = pd.DataFrame(
        [
            extreme_group_test(data, "rights_content_pre_pct_quartile", "Q1", "Q4", outcome)
            for outcome in ["period_return", "period_excess_return", "record_day_return", "record_day_excess_return"]
        ]
        + [
            extreme_group_test(data, "gap_trading_days_tertile", "T1", "T3", outcome)
            for outcome in ["period_return", "period_excess_return", "period_avg_daily_log_excess"]
        ]
    )

    regressions = pd.concat(
        [
            ols_hc3(
                data,
                "period_return",
                ["benchmark_period_return", "rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale"],
                "区间原始收益（控制大盘）",
            ),
            ols_hc3(
                data,
                "period_excess_return",
                ["rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale"],
                "区间超额收益",
            ),
            ols_hc3(
                data,
                "record_day_excess_return",
                ["rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale", "period_excess_return"],
                "登记日超额收益",
            ),
            ols_hc3(
                data.assign(period_excess_positive=(data["period_excess_return"] > 0).astype(float)),
                "period_excess_positive",
                ["rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale"],
                "区间跑赢概率（线性概率模型）",
            ),
            ols_hc3(
                data.assign(record_excess_positive=(data["record_day_excess_return"] > 0).astype(float)),
                "record_excess_positive",
                ["rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale", "period_excess_return"],
                "登记日跑赢概率（线性概率模型）",
            ),
        ],
        ignore_index=True,
    )

    # Winsorized OLS at 2.5%/97.5% provides a concise outlier robustness check.
    winsor = data.copy()
    for col in ["period_return", "period_excess_return", "record_day_excess_return"]:
        lo, hi = winsor[col].quantile([0.025, 0.975])
        winsor[col] = winsor[col].clip(lo, hi)
    regressions_winsor = pd.concat(
        [
            ols_hc3(
                winsor,
                "period_excess_return",
                ["rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale"],
                "区间超额收益（2.5%缩尾）",
            ),
            ols_hc3(
                winsor,
                "record_day_excess_return",
                ["rights_content_pre_pct", "log_gap_trading_days", "log_issue_scale", "period_excess_return"],
                "登记日超额收益（2.5%缩尾）",
            ),
        ],
        ignore_index=True,
    )

    # Save machine-readable and flat outputs for audit and workbook generation.
    data.to_csv(OUT / "sample_data.csv", index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
    summaries.to_csv(OUT / "summary_statistics.csv", index=False, encoding="utf-8-sig")
    correlations.to_csv(OUT / "correlations.csv", index=False, encoding="utf-8-sig")
    group_summaries.to_csv(OUT / "group_summaries.csv", index=False, encoding="utf-8-sig")
    extreme_tests.to_csv(OUT / "extreme_group_tests.csv", index=False, encoding="utf-8-sig")
    regressions.to_csv(OUT / "regressions_hc3.csv", index=False, encoding="utf-8-sig")
    regressions_winsor.to_csv(OUT / "regressions_winsor_hc3.csv", index=False, encoding="utf-8-sig")

    # Curated Chinese-language tables for the user-facing workbook.
    workbook_columns = {
        "stock_code": "正股代码",
        "stock_name": "正股名称",
        "bond_code": "转债代码",
        "bond_name": "转债名称",
        "rating": "评级",
        "issue_scale_100m": "发行规模_亿元",
        "allotment_yuan_per_share": "每股配售额_元",
        "initial_conversion_price": "初始转股价_元",
        "approval_notice_date": "同意注册公告日",
        "issue_announcement_date": "正式发行公告日",
        "record_date": "股权登记日",
        "subscription_date": "申购日",
        "gap_calendar_days": "公告跨度_自然日",
        "gap_trading_days": "公告跨度_交易日",
        "approval_close_qfq": "同意注册日收盘_前复权",
        "issue_close_qfq": "发行公告日收盘_前复权",
        "record_close_qfq": "登记日收盘_前复权",
        "rights_content_pre_pct": "含权率_发行公告日",
        "rights_content_record_pct": "含权率_登记日",
        "period_return": "两公告区间收益",
        "benchmark_period_return": "同期中证全指收益",
        "period_excess_return": "两公告区间超额",
        "pre_issue_excess_return": "发行公告日前区间超额",
        "issue_open_gap": "发行公告日开盘缺口",
        "issue_intraday_return": "发行公告日日内收益",
        "issue_day_return": "发行公告日收益",
        "issue_day_excess_return": "发行公告日超额",
        "record_open_gap": "登记日开盘缺口",
        "record_intraday_return": "登记日日内收益",
        "record_day_return": "登记日收益",
        "record_day_excess_return": "登记日超额",
        "subscription_open_gap": "申购日开盘缺口",
        "subscription_intraday_return": "申购日日内收益",
        "subscription_day_return": "申购日收益",
        "subscription_day_excess_return": "申购日超额",
        "rights_content_pre_pct_quartile": "含权率四分位",
        "gap_trading_days_tertile": "跨度三分位",
        "approval_announcement_title": "同意注册公告标题",
        "issue_announcement_title": "发行公告标题",
        "approval_source_url": "同意注册公告来源",
        "issue_source_url": "发行公告来源",
    }
    workbook_data = data[list(workbook_columns)].rename(columns=workbook_columns)
    workbook_data.to_csv(OUT / "workbook_data.csv", index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")

    metric_names = {
        "period_return": "两公告区间收益",
        "period_excess_return": "两公告区间超额",
        "pre_issue_return": "正式发行公告日前区间收益",
        "pre_issue_excess_return": "正式发行公告日前区间超额",
        "issue_open_gap": "正式发行公告日开盘缺口",
        "issue_intraday_return": "正式发行公告日日内收益",
        "issue_day_return": "正式发行公告日收益",
        "issue_day_excess_return": "正式发行公告日超额",
        "record_open_gap": "股权登记日开盘缺口",
        "record_intraday_return": "股权登记日日内收益",
        "record_day_return": "股权登记日收益",
        "record_day_excess_return": "股权登记日超额",
        "subscription_open_gap": "申购日开盘缺口",
        "subscription_intraday_return": "申购日日内收益",
        "subscription_day_return": "申购日收益",
        "subscription_day_excess_return": "申购日超额",
        "next_record_day_return": "登记日后下一交易日收益",
        "next_record_day_excess_return": "登记日后下一交易日超额",
    }
    workbook_stats = summaries.copy()
    workbook_stats["metric"] = workbook_stats["metric"].map(metric_names).fillna(workbook_stats["metric"])
    workbook_stats = workbook_stats.rename(
        columns={
            "metric": "指标",
            "n": "样本数",
            "mean": "均值",
            "median": "中位数",
            "std": "标准差",
            "win_rate": "上涨概率",
            "win_rate_ci_low": "上涨概率95%CI下限",
            "win_rate_ci_high": "上涨概率95%CI上限",
            "mean_ci_low": "均值Bootstrap95%CI下限",
            "mean_ci_high": "均值Bootstrap95%CI上限",
            "t_test_p": "均值t检验p值",
            "wilcoxon_p": "Wilcoxon检验p值",
        }
    )
    workbook_stats.to_csv(OUT / "workbook_statistics.csv", index=False, encoding="utf-8-sig")

    workbook_corr = correlations.rename(
        columns={
            "test": "检验",
            "x": "解释变量",
            "y": "结果变量",
            "n": "样本数",
            "rho": "Spearman相关系数",
            "p_value": "p值",
            "q_value_bh": "BH校正q值",
        }
    )
    workbook_corr.to_csv(OUT / "workbook_correlations.csv", index=False, encoding="utf-8-sig")

    workbook_group = group_summaries.rename(
        columns={
            "group_variable": "分组变量",
            "group": "分组",
            "outcome": "结果变量",
            "x_min": "组内下限",
            "x_max": "组内上限",
            "n": "样本数",
            "mean": "均值",
            "median": "中位数",
            "std": "标准差",
            "win_rate": "上涨概率",
            "win_rate_ci_low": "上涨概率95%CI下限",
            "win_rate_ci_high": "上涨概率95%CI上限",
            "mean_ci_low": "均值95%CI下限",
            "mean_ci_high": "均值95%CI上限",
            "t_test_p": "t检验p值",
            "wilcoxon_p": "Wilcoxon检验p值",
        }
    )
    workbook_group["分组变量"] = workbook_group["分组变量"].replace(
        {"rights_content_pre_pct_quartile": "含权率四分位", "gap_trading_days_tertile": "公告跨度三分位"}
    )
    workbook_group["结果变量"] = workbook_group["结果变量"].map(metric_names).fillna(workbook_group["结果变量"])
    workbook_group.to_csv(OUT / "workbook_groups.csv", index=False, encoding="utf-8-sig")

    workbook_reg = pd.concat([regressions, regressions_winsor], ignore_index=True)
    workbook_reg = workbook_reg.rename(
        columns={
            "model": "模型",
            "outcome": "结果变量",
            "term": "解释变量",
            "coefficient": "标准化系数",
            "hc3_se": "HC3稳健标准误",
            "t_value": "t值",
            "p_value": "p值",
            "ci_low": "95%CI下限",
            "ci_high": "95%CI上限",
            "n": "样本数",
            "r_squared": "R方",
        }
    )
    workbook_reg.to_csv(OUT / "workbook_regressions.csv", index=False, encoding="utf-8-sig")

    metadata = {
        "sample_start": SAMPLE_START.strftime("%Y-%m-%d"),
        "sample_end": SAMPLE_END.strftime("%Y-%m-%d"),
        "sample_size_initial": int(len(bonds)),
        "sample_size_analyzed": int(len(data)),
        "missing_approval": missing_approval,
        "missing_issue": missing_issue,
        "benchmark_code": BENCHMARK_CODE,
        "benchmark_name": "中证全指",
        "price_adjustment": "正股前复权；指数不复权",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "sources": {
            "bond_list": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_BOND_CB_LIST",
            "announcements": "https://np-anotice-stock.eastmoney.com/api/security/ann",
            "prices": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            "exchange_announcements_sse": "https://bond.sse.com.cn/disclosure/announ/convertible/",
            "exchange_disclosure_szse": "https://www.szse.cn/disclosure/listed/notice/",
        },
    }
    (OUT / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
