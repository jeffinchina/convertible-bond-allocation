from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from monitor import (
    Announcement,
    HttpClient,
    absolute_url,
    atomic_write_json,
    clean_text,
    download_evidence,
    escape,
    fetch_company_concepts,
    fetch_company_profile,
    fetch_market_snapshot,
    fetch_sse,
    find_list,
    iso_date,
    normalized_title,
    parse_formal_terms,
    pdf_text,
    pick,
    previous_market_day,
    read_json,
    safe_file_name,
    send_email,
    strip_terminal_escapes,
    subscription_reminder_lock,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
QUEUE_PATH = ROOT / "bond_lifecycle_reminders.json"
STATE_PATH = ROOT / "bond_lifecycle_state.json"
LOCK_PATH = ROOT / "bond_lifecycle_reminders.lock"
SUBSCRIPTION_QUEUE_PATH = ROOT / "subscription_reminders.json"
EVIDENCE_DIR = ROOT / "evidence" / "bond-lifecycle"
REPORTS_DIR = ROOT / "reports"
RAW_DIR = ROOT / "raw" / "bond-lifecycle"

REMINDER_TIME = dt.time(9, 15)
REMINDER_CUTOFF = dt.time(15, 0)
INITIAL_LOOKBACK_DAYS = 90
DISCOVERY_OVERLAP_DAYS = 3
QUEUE_RETENTION_DAYS = 180

OFFICIAL_DOMAINS = {
    "sse": "sse.com.cn",
    "szse": "szse.cn",
}
HARD_EXCLUSIONS = (
    "向特定对象发行",
    "发行股份购买资产",
    "可交换公司债券",
)


def _record_value(record: Announcement | dict[str, Any], field: str) -> Any:
    if isinstance(record, dict):
        return record.get(field)
    return getattr(record, field, None)


def lifecycle_source_key(record: Announcement | dict[str, Any]) -> str:
    # The shared SSE adapter's source_id can be a bulletin category, so its
    # generic key is not a unique document identifier. Include the URL/date.
    seed = "|".join(
        clean_text(_record_value(record, field))
        for field in ("source", "code", "source_id", "url", "date", "title")
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def classify_lifecycle_title(title: str) -> tuple[str, str]:
    """Classify only actionable post-subscription convertible-bond notices."""

    value = normalized_title(title)
    if "可转换公司债券" not in value:
        return "none", "不含完整的可转换公司债券主题词"
    excluded = next((word for word in HARD_EXCLUSIONS if word in value), None)
    if excluded:
        return "none", f"排除：{excluded}"
    if "中签率" in value or "优先配售结果" in value:
        return "none", "排除：中签率或优先配售结果并非个人出签结果"
    if any(
        signal in value
        for signal in (
            "网上中签结果公告",
            "中签号码公告",
            "摇号中签结果公告",
            "摇号抽签结果公告",
        )
    ):
        return "draw", "命中：官方网上中签结果/中签号码公告"
    if "上市公告书" in value:
        return "listing", "命中：可转换公司债券上市公告书"
    return "none", "非出签或上市提醒公告"


def is_official_exchange_url(source: str, value: Any) -> bool:
    domain = OFFICIAL_DOMAINS.get(clean_text(source).lower())
    try:
        parsed = urlparse(clean_text(value))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(
        domain
        and parsed.scheme == "https"
        and (host == domain or host.endswith("." + domain))
    )


def lifecycle_document_matches(kind: str, text: Any) -> bool:
    """Confirm the downloaded official PDF is the notice named by the API row."""

    # Validate the cover/title area.  Full listing prospectuses can mention
    # excluded security types much later in boilerplate and must not turn a
    # correct cover page into a false rejection.
    value = normalized_title(str(text or "")[:1000])
    if "可转换公司债券" not in value:
        return False
    if any(word in value for word in HARD_EXCLUSIONS):
        return False
    if kind == "draw":
        return any(
            signal in value
            for signal in (
                "网上中签结果公告",
                "中签号码公告",
                "摇号中签结果公告",
                "摇号抽签结果公告",
            )
        )
    if kind == "listing":
        return "上市公告书" in value
    return False


def _cn_date(value: str) -> str | None:
    match = re.search(
        r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?",
        value,
    )
    if not match:
        return None
    try:
        return dt.date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def _first_match(patterns: Iterable[str], value: str, flags: int = 0) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, value, flags)
        if match:
            return clean_text(match.group(1))
    return None


def _normalize_bond_short_name(value: Any) -> str | None:
    name = re.sub(r"\s+", "", clean_text(value))
    if not name or len(name) > 12:
        return None
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{1,10}(?:转债|转\d{1,2})", name):
        return None
    if any(word in name for word in ("代码", "简称", "上市", "日期", "扩位")):
        return None
    return name


def parse_lifecycle_terms(text: str, kind: str) -> dict[str, Any]:
    """Extract deterministic fields from an official result/listing PDF."""

    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw)
    try:
        formal = parse_formal_terms(raw)
    except Exception:
        formal = {}

    bond_name = _normalize_bond_short_name(formal.get("bondShortName"))
    if not bond_name:
        captured = _first_match(
            (
                r"(?:可转换公司债券|可转债|债券)(?:简称|名称)(?:为)?[：:]?[‘“\"']?([\u4e00-\u9fffA-Za-z0-9]{1,10}(?:转债|转\d{1,2}))",
                r"简称(?:为)?[：:]?[‘“\"']?([\u4e00-\u9fffA-Za-z0-9]{1,10}(?:转债|转\d{1,2}))",
                r"以下简称[‘“\"']?([\u4e00-\u9fffA-Za-z0-9]{1,10}(?:转债|转\d{1,2}))[’”\"']?",
            ),
            compact,
        )
        bond_name = _normalize_bond_short_name(captured)

    bond_code = clean_text(formal.get("bondCode"))
    if not re.fullmatch(r"\d{6}", bond_code):
        bond_code = _first_match(
            (
                r"(?:可转换公司债券|可转债|债券)(?:代码|证券代码)[：:]?(\d{6})",
                r"债券代码[：:]?(\d{6})",
            ),
            compact,
        ) or ""

    listing_date = None
    if kind == "listing":
        listing_date = _cn_date(
            _first_match(
                (
                    r"上市(?:日期|时间)[：:]?((?:20\d{2})\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)",
                    r"(?:20\d{2})年可转换公司债券于((?:20\d{2})\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)上市",
                ),
                compact,
            )
            or ""
        )

    payment_date = None
    if kind == "draw":
        payment_text = _first_match(
            (
                r"(?:缴款日期|缴款日|缴款时间)[：:]?((?:20\d{2})\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)",
                r"资金账户(?:应)?在((?:20\d{2})\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)",
                r"((?:20\d{2})\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?).{0,100}?(?:中签投资者|网上投资者).{0,100}?(?:资金账户|认购资金)",
                r"(?:中签投资者|网上投资者).{0,100}?((?:20\d{2})\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?).{0,100}?(?:资金账户|认购资金|缴款)",
            ),
            compact,
        )
        payment_date = _cn_date(payment_text or "")

    lot_match = re.search(
        r"每个中签号码(?:只能|可)?认购(\d+)(手|张).{0,30}?([\d,]+)元",
        compact,
    )
    lot_quantity = int(lot_match.group(1)) if lot_match else None
    lot_unit = lot_match.group(2) if lot_match else None
    lot_count = (
        lot_quantity
        if lot_unit == "手"
        else lot_quantity / 10
        if lot_unit == "张" and lot_quantity is not None and lot_quantity % 10 == 0
        else None
    )
    lot_amount = (
        float(lot_match.group(3).replace(",", "")) if lot_match else None
    )
    amount = formal.get("finalAmount") or formal.get("registeredAmount") or {}
    return {
        "bondShortName": bond_name,
        "bondCode": bond_code or None,
        "listingDate": listing_date,
        "paymentDate": payment_date,
        "lotCount": lot_count,
        "lotQuantity": lot_quantity,
        "lotUnit": lot_unit,
        "lotAmountYuan": lot_amount,
        "issueAmountYuan": amount.get("valueYuan") if isinstance(amount, dict) else None,
        "initialConversionPrice": formal.get("initialConversionPrice"),
        "creditRating": formal.get("creditRating"),
        "termYears": formal.get("termYears"),
    }


def reminder_due(entry: dict[str, Any], now: dt.datetime) -> bool:
    if entry.get("status") == "superseded" or entry.get("supersededBy"):
        return False
    if entry.get("sentAt"):
        return False
    try:
        target = dt.date.fromisoformat(str(entry.get("targetDate")))
    except (TypeError, ValueError):
        return False
    local_time = now.timetz().replace(tzinfo=None)
    return (
        target == now.date()
        and REMINDER_TIME <= local_time < REMINDER_CUTOFF
    )


def lifecycle_subject(entry: dict[str, Any]) -> str:
    prefix = "【可转债出签】" if entry.get("kind") == "draw" else "【可转债上市】"
    return prefix + str(entry["bondShortName"])


def _display(value: Any, suffix: str = "") -> str:
    if value in (None, "", []):
        return "未披露"
    return escape(f"{value}{suffix}")


def build_lifecycle_html(entry: dict[str, Any]) -> str:
    kind = entry.get("kind")
    amount = entry.get("issueAmountYuan")
    amount_text = f"{float(amount) / 100000000:.2f}亿元" if amount else "未披露"
    price = entry.get("initialConversionPrice")
    price_text = f"{float(price):.2f}元" if price else "未披露"
    close = entry.get("stockClose")
    close_text = f"{float(close):.2f}元" if close else "未披露"
    value = entry.get("conversionValue")
    value_text = f"{float(value):.2f}元" if value else "未披露"
    term = entry.get("termYears")
    term_text = f"{int(term)}年" if term else "未披露"
    concepts = "、".join(entry.get("concepts") or []) or "暂无可靠概念数据"
    official_url = clean_text(entry.get("officialUrl"))
    official_link = (
        f'<a href="{escape(official_url)}">{escape(entry.get("officialTitle") or "交易所原始公告")}</a>'
        if is_official_exchange_url(str(entry.get("officialSource") or ""), official_url)
        else escape(entry.get("officialTitle") or "交易所原始公告")
    )
    if kind == "draw":
        heading = f"今日可转债出签提醒：{escape(entry['bondShortName'])}"
        action = (
            "请登录券商账户查询是否中签；若中签，请在缴款截止前确保资金账户余额充足。"
            "本系统无法读取个人券商账户，邮件本身不代表已经中签。"
        )
        payment_basis = clean_text(entry.get("paymentDateBasis"))
        payment_text = _display(entry.get("paymentDate"))
        if payment_basis:
            payment_text += f'<br><span class="muted">{escape(payment_basis)}</span>'
        lot_amount = entry.get("lotAmountYuan")
        lot_amount_text = (
            escape(f"{float(lot_amount):,.0f}元")
            if lot_amount not in (None, "")
            else "未披露"
        )
        timing_rows = f"""
<tr><th>出签公告日</th><td>{_display(entry.get('announcementDate'))}</td></tr>
<tr><th>缴款日</th><td>{payment_text}</td></tr>
<tr><th>每个中签号码</th><td>{_display(entry.get('lotQuantity') if entry.get('lotQuantity') is not None else entry.get('lotCount'), entry.get('lotUnit') or '手')} / {lot_amount_text}</td></tr>"""
        footer = "请以券商账户中的实际中签结果、缴款金额和截止时间为准。"
    else:
        heading = f"今日可转债上市提醒：{escape(entry['bondShortName'])}"
        action = (
            "该可转债今日上市。若账户中持有，请在开盘前确认持仓与交易规则，"
            "并关注上市首日价格、临时停牌和成交情况。"
        )
        timing_rows = f"""
<tr><th>上市日期</th><td>{_display(entry.get('listingDate'))}</td></tr>
<tr><th>正股最近收盘</th><td>{escape(close_text)}（{_display(entry.get('stockCloseDate'))}）</td></tr>
<tr><th>参考转股价值</th><td>{escape(value_text)}</td></tr>"""
        footer = "是否卖出及成交价格取决于个人持仓、风险承受能力与当日市场，本邮件不提供交易指令。"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2937;line-height:1.55;margin:16px}}h2{{font-size:20px;margin:0 0 10px}}h3{{font-size:16px;margin:18px 0 6px}}table{{border-collapse:collapse;width:100%;max-width:720px}}th,td{{border:1px solid #d8dee8;padding:7px;text-align:left;vertical-align:top}}th{{width:34%;background:#f6f8fa}}.action{{background:#fff7d6;border-left:4px solid #e0a800;padding:10px;margin:10px 0}}.muted{{color:#5f6368;font-size:12px}}</style></head><body data-lifecycle-kind="{escape(kind)}">
<h2>{heading}</h2><div class="action"><strong>行动提醒：</strong>{escape(action)}</div>
<h3>新债基本信息</h3><table>
<tr><th>新债简称 / 代码</th><td>{_display(entry.get('bondShortName'))} / {_display(entry.get('bondCode'))}</td></tr>
{timing_rows}
<tr><th>发行规模</th><td>{escape(amount_text)}</td></tr>
<tr><th>初始转股价</th><td>{escape(price_text)}</td></tr>
<tr><th>债项评级 / 期限</th><td>{_display(entry.get('creditRating'))} / {escape(term_text)}</td></tr>
</table><h3>正股与题材</h3><table>
<tr><th>正股简称 / 代码</th><td>{_display(entry.get('stockName'))} / {_display(entry.get('stockCode'))}</td></tr>
<tr><th>行业</th><td>{_display(entry.get('industry'))}</td></tr>
<tr><th>主营业务</th><td>{_display(entry.get('mainBusiness'))}</td></tr>
<tr><th>题材概念</th><td>{escape(concepts)}</td></tr>
</table><p><strong>官方依据：</strong>{official_link}</p>
<p class="muted">{escape(footer)} 最终信息以交易所原始公告和券商页面为准；本邮件仅作时间提醒，不构成投资建议。</p>
</body></html>"""


def _master_entry(
    stock_code: str,
    bond_code: str | None,
    bond_name: str | None = None,
    announcement_date: str = "",
    subscription_queue_path: Path = SUBSCRIPTION_QUEUE_PATH,
) -> dict[str, Any]:
    queue = read_json(subscription_queue_path, {"entries": {}})
    matches = []
    for entry in (queue.get("entries") or {}).values():
        if str(entry.get("stockCode") or "") != stock_code:
            continue
        if entry.get("status") == "superseded" or entry.get("supersededBy"):
            continue
        if bond_code:
            if str(entry.get("bondCode") or "") != bond_code:
                continue
        elif not bond_name or _normalize_bond_short_name(entry.get("bondShortName")) != bond_name:
            continue
        if announcement_date and str(entry.get("subscriptionDate") or "") > announcement_date:
            continue
        matches.append(entry)
    if not matches:
        return {}
    return dict(max(matches, key=lambda item: str(item.get("updatedAt") or "")))


def _merge_optional(entry: dict[str, Any], master: dict[str, Any]) -> None:
    fields = (
        "stockName",
        "bondShortName",
        "bondCode",
        "issueAmountYuan",
        "initialConversionPrice",
        "creditRating",
        "termYears",
        "stockClose",
        "stockCloseDate",
        "conversionValue",
        "industry",
        "mainBusiness",
        "concepts",
    )
    for field in fields:
        if entry.get(field) in (None, "", []) and master.get(field) not in (None, "", []):
            entry[field] = master[field]


def build_lifecycle_entry(
    record: Announcement | dict[str, Any],
    client: HttpClient,
    now: dt.datetime,
    *,
    evidence_dir: Path = EVIDENCE_DIR,
    subscription_queue_path: Path = SUBSCRIPTION_QUEUE_PATH,
    download_func: Callable[..., dict[str, Any]] = download_evidence,
    pdf_text_func: Callable[[Path], str] = pdf_text,
    enrich: bool = True,
) -> tuple[dict[str, Any] | None, list[str]]:
    kind, _ = classify_lifecycle_title(str(_record_value(record, "title") or ""))
    if kind == "none":
        return None, []
    source = clean_text(_record_value(record, "source")).lower()
    source_url = clean_text(_record_value(record, "url"))
    if not is_official_exchange_url(source, source_url):
        return None, ["officialExchangeUrl"]
    stock_code = clean_text(_record_value(record, "code"))
    announcement_date = clean_text(_record_value(record, "date"))
    source_key = lifecycle_source_key(record)
    event_dir = evidence_dir / announcement_date / safe_file_name(
        f"{stock_code}_{kind}_{source_key}"
    )
    target = event_dir / "official-notice.pdf"
    referer = "https://www.sse.com.cn/" if source == "sse" else "https://www.szse.cn/"
    try:
        downloaded = download_func(client, source_url, target, referer)
    except Exception as exc:
        return None, [f"officialPdfDownload:{strip_terminal_escapes(exc)}"]
    final_url = clean_text(downloaded.get("url") or source_url)
    if not downloaded.get("pdfValidated"):
        return None, ["officialPdfInvalid"]
    if not is_official_exchange_url(source, final_url):
        return None, ["officialPdfRedirect"]
    document_path = Path(str(downloaded.get("path") or target))
    try:
        text = pdf_text_func(document_path)
    except Exception as exc:
        return None, [f"officialPdfText:{strip_terminal_escapes(exc)}"]
    if len(clean_text(text)) < 40:
        return None, ["officialPdfTextEmpty"]
    if not lifecycle_document_matches(kind, text):
        return None, ["officialPdfContentMismatch"]
    terms = parse_lifecycle_terms(text, kind)
    master = _master_entry(
        stock_code,
        terms.get("bondCode"),
        terms.get("bondShortName"),
        announcement_date,
        subscription_queue_path,
    )
    entry: dict[str, Any] = {
        "kind": kind,
        "sourceKey": source_key,
        "stockCode": stock_code,
        "stockName": clean_text(_record_value(record, "company")),
        "announcementDate": announcement_date,
        "bondShortName": terms.get("bondShortName"),
        "bondCode": terms.get("bondCode"),
        "listingDate": terms.get("listingDate"),
        "paymentDate": terms.get("paymentDate"),
        "lotCount": terms.get("lotCount"),
        "lotQuantity": terms.get("lotQuantity"),
        "lotUnit": terms.get("lotUnit"),
        "lotAmountYuan": terms.get("lotAmountYuan"),
        "issueAmountYuan": terms.get("issueAmountYuan"),
        "initialConversionPrice": terms.get("initialConversionPrice"),
        "creditRating": terms.get("creditRating"),
        "termYears": terms.get("termYears"),
        "officialSource": source,
        "officialTitle": clean_text(_record_value(record, "title")),
        "officialUrl": final_url,
        "officialSha256": clean_text(downloaded.get("sha256")),
        "status": "active",
        "updatedAt": now.isoformat(),
    }
    _merge_optional(entry, master)
    if kind == "draw" and not entry.get("paymentDate"):
        # The result/number notice itself is the T+2 action trigger.  Some SSE
        # notices omit a repeated "payment date" field, so retain the official
        # publication date as an explicitly labelled reminder-day fallback.
        entry["paymentDate"] = announcement_date
        entry["paymentDateBasis"] = "官方中签结果公告发布日期（请以券商截止时间为准）"
    if enrich and stock_code:
        if not entry.get("industry") or not entry.get("mainBusiness"):
            try:
                profile = fetch_company_profile(client, stock_code, event_dir / "company-profile")
                entry["industry"] = entry.get("industry") or profile.get("industry")
                entry["mainBusiness"] = entry.get("mainBusiness") or profile.get("mainBusiness")
            except Exception:
                pass
        if not entry.get("concepts"):
            try:
                concepts = fetch_company_concepts(client, stock_code, event_dir / "company-concepts")
                entry["concepts"] = list(concepts.get("concepts") or [])
            except Exception:
                pass
        if kind == "listing":
            try:
                market = fetch_market_snapshot(client, stock_code, now, event_dir / "market")
                entry["stockName"] = market.get("securityName") or entry.get("stockName")
                entry["stockClose"] = market.get("latestClose")
                entry["stockCloseDate"] = market.get("latestCloseDate")
                if entry.get("stockClose") and entry.get("initialConversionPrice"):
                    entry["conversionValue"] = round(
                        100
                        * float(entry["stockClose"])
                        / float(entry["initialConversionPrice"]),
                        2,
                    )
            except Exception:
                pass
    target_date = announcement_date if kind == "draw" else entry.get("listingDate")
    entry["targetDate"] = target_date
    required = {
        "stockCode": entry.get("stockCode"),
        "stockName": entry.get("stockName"),
        "bondShortName": entry.get("bondShortName"),
        "targetDate": target_date,
        "officialSha256": entry.get("officialSha256"),
    }
    if kind == "listing":
        required["bondCode"] = entry.get("bondCode")
    missing = [field for field, value in required.items() if not value]
    try:
        dt.date.fromisoformat(str(target_date))
    except (TypeError, ValueError):
        if "targetDate" not in missing:
            missing.append("targetDate")
    if missing:
        return None, sorted(set(missing))
    bond_identity = str(entry.get("bondCode") or f"{stock_code}:{entry['bondShortName']}")
    key_seed = "|".join([kind, bond_identity, str(target_date)])
    entry["reminderKey"] = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:24]
    return entry, []


def _same_bond(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_code, right_code = left.get("bondCode"), right.get("bondCode")
    if left_code and right_code:
        return str(left_code) == str(right_code)
    return (
        bool(left.get("stockCode"))
        and str(left.get("stockCode")) == str(right.get("stockCode"))
        and bool(left.get("bondShortName"))
        and _normalize_bond_short_name(left.get("bondShortName"))
        == _normalize_bond_short_name(right.get("bondShortName"))
    )


def _notice_order(entry: dict[str, Any]) -> tuple[str, int]:
    return (
        str(entry.get("announcementDate") or ""),
        int(any(word in str(entry.get("officialTitle") or "") for word in ("更正", "修订", "更新"))),
    )


def upsert_entries(
    queue: dict[str, Any], entries: Iterable[dict[str, Any]], now: dt.datetime
) -> int:
    stored = queue.setdefault("entries", {})
    changed = 0
    # APIs usually return newest first. Apply old notices first so a correction
    # cannot be undone by an older document later in the same discovery batch.
    ordered = sorted(entries, key=_notice_order)
    for entry in ordered:
        existing = stored.get(entry["reminderKey"]) or {}
        same_bond = [
            old for old in stored.values()
            if old.get("kind") == entry.get("kind")
            and _same_bond(old, entry)
        ]
        newest_order = max((_notice_order(old) for old in same_bond), default=("", 0))
        if _notice_order(entry) < newest_order:
            continue
        if existing.get("supersededBy") and _notice_order(entry) <= newest_order:
            continue
        same_target = [old for old in same_bond if old.get("targetDate") == entry.get("targetDate")]
        if not existing and same_target:
            existing = max(same_target, key=lambda item: (bool(item.get("sentAt")), _notice_order(item)))
        for field in ("sentAt", "duplicateAccepted", "preparedHtml", "preparedSubject", "preparedAt"):
            if field in existing:
                entry[field] = existing[field]
        for old_key, old in list(stored.items()):
            if old_key == entry["reminderKey"]:
                continue
            if (
                old.get("kind") == entry.get("kind")
                and _same_bond(old, entry)
            ):
                old["status"] = "superseded"
                old["supersededBy"] = entry["reminderKey"]
                old["supersededAt"] = now.isoformat()
        stored[entry["reminderKey"]] = entry
        changed += 1
    expiry = now.date() - dt.timedelta(days=QUEUE_RETENTION_DAYS)
    for key, entry in list(stored.items()):
        try:
            if dt.date.fromisoformat(str(entry.get("targetDate"))) < expiry:
                del stored[key]
        except (TypeError, ValueError):
            del stored[key]
    queue.update({"schemaVersion": 1, "updatedAt": now.isoformat()})
    return changed


def _fetch_complete_window(
    fetcher: Callable[..., tuple[list[Announcement], Any]],
    client: HttpClient,
    endpoint: str,
    start: dt.datetime,
    end: dt.datetime,
    max_pages: int,
) -> tuple[list[Announcement], list[dict[str, Any]]]:
    """Split date ranges when a paged endpoint truncates or repeats page one."""

    records, pages = fetcher(client, endpoint, start, end, max_pages)
    unique = {lifecycle_source_key(record): record for record in records}
    declared_total = 0
    effective_page_size = 100
    for page in pages if isinstance(pages, list) else [pages]:
        if not isinstance(page, dict):
            continue
        help_info = page.get("pageHelp") or page.get("PAGEHELP") or {}
        try:
            effective_page_size = int(page.get("_lifecyclePageSize") or effective_page_size)
        except (TypeError, ValueError):
            pass
        for value in (help_info.get("total"), page.get("announceCount"), page.get("total")):
            try:
                declared_total = max(declared_total, int(value or 0))
            except (TypeError, ValueError):
                pass
    incomplete = (
        len(unique) < len(records)
        or declared_total > len(unique)
        or (not declared_total and len(records) >= max_pages * effective_page_size)
    )
    audit = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rawRecordCount": len(records),
        "uniqueRecordCount": len(unique),
        "declaredTotal": declared_total,
        "split": incomplete,
        "pages": pages,
    }
    if not incomplete:
        return list(unique.values()), [audit]
    if start.date() >= end.date():
        raise RuntimeError(
            f"官方分页不完整：{start.date()}，返回{len(records)}条/唯一{len(unique)}条/总计{declared_total}条"
        )
    middle = start.date() + dt.timedelta(days=(end.date() - start.date()).days // 2)
    left_end = dt.datetime.combine(middle, dt.time(23, 59, 59), tzinfo=end.tzinfo)
    right_start = dt.datetime.combine(middle + dt.timedelta(days=1), dt.time.min, tzinfo=start.tzinfo)
    left_records, left_audit = _fetch_complete_window(fetcher, client, endpoint, start, left_end, max_pages)
    right_records, right_audit = _fetch_complete_window(fetcher, client, endpoint, right_start, end, max_pages)
    combined = {lifecycle_source_key(record): record for record in left_records + right_records}
    return list(combined.values()), [audit, *left_audit, *right_audit]


def fetch_szse_lifecycle(
    client: HttpClient,
    endpoint: str,
    start: dt.datetime,
    end: dt.datetime,
    max_pages: int,
) -> tuple[list[Announcement], list[Any]]:
    """Use the official endpoint's effective 50-row pagination contract."""

    page_size = 50
    raw_pages: list[Any] = []
    all_rows: list[dict[str, Any]] = []
    declared_total = 0
    for page in range(1, max_pages + 1):
        payload = client.post_json(
            endpoint + "?random=" + str(time.time()),
            {
                "seDate": [start.date().isoformat(), end.date().isoformat()],
                "channelCode": ["listedNotice_disc"],
                "pageSize": page_size,
                "pageNum": page,
                "stock": [],
                "searchKey": ["可转换公司债券"],
            },
            headers={
                "Referer": "https://www.szse.cn/disclosure/listed/notice/index.html",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        if isinstance(payload, dict):
            payload["_lifecyclePageSize"] = page_size
        raw_pages.append(payload)
        rows = find_list(payload)
        all_rows.extend(rows)
        if isinstance(payload, dict):
            try:
                declared_total = max(declared_total, int(payload.get("announceCount") or 0))
            except (TypeError, ValueError):
                pass
        if not rows or len(rows) < page_size or (declared_total and len(all_rows) >= declared_total):
            break

    records: list[Announcement] = []
    for row in all_rows:
        attachment = clean_text(pick(row, "attachPath", "url", "announcementUrl", "pdfUrl"))
        url = (
            "https://disc.static.szse.cn/download" + attachment
            if attachment.startswith("/disc/")
            else absolute_url("https://www.szse.cn/", attachment)
        )
        records.append(
            Announcement(
                source="szse",
                market="SZSE",
                code=clean_text(pick(row, "secCode", "stockCode", "companyCode")),
                company=clean_text(pick(row, "secName", "stockName", "companyName")),
                title=clean_text(pick(row, "title", "announceTitle", "announcementTitle")),
                date=iso_date(pick(row, "publishTime", "publishDate", "announcementTime")),
                url=url,
                source_id=clean_text(pick(row, "id", "announcementId", "attachPath", "url")),
                raw=row,
            )
        )
    return records, raw_pages


def _discovery_start(now: dt.datetime, state: dict[str, Any]) -> dt.datetime:
    cutoff = state.get("lastSuccessfulDiscoveryCutoff")
    if cutoff:
        try:
            value = dt.datetime.fromisoformat(str(cutoff))
            if value.tzinfo is None:
                value = value.replace(tzinfo=now.tzinfo)
            return value.astimezone(now.tzinfo) - dt.timedelta(days=DISCOVERY_OVERLAP_DAYS)
        except ValueError:
            pass
    return now - dt.timedelta(days=INITIAL_LOOKBACK_DAYS)


def discover_lifecycle_notices(
    client: HttpClient,
    config: dict[str, Any],
    start: dt.datetime,
    now: dt.datetime,
    *,
    fetchers: dict[str, Callable[..., tuple[list[Announcement], Any]]] | None = None,
) -> dict[str, Any]:
    sources = config["officialSources"]
    max_pages = int(config.get("maxPages", 8))
    active_fetchers = fetchers or {"sse": fetch_sse, "szse": fetch_szse_lifecycle}
    endpoints = {
        "sse": sources["sseAnnouncements"],
        "szse": sources["szseAnnouncements"],
    }
    notices: list[Announcement] = []
    statuses: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}
    errors: list[str] = []
    for source in ("sse", "szse"):
        try:
            records, pages = _fetch_complete_window(active_fetchers[source],
                client, endpoints[source], start, now, max_pages
            )
            selected = []
            for record in records:
                kind, reason = classify_lifecycle_title(record.title)
                if kind == "none":
                    continue
                if not is_official_exchange_url(source, record.url):
                    errors.append(f"{source} 命中公告不是交易所官方链接：{record.title}")
                    continue
                record.reason = reason
                selected.append(record)
            notices.extend(selected)
            raw[source] = pages
            statuses.append(
                {"source": source, "ok": True, "recordCount": len(records), "noticeCount": len(selected)}
            )
        except Exception as exc:
            detail = strip_terminal_escapes(exc)
            errors.append(f"{source} 官方公告检索失败：{detail}")
            statuses.append({"source": source, "ok": False, "error": detail})
            raw[source] = {"error": detail}
    dedup: dict[str, Announcement] = {}
    for record in notices:
        kind, _ = classify_lifecycle_title(record.title)
        key = "|".join([kind, record.code, record.date, normalized_title(record.title)])
        dedup[key] = record
    return {
        "ok": not errors and all(item["ok"] for item in statuses),
        "records": list(dedup.values()),
        "statuses": statuses,
        "raw": raw,
        "errors": errors,
    }


def run_lifecycle_reminders(
    config: dict[str, Any],
    now: dt.datetime,
    *,
    queue_path: Path = QUEUE_PATH,
    state_path: Path = STATE_PATH,
    lock_path: Path = LOCK_PATH,
    subscription_queue_path: Path = SUBSCRIPTION_QUEUE_PATH,
    evidence_dir: Path = EVIDENCE_DIR,
    reports_dir: Path = REPORTS_DIR,
    raw_dir: Path = RAW_DIR,
    client: HttpClient | None = None,
    fetchers: dict[str, Callable[..., tuple[list[Announcement], Any]]] | None = None,
    download_func: Callable[..., dict[str, Any]] = download_evidence,
    pdf_text_func: Callable[[Path], str] = pdf_text,
    send_func: Callable[..., dict[str, Any]] = send_email,
    dry_run: bool = False,
    discover: bool = True,
    enrich: bool = True,
) -> dict[str, Any]:
    active_client = client or HttpClient(
        int(config.get("sourceTimeoutSeconds", 35)),
        int(config.get("sourceRetries", 3)),
    )
    state = read_json(state_path, {"schemaVersion": 1})
    start = _discovery_start(now, state)
    discovery = (
        discover_lifecycle_notices(
            active_client, config, start, now, fetchers=fetchers
        )
        if discover
        else {"ok": True, "records": [], "statuses": [], "raw": {}, "errors": []}
    )
    actions: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    errors = list(discovery["errors"])
    with subscription_reminder_lock(lock_path):
        queue = read_json(queue_path, {"schemaVersion": 1, "entries": {}})
        known_sources = {
            str(entry.get("sourceKey"))
            for entry in (queue.get("entries") or {}).values()
            if entry.get("sourceKey")
        }
        new_entries: list[dict[str, Any]] = []
        for record in discovery["records"]:
            source_key = lifecycle_source_key(record)
            if source_key in known_sources:
                continue
            # An expired draw result is no longer actionable. Listing notices
            # are parsed because they may announce a future target date.
            kind, _ = classify_lifecycle_title(record.title)
            if kind == "draw":
                try:
                    if dt.date.fromisoformat(record.date) < now.date():
                        continue
                except ValueError:
                    pass
            entry, missing = build_lifecycle_entry(
                record,
                active_client,
                now,
                evidence_dir=evidence_dir,
                subscription_queue_path=subscription_queue_path,
                download_func=download_func,
                pdf_text_func=pdf_text_func,
                enrich=enrich,
            )
            if missing:
                problem = {
                    "source": record.source,
                    "stockCode": record.code,
                    "title": record.title,
                    "missingFields": missing,
                }
                incomplete.append(problem)
                errors.append(
                    f"{record.code} {kind} 官方公告解析不完整：{', '.join(missing)}"
                )
            if entry:
                new_entries.append(entry)
                known_sources.add(source_key)
        changed = upsert_entries(queue, new_entries, now)
        if not dry_run:
            atomic_write_json(queue_path, queue)
        for entry in sorted(
            (queue.get("entries") or {}).values(),
            key=lambda item: (str(item.get("targetDate")), str(item.get("bondCode"))),
        ):
            if not reminder_due(entry, now):
                continue
            if not entry.get("preparedHtml"):
                if entry.get("kind") == "listing" and enrich:
                    try:
                        market = fetch_market_snapshot(
                            active_client,
                            str(entry["stockCode"]),
                            now,
                            evidence_dir / str(entry["targetDate"]) / f"listing-market-{entry['reminderKey']}",
                        )
                        entry["stockClose"] = market.get("latestClose")
                        entry["stockCloseDate"] = market.get("latestCloseDate")
                        entry["conversionValue"] = (
                            round(100 * float(entry["stockClose"]) / float(entry["initialConversionPrice"]), 2)
                            if entry.get("stockClose") and entry.get("initialConversionPrice")
                            else None
                        )
                    except Exception as exc:
                        entry["marketRefreshError"] = strip_terminal_escapes(exc)
                        if str(entry.get("stockCloseDate") or "") != previous_market_day(now.date()).isoformat():
                            entry["stockClose"] = entry["stockCloseDate"] = entry["conversionValue"] = None
                entry["preparedSubject"] = lifecycle_subject(entry)
                entry["preparedHtml"] = build_lifecycle_html(entry)
                entry["preparedAt"] = now.isoformat()
                if not dry_run:
                    # Persist an immutable body before SMTP. If delivery succeeds
                    # but this process crashes, the sender can deduplicate retry.
                    atomic_write_json(queue_path, queue)
            subject = str(entry["preparedSubject"])
            report = (
                reports_dir
                / str(entry["targetDate"])
                / f"{entry['kind']}-reminder-{entry['reminderKey']}.html"
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(str(entry["preparedHtml"]), encoding="utf-8")
            action = {
                "reminderKey": entry["reminderKey"],
                "kind": entry["kind"],
                "subject": subject,
                "report": str(report.resolve()),
            }
            if dry_run:
                action["status"] = "would_send"
                actions.append(action)
                continue
            try:
                delivery = send_func(config, subject, report)
                if not delivery.get("ok"):
                    raise RuntimeError("SMTP 未确认成功，提醒保持待发送")
                entry["sentAt"] = now.isoformat()
                entry["duplicateAccepted"] = bool(delivery.get("duplicate"))
                atomic_write_json(queue_path, queue)
                action["status"] = "sent"
                action["duplicateAccepted"] = bool(delivery.get("duplicate"))
            except Exception as exc:
                detail = strip_terminal_escapes(exc)
                action["status"] = "failed"
                action["error"] = detail
                errors.append(f"{entry.get('bondCode')} {entry.get('kind')} 提醒发送失败：{detail}")
            actions.append(action)
    if not dry_run:
        stamp = now.strftime("%Y%m%dT%H%M%S")
        raw_path = raw_dir / now.date().isoformat() / f"discovery-{stamp}.json"
        atomic_write_json(
            raw_path,
            {
                "schemaVersion": 1,
                "start": start.isoformat(),
                "end": now.isoformat(),
                "statuses": discovery["statuses"],
                "records": [asdict(item) for item in discovery["records"]],
                "responses": discovery["raw"],
            },
        )
        state.update(
            {
                "schemaVersion": 1,
                "lastRunAt": now.isoformat(),
                "lastRunResult": "success" if not errors else "failed",
            }
        )
        if discover and discovery["ok"] and not incomplete:
            state["lastSuccessfulDiscoveryCutoff"] = now.isoformat()
        atomic_write_json(state_path, state)
    return {
        "ok": not errors,
        "runAt": now.isoformat(),
        "discoveryStart": start.isoformat(),
        "sourceStatuses": discovery["statuses"],
        "discovered": len(discovery["records"]),
        "addedOrUpdated": changed,
        "incomplete": incomplete,
        "actions": actions,
        "errors": errors,
        "dryRun": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="可转债出签与上市提醒")
    parser.add_argument("--dry-run", action="store_true", help="不发邮件、不改队列和状态")
    parser.add_argument("--as-of", help="模拟北京时间，ISO 格式")
    parser.add_argument("--no-discovery", action="store_true", help="只消费本地可信队列")
    args = parser.parse_args()
    config = read_json(CONFIG_PATH, {})
    timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    if args.as_of:
        now = dt.datetime.fromisoformat(args.as_of)
        now = now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    else:
        now = dt.datetime.now(timezone)
    result = run_lifecycle_reminders(
        config,
        now,
        dry_run=args.dry_run,
        discover=not args.no_discovery,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
