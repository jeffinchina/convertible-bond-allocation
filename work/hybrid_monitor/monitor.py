from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HOLIDAYS_PATH = ROOT / "official_holidays.json"
STATE_PATH = ROOT / "state.json"
INVENTORY_PATH = ROOT / "pending_inventory.json"
SUBSCRIPTION_REMINDER_PATH = ROOT / "subscription_reminders.json"
SUBSCRIPTION_REMINDER_LOCK_PATH = ROOT / "subscription_reminders.lock"
RAW_DIR = ROOT / "raw"
EVIDENCE_DIR = ROOT / "evidence"
REPORTS_DIR = ROOT / "reports"
COMPANY_SURVEY_URL = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
)
BUSINESS_ANALYSIS_URL = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
)
COMPANY_INDUSTRY_FALLBACK_URL = "https://push2.eastmoney.com/api/qt/stock/get"
COMPANY_CONCEPT_URL = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
)
CNINFO_QUERY_FALLBACK_ENDPOINTS = (
    "https://list.cninfo.com.cn/cninfo/new/hisAnnouncement/query",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SUBSCRIPTION_REMINDER_SLOTS = ("08:50", "09:50")
SUBSCRIPTION_REMINDER_CUTOFF = dt.time(15, 0)


REGISTRATION_REQUIRED = (
    "可转换公司债券",
)
REGISTRATION_SIGNALS = (
    "同意注册",
    "注册批复",
    "证监许可",
)
FORMAL_SIGNALS = (
    "发行公告",
    "募集说明书",
    "股权登记日",
    "原股东优先配售",
)
HARD_EXCLUSIONS = (
    "向特定对象发行",
    "发行股份购买资产",
    "重组",
)
NON_EVENT_EXCLUSIONS = (
    "预案",
    "论证分析报告",
    "申请文件",
    "受理",
    "问询",
    "回复",
    "审核中心意见",
    "上市委",
    "审核通过",
    "审核状态",
    "注册生效",
    "进展公告",
    "上市公告",
    "上市交易",
    "发行结果",
    "中签率",
    "中签号码",
    "网下发行",
    "网上发行",
    "配售结果",
    "募集资金",
    "受托管理",
    "跟踪评级",
    "评级报告",
    "法律意见书",
    "保荐书",
    "审计报告",
    "付息",
    "赎回",
    "回售",
    "转股价格",
    "转股结果",
    "转股情况",
    "债券持有人",
    "解除限售",
    "修订稿",
    "申报稿",
    "上会稿",
    "审核稿",
)

FORMAL_PROSPECTUS_EXCLUSIONS = (
    "修订稿",
    "申报稿",
    "注册稿",
    "上会稿",
    "审核稿",
    "草案",
    "预案",
    "提示性公告",
    "申请文件",
    "摘要",
)

TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)


@contextmanager
def subscription_reminder_lock(
    path: Path = SUBSCRIPTION_REMINDER_LOCK_PATH,
    timeout_seconds: float = 15.0,
) -> Iterable[None]:
    """Serialize queue mutations across the monitor and reminder task."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    deadline = time.monotonic() + timeout_seconds
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            while not locked:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("申购提醒队列正在被另一进程更新")
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def clean_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if len(value) == 1 else " ".join(str(item) for item in value)
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def strip_terminal_escapes(value: Any) -> str:
    """Remove ANSI/VT control sequences before diagnostics enter HTML or JSON."""
    return TERMINAL_ESCAPE_RE.sub("", str(value or ""))


def normalized_title(value: str) -> str:
    return re.sub(r"[\s\-—_（）()【】\[\]：:，,。.·]", "", clean_text(value))


def iso_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        stamp = value / 1000 if value > 10_000_000_000 else value
        return (
            dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value))
    if not match:
        return str(value)[:10]
    return dt.date(*(int(part) for part in match.groups())).isoformat()


def absolute_url(base: str, value: str) -> str:
    value = html.unescape(value or "").strip()
    if value.startswith("//"):
        return "https:" + value
    return urllib.parse.urljoin(base, value)


@dataclass
class SourceStatus:
    source: str
    ok: bool
    count: int = 0
    detail: str = ""
    url: str = ""


@dataclass
class Announcement:
    source: str
    market: str
    code: str
    company: str
    title: str
    date: str
    url: str
    source_id: str
    stage: str = "none"
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        seed = "|".join(
            [self.source, self.source_id or self.url, normalized_title(self.title)]
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


class HttpClient:
    def __init__(self, timeout: int, retries: int):
        self.timeout = timeout
        self.retries = retries

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> tuple[bytes, dict[str, str], str]:
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        request_headers.update(headers or {})
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = urllib.request.Request(
                    url, data=data, headers=request_headers, method=method
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return (
                        response.read(),
                        {k.lower(): v for k, v in response.headers.items()},
                        response.geturl(),
                    )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.8 * attempt)
        raise RuntimeError(f"请求失败（{self.retries}次）：{url}；{last_error}")

    def get_text(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> tuple[str, str]:
        body, response_headers, final_url = self.request(url, headers=headers)
        if response_headers.get("content-encoding", "").lower() == "gzip" or body.startswith(
            b"\x1f\x8b"
        ):
            body = gzip.decompress(body)
        content_type = response_headers.get("content-type", "")
        match = re.search(r"charset=([\w-]+)", content_type, re.I)
        encodings = [match.group(1)] if match else []
        encodings.extend(["utf-8", "gb18030"])
        for encoding in encodings:
            try:
                return body.decode(encoding), final_url
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace"), final_url

    def get_json(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> Any:
        text, _ = self.get_text(url, headers=headers)
        value = json.loads(text)
        if isinstance(value, str):
            value = json.loads(value)
        return value

    def post_form(
        self, url: str, form: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> tuple[str, str]:
        data = urllib.parse.urlencode(form).encode("utf-8")
        merged = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        merged.update(headers or {})
        body, response_headers, final_url = self.request(
            url, method="POST", headers=merged, data=data
        )
        charset = re.search(
            r"charset=([\w-]+)", response_headers.get("content-type", ""), re.I
        )
        for encoding in ([charset.group(1)] if charset else []) + ["utf-8", "gb18030"]:
            try:
                return body.decode(encoding), final_url
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace"), final_url

    def post_json(
        self, url: str, value: Any, *, headers: dict[str, str] | None = None
    ) -> Any:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json;charset=UTF-8"}
        merged.update(headers or {})
        body, _, _ = self.request(url, method="POST", headers=merged, data=data)
        return json.loads(body.decode("utf-8-sig"))


def classify_title(title: str) -> tuple[str, str]:
    value = normalized_title(title)
    if not all(word in value for word in REGISTRATION_REQUIRED):
        return "none", "不含完整的‘可转换公司债券’主题词"
    hard = next((word for word in HARD_EXCLUSIONS if word in value), None)
    if hard:
        return "none", f"排除：{hard}"

    is_registration = (
        "向不特定对象发行" in value
        and any(signal in value for signal in REGISTRATION_SIGNALS)
        and ("公告" in value or "批复" in value)
    )
    if is_registration:
        return "registration", "命中：向不特定对象发行+可转换公司债券+同意注册/注册批复"

    if "发行公告" in value and (
        "向不特定对象发行" in value or "公开发行" in value
    ):
        return "formal", "命中：可转换公司债券发行公告"

    if "募集说明书" in value:
        draft_marker = next(
            (word for word in FORMAL_PROSPECTUS_EXCLUSIONS if word in value), None
        )
        if draft_marker:
            return "none", f"排除非正式发行募集说明书：{draft_marker}"
        return "formal", "命中：正式募集说明书"

    if (
        ("股权登记日" in value or "原股东优先配售" in value)
        and "发行" in value
    ):
        return "formal", "命中：股权登记日/原股东优先配售正式文件"

    excluded = next((word for word in NON_EVENT_EXCLUSIONS if word in value), None)
    if excluded:
        return "none", f"排除：{excluded}"
    return "none", "相关但不属于同意注册公告或正式发行文件"


def date_in_window(value: str, start: dt.datetime, end: dt.datetime) -> bool:
    if not value:
        return False
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        return False
    return start.date() <= day <= end.date()


def parse_sse_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    page_help = payload.get("pageHelp") or payload.get("PAGEHELP") or {}
    data = page_help.get("data") or page_help.get("DATA") or []
    return data if isinstance(data, list) else []


def fetch_sse(
    client: HttpClient,
    endpoint: str,
    start: dt.datetime,
    end: dt.datetime,
    max_pages: int,
) -> tuple[list[Announcement], Any]:
    all_rows: list[dict[str, Any]] = []
    raw_pages: list[Any] = []
    for page in range(1, max_pages + 1):
        params = {
            "isPagination": "true",
            "productId": "",
            "keyWord": "可转换公司债券",
            "securityType": "0101,120100,020100,020200,120200",
            "reportType2": "DQGG",
            "reportType": "ALL",
            "beginDate": start.date().isoformat(),
            "endDate": end.date().isoformat(),
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": str(page),
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": str(max_pages),
        }
        url = endpoint + "?" + urllib.parse.urlencode(params)
        payload = client.get_json(
            url,
            headers={
                "Referer": "https://www.sse.com.cn/",
                "Accept": "application/json,text/javascript,*/*;q=0.01",
            },
        )
        raw_pages.append(payload)
        rows = parse_sse_records(payload)
        all_rows.extend(rows)
        if len(rows) < 100:
            break

    records: list[Announcement] = []
    for row in all_rows:
        title = clean_text(row.get("TITLE") or row.get("title"))
        stage, reason = classify_title(title)
        url = absolute_url("https://www.sse.com.cn/", row.get("URL") or row.get("url") or "")
        records.append(
            Announcement(
                source="sse",
                market="SSE",
                code=clean_text(row.get("SECURITY_CODE") or row.get("securityCode")),
                company=clean_text(row.get("SECURITY_NAME") or row.get("securityName")),
                title=title,
                date=iso_date(row.get("SSEDATE") or row.get("ADDDATE") or row.get("publishTime")),
                url=url,
                source_id=clean_text(row.get("BULLETIN_TYPE") or row.get("URL") or url),
                stage=stage,
                reason=reason,
                raw=row,
            )
        )
    return records, raw_pages


def find_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "announcements", "announceList", "list"):
            found = value.get(key)
            if isinstance(found, list):
                return [item for item in found if isinstance(item, dict)]
        for child in value.values():
            found = find_list(child)
            if found:
                return found
    return []


def pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return ""


def record_dict(record: Announcement) -> dict[str, Any]:
    value = asdict(record)
    value.pop("raw", None)
    return value


def fetch_szse(
    client: HttpClient,
    endpoint: str,
    start: dt.datetime,
    end: dt.datetime,
    max_pages: int,
) -> tuple[list[Announcement], Any]:
    raw_pages: list[Any] = []
    all_rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        url = endpoint + "?random=" + str(time.time())
        payload = client.post_json(
            url,
            {
                "seDate": [start.date().isoformat(), end.date().isoformat()],
                "channelCode": ["listedNotice_disc"],
                "pageSize": 100,
                "pageNum": page,
                "stock": [],
                "searchKey": ["可转换公司债券"],
            },
            headers={
                "Referer": "https://www.szse.cn/disclosure/listed/notice/index.html",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        raw_pages.append(payload)
        rows = find_list(payload)
        all_rows.extend(rows)
        if len(rows) < 100:
            break

    records: list[Announcement] = []
    for row in all_rows:
        title = clean_text(pick(row, "title", "announceTitle", "announcementTitle"))
        stage, reason = classify_title(title)
        attachment = clean_text(
            pick(row, "attachPath", "url", "announcementUrl", "pdfUrl")
        )
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
                title=title,
                date=iso_date(pick(row, "publishTime", "publishDate", "announcementTime")),
                url=url,
                source_id=clean_text(pick(row, "id", "announcementId", "attachPath", "url")),
                stage=stage,
                reason=reason,
                raw=row,
            )
        )
    return records, raw_pages


def fetch_cninfo_market(
    client: HttpClient,
    endpoint: str,
    start: dt.datetime,
    end: dt.datetime,
    column: str,
    plate: str,
    max_pages: int,
    search_key: str = "可转换公司债券",
) -> tuple[list[Announcement], list[Any]]:
    raw_pages: list[Any] = []
    all_rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = post_cninfo_form(
            client,
            endpoint,
            {
                "pageNum": page,
                "pageSize": 100,
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": "",
                "searchkey": search_key,
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start.date().isoformat()}~{end.date().isoformat()}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
        )
        raw_pages.append(payload)
        rows = payload.get("announcements") or []
        all_rows.extend(rows)
        total_pages = int(payload.get("totalpages") or payload.get("totalPages") or 0)
        if (total_pages and page >= total_pages) or (not total_pages and len(rows) < 30):
            break

    market = "SSE" if plate == "sh" else "SZSE"
    records: list[Announcement] = []
    for row in all_rows:
        title = clean_text(row.get("announcementTitle"))
        stage, reason = classify_title(title)
        adjunct = str(row.get("adjunctUrl") or "")
        url = absolute_url("https://static.cninfo.com.cn/", adjunct)
        records.append(
            Announcement(
                source="cninfo",
                market=market,
                code=clean_text(row.get("secCode")),
                company=clean_text(row.get("secName")),
                title=title,
                date=iso_date(row.get("announcementTime")),
                url=url,
                source_id=clean_text(row.get("announcementId") or adjunct),
                stage=stage,
                reason=reason,
                raw=row,
            )
        )
    return records, raw_pages


def post_cninfo_form(
    client: HttpClient,
    endpoint: str,
    form: dict[str, Any],
) -> dict[str, Any]:
    """POST a CNInfo query with an auditable official-domain failover.

    The primary endpoint is probed at the start of every process. If it is blocked
    or returns invalid JSON, the working official endpoint is preferred for the
    remaining calls made by the same client, avoiding repeated retry delays while
    still allowing the primary endpoint to recover on the next scheduled run.
    """

    preferred = str(getattr(client, "_cninfo_query_endpoint", "") or "")
    candidates: list[str] = []
    for candidate in (preferred, endpoint, *CNINFO_QUERY_FALLBACK_ENDPOINTS):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    failures: list[dict[str, str]] = []
    for candidate in candidates:
        referer = (
            "https://list.cninfo.com.cn/cninfo/new/index"
            if urllib.parse.urlparse(candidate).hostname == "list.cninfo.com.cn"
            else "https://www.cninfo.com.cn/"
        )
        try:
            text, final_url = client.post_form(
                candidate,
                form,
                headers={
                    "Referer": referer,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("响应不是 JSON 对象")
        except Exception as exc:
            failures.append({"endpoint": candidate, "error": str(exc)})
            continue

        setattr(client, "_cninfo_query_endpoint", candidate)
        audited_payload = dict(payload)
        audited_payload["_fetchMeta"] = {
            "primaryEndpoint": endpoint,
            "usedEndpoint": candidate,
            "finalUrl": final_url,
            "fallbackUsed": candidate != endpoint,
            "failures": failures,
        }
        return audited_payload

    details = "；".join(
        f"{item['endpoint']}：{item['error']}" for item in failures
    )
    raise RuntimeError(f"巨潮公告主站与官方备用域名均失败：{details}")


def cninfo_transport_detail(raw: Any) -> str:
    used_fallbacks: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            meta = value.get("_fetchMeta")
            if isinstance(meta, dict) and meta.get("fallbackUsed"):
                endpoint = str(meta.get("usedEndpoint") or "")
                if endpoint:
                    used_fallbacks.add(endpoint)
            for key, child in value.items():
                if key != "_fetchMeta":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw)
    if not used_fallbacks:
        return ""
    return "；巨潮主站访问受限，已切换官方备用域名"


def fetch_cninfo(
    client: HttpClient,
    endpoint: str,
    start: dt.datetime,
    end: dt.datetime,
    max_pages: int,
) -> tuple[list[Announcement], Any]:
    records: list[Announcement] = []
    raw: dict[str, Any] = {}
    for column, plate in (("sse", "sh"), ("szse", "sz")):
        market_records, pages = fetch_cninfo_market(
            client, endpoint, start, end, column, plate, max_pages
        )
        records.extend(market_records)
        raw[plate] = pages
    return records, raw


def parse_csrc_results(page: str) -> list[Announcement]:
    results: list[Announcement] = []
    anchor_pattern = re.compile(r"<a\b(?P<attrs>[^>]*)>", re.I)
    for match in anchor_pattern.finditer(page):
        attrs = {
            name.lower(): html.unescape(value)
            for name, _, value in re.findall(
                r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", match.group("attrs"), re.S
            )
        }
        href = attrs.get("href", "")
        title = clean_text(attrs.get("title", ""))
        if not title:
            end_anchor = page.find("</a>", match.end())
            if end_anchor != -1:
                title = clean_text(page[match.end() : end_anchor])
        if "可转换公司债券" not in title:
            continue
        if not title:
            continue
        tail = page[match.end() : match.end() + 2500]
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", clean_text(tail))
        stage, reason = classify_title(title)
        url = absolute_url("https://www.csrc.gov.cn/", href)
        if not re.search(r"csrc/c\d+/c\d+/content\.shtml", url):
            continue
        results.append(
            Announcement(
                source="csrc",
                market="CSRC",
                code="",
                company=company_from_title(title),
                title=title,
                date=date_match.group(0) if date_match else "",
                url=url,
                source_id=url,
                stage=stage,
                reason=reason,
            )
        )
    dedup: dict[str, Announcement] = {}
    for result in results:
        dedup[result.url + normalized_title(result.title)] = result
    return list(dedup.values())


def fetch_csrc_search(
    client: HttpClient, endpoint: str, query: str
) -> tuple[list[Announcement], str]:
    page, _ = client.post_form(
        endpoint,
        {
            "searchWord": query,
            "uc": "1",
            "siteCode": "bm56000001",
            "column": "全部",
        },
        headers={"Referer": "https://www.csrc.gov.cn/"},
    )
    return parse_csrc_results(page), page


def company_from_title(title: str) -> str:
    value = clean_text(title)
    patterns = (
        r"关于(?:收到)?(?:同意)?(.+?股份有限公司).*?可转换公司债券",
        r"^(.+?股份有限公司).*?可转换公司债券",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return ""


def company_key(record: Announcement) -> str:
    if record.code:
        return record.code
    company = record.company or company_from_title(record.title)
    return normalized_title(company).replace("股份有限公司", "").replace("有限公司", "")


def titles_similar(left: str, right: str) -> bool:
    a, b = normalized_title(left), normalized_title(right)
    if not a or not b:
        return False
    return a in b or b in a or ("发行公告" in a and "发行公告" in b) or (
        "募集说明书" in a and "募集说明书" in b
    )


def company_matches(record: Announcement, company: str) -> bool:
    left = normalized_title(company).replace("股份有限公司", "").replace("有限公司", "")
    right = normalized_title(record.company or record.title).replace("股份有限公司", "").replace("有限公司", "")
    return bool(left and right and (left in right or right in left))


def group_candidates(records: Iterable[Announcement]) -> list[dict[str, Any]]:
    records = list(records)
    csrc_records = [record for record in records if record.source == "csrc" and record.stage != "none"]
    groups: dict[tuple[str, str], list[Announcement]] = {}
    for record in records:
        if record.stage == "none":
            continue
        if record.source == "csrc":
            continue
        key = company_key(record) or normalized_title(record.title)[:24]
        groups.setdefault((key, record.stage), []).append(record)
    output: list[dict[str, Any]] = []
    matched_csrc: set[str] = set()
    for (key, stage), group in groups.items():
        primary = next((r for r in group if r.source in ("sse", "szse")), None)
        cninfo = next((r for r in group if r.source == "cninfo"), None)
        best = primary or cninfo or group[0]
        company = best.company or company_from_title(best.title)
        csrc = next(
            (
                record
                for record in csrc_records
                if company_matches(record, company)
                and (record.stage == stage or (stage == "formal" and record.stage == "registration"))
            ),
            None,
        )
        if csrc:
            matched_csrc.add(csrc.key)
        output.append(
            {
                "groupKey": key,
                "stage": stage,
                "code": best.code,
                "company": best.company or company_from_title(best.title),
                "date": best.date,
                "primary": record_dict(primary) if primary else None,
                "cninfo": record_dict(cninfo) if cninfo else None,
                "csrc": record_dict(csrc) if csrc else None,
                "records": [record_dict(item) for item in group] + ([record_dict(csrc)] if csrc else []),
                "crossValidation": {
                    "layer1": "matched" if primary else "missing",
                    "layer2": "matched" if cninfo else "missing",
                    "layer3": "matched" if csrc else "not_independently_found",
                    "titleConsistent": bool(
                        primary and cninfo and titles_similar(primary.title, cninfo.title)
                    ),
                    "conflictRule": "事实冲突时以上交所/深交所原始公告为准",
                },
            }
        )
    for csrc in csrc_records:
        if csrc.key in matched_csrc:
            continue
        output.append(
            {
                "groupKey": company_key(csrc) or normalized_title(csrc.title)[:24],
                "stage": csrc.stage,
                "code": "",
                "company": csrc.company or company_from_title(csrc.title),
                "date": csrc.date,
                "primary": None,
                "cninfo": None,
                "csrc": record_dict(csrc),
                "records": [record_dict(csrc)],
                "crossValidation": {
                    "layer1": "missing",
                    "layer2": "missing",
                    "layer3": "matched",
                    "titleConsistent": False,
                    "conflictRule": "事实冲突时以上交所/深交所原始公告为准",
                },
            }
        )
    return sorted(output, key=lambda item: (item["date"], item["code"], item["stage"]))


def cached_closed_dates(day: dt.date) -> tuple[set[dt.date], dict[str, Any]]:
    config = read_json(HOLIDAYS_PATH, {})
    year = config.get(str(day.year))
    if not year:
        raise RuntimeError(f"official_holidays.json 尚无 {day.year} 年官方休市安排")
    closed: set[dt.date] = set()
    for start_text, end_text in year.get("closedRanges", []):
        current = dt.date.fromisoformat(start_text)
        end = dt.date.fromisoformat(end_text)
        while current <= end:
            closed.add(current)
            current += dt.timedelta(days=1)
    return closed, year


def check_trading_day(
    client: HttpClient, sources: dict[str, str], day: dt.date
) -> tuple[bool, list[SourceStatus], dict[str, Any]]:
    closed, cache = cached_closed_dates(day)
    statuses: list[SourceStatus] = []
    raw: dict[str, Any] = {}
    for name, key in (("sse_calendar", "sseCalendar"), ("szse_calendar", "szseCalendar")):
        url = sources[key]
        try:
            page, final_url = client.get_text(url, headers={"Referer": url})
            plain = clean_text(page)
            marker_ok = str(day.year) in plain and "休市" in plain
            statuses.append(
                SourceStatus(
                    source=name,
                    ok=marker_ok,
                    count=1 if marker_ok else 0,
                    detail=(
                        f"已在线核验{day.year}年官方休市安排"
                        if marker_ok
                        else "页面可访问，但未识别到当年休市安排"
                    ),
                    url=final_url,
                )
            )
            raw[name] = {"url": final_url, "sha256": hashlib.sha256(page.encode()).hexdigest()}
        except Exception as exc:
            statuses.append(
                SourceStatus(source=name, ok=False, detail=str(exc), url=url)
            )
            raw[name] = {"url": url, "error": str(exc)}
    # 两家官网临时不可用时，不把工作日误判为交易日；失败由主流程报警。
    is_weekday = day.weekday() < 5
    is_open = is_weekday and day not in closed
    raw["cachedOfficialSchedule"] = cache
    return is_open, statuses, raw


def safe_file_name(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", value).strip("._")
    return value[:100] or "document"


def sse_waf_cookie(challenge: bytes) -> str | None:
    text = challenge.decode("utf-8", errors="ignore")
    match = re.search(r"var\s+arg1=['\"]([0-9A-Fa-f]{40})['\"]", text)
    if not match or "acw_sc__v2" not in text:
        return None
    arg1 = match.group(1)
    positions = [
        0xF, 0x23, 0x1D, 0x18, 0x21, 0x10, 0x1, 0x26, 0xA, 0x9,
        0x13, 0x1F, 0x28, 0x1B, 0x16, 0x17, 0x19, 0xD, 0x6, 0xB,
        0x27, 0x12, 0x14, 0x8, 0xE, 0x15, 0x20, 0x1A, 0x2, 0x1E,
        0x7, 0x4, 0x11, 0x5, 0x3, 0x1C, 0x22, 0x25, 0xC, 0x24,
    ]
    mask = "3000176000856006061501533003690027800375"
    reordered = [""] * len(positions)
    for source_index, char in enumerate(arg1, start=1):
        reordered[positions.index(source_index)] = char
    arg2 = "".join(reordered)
    return "".join(
        f"{int(arg2[index:index + 2], 16) ^ int(mask[index:index + 2], 16):02x}"
        for index in range(0, min(len(arg2), len(mask)), 2)
    )


def download_cninfo_with_browser_compatibility(
    url: str,
    referer: str,
    timeout: int,
    retries: int,
) -> tuple[bytes, dict[str, str], str]:
    try:
        from curl_cffi import requests as browser_requests
    except ImportError as exc:
        raise RuntimeError("缺少 curl_cffi，无法启用巨潮附件 HTTPS 兼容通道") from exc

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = browser_requests.get(
                url,
                headers={"Referer": referer, "Accept": "application/pdf,*/*;q=0.8"},
                impersonate="chrome",
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            # curl_cffi exposes the decoded body, so do not decompress it a second time.
            headers.pop("content-encoding", None)
            return bytes(response.content), headers, str(response.url)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)
    raise RuntimeError(f"浏览器兼容 HTTPS 请求失败（{retries}次）：{url}；{last_error}")


def download_evidence(
    client: HttpClient, url: str, target: Path, referer: str
) -> dict[str, Any]:
    transport = "urllib"
    primary_error = ""
    try:
        body, headers, final_url = client.request(url, headers={"Referer": referer})
    except Exception as exc:
        primary_error = str(exc)
        if urllib.parse.urlparse(url).hostname != "static.cninfo.com.cn":
            raise
        body, headers, final_url = download_cninfo_with_browser_compatibility(
            url, referer, client.timeout, client.retries
        )
        transport = "curl_cffi_chrome_https"
    if headers.get("content-encoding", "").lower() == "gzip" or body.startswith(b"\x1f\x8b"):
        body = gzip.decompress(body)
    cookie = sse_waf_cookie(body)
    if cookie:
        body, headers, final_url = client.request(
            final_url,
            headers={
                "Referer": referer,
                "Cookie": f"acw_sc__v2={cookie}",
            },
        )
        if headers.get("content-encoding", "").lower() == "gzip" or body.startswith(b"\x1f\x8b"):
            body = gzip.decompress(body)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    is_pdf = body.startswith(b"%PDF") or "pdf" in headers.get("content-type", "").lower()
    return {
        "path": str(target.resolve()),
        "url": final_url,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "contentType": headers.get("content-type", ""),
        "pdfValidated": is_pdf,
        "transport": transport,
        "transportFallback": transport != "urllib",
        "primaryTransportError": primary_error or None,
    }


def pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def money_to_yuan(number: str, unit: str) -> float:
    value = float(number.replace(",", ""))
    multiplier = {"亿元": 100_000_000, "万元": 10_000, "元": 1}[unit]
    return value * multiplier


def parse_registration_amount(text: str) -> dict[str, Any] | None:
    text = re.sub(r"\s+", "", text)
    # A prospectus often repeats the current issue amount alongside historical
    # adjustments.  Returning the first broad regex match can therefore mistake
    # text such as "调减本次……募集资金6,800万元" for the current issue amount.
    # Collect all candidates and prefer explicit total/issue-size wording that is
    # not introduced by an adjustment or other historical qualifier.
    patterns = (
        (
            130,
            r"本次发行总额(?:为|：|:)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)(?:（含本数）|\(含本数\))?(?:可转债|可转换公司债券)?",
        ),
        (
            128,
            r"本次发行(?:的)?(?:可转债|可转换公司债券)(?:发行)?总额(?:为)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            126,
            r"本次发行的募集资金总额(?:为|不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            126,
            r"本次(?:拟)?(?:向不特定对象)?发行(?:的)?可转换公司债券(?:的)?募集资金总额(?:为)?(?:不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            124,
            r"本次发行(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)(?:的)?(?:可转债|可转换公司债券)",
        ),
        (
            122,
            r"预计募集资金量(?:（含发行费用）)?(?:为)?(?:不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            120,
            r"本次(?:可转债)?(?:发行)?预计募集资金总额(?:为)?(?:不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            118,
            r"(?:公司)?本次(?:发行)?(?:可转债)?(?:拟|预计)?募集资金总额(?:为)?(?:预计)?(?:不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            116,
            r"本次募集资金总额(?:为|不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            90,
            r"(?:公司)?本次(?:拟)?发行可转换(?:公司)?债券的金额(?:为)?(?:不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
        (
            80,
            r"(?:公司)?本次(?:拟)?向不特定对象发行可转换公司债券(?:的)?募集资金(?:总额)?(?:为)?(?:不超过)?(?:人民币)?\s*([\d,.]+)\s*(亿元|万元|元)",
        ),
    )
    historical_markers = (
        "调减",
        "调增",
        "扣减",
        "减少",
        "原拟",
        "原计划",
        "调整前",
        "变更前",
        "不再使用",
        "取消",
        "撤回",
    )
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int, float]] = set()
    for priority, pattern in patterns:
        for match in re.finditer(pattern, text):
            value = money_to_yuan(match.group(1), match.group(2))
            if value < 10_000_000:
                continue
            key = (match.start(), match.end(), value)
            if key in seen:
                continue
            seen.add(key)
            context = text[max(0, match.start() - 24) : match.start()]
            historical = any(marker in context for marker in historical_markers)
            candidates.append(
                {
                    "value": value,
                    "rawText": clean_text(match.group(0)),
                    "priority": priority,
                    "historical": historical,
                    "position": match.start(),
                }
            )
    if not candidates:
        return None

    current_candidates = [item for item in candidates if not item["historical"]]
    pool = current_candidates or candidates
    frequency: dict[float, int] = {}
    for item in pool:
        frequency[item["value"]] = frequency.get(item["value"], 0) + 1
    selected = max(
        pool,
        key=lambda item: (
            item["priority"],
            frequency[item["value"]],
            -item["position"],
        ),
    )
    return {
        "valueYuan": selected["value"],
        "rawText": selected["rawText"],
    }


def parse_latest_share_snapshot(text: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"截至\s*(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
        r"[，,]?\s*公司总股本(?:为|是|：|:)?\s*([\d,.]+)\s*(万股|股)"
    )
    snapshots: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        date_value = iso_date(re.sub(r"\s+", "", match.group(1)))
        shares = float(match.group(2).replace(",", ""))
        if match.group(3) == "万股":
            shares *= 10_000
        if shares >= 1_000_000:
            snapshots.append(
                {
                    "value": int(round(shares)),
                    "asOfDate": date_value,
                    "rawText": clean_text(match.group(0)),
                }
            )
    return max(snapshots, key=lambda item: item["asOfDate"]) if snapshots else None


def parse_formal_share_snapshot(text: str) -> dict[str, Any] | None:
    return select_formal_share_snapshot(parse_formal_share_candidates(text))


def parse_formal_share_candidates(text: str) -> list[dict[str, Any]]:
    """Collect labelled share bases without silently choosing the first number."""
    compact = re.sub(r"\s+", "", text)
    patterns = (
        (
            r"可参与(?:本次发行)?(?:原股东)?优先配售的(?:A股)?股本(?:(?:总数|数量|总额))?(?:为|是|：|:)?([\d,.]+)股",
            "正式发行文件披露的可参与优先配售股本",
        ),
        (
            r"可参与原股东优先配售的(?:A股)?股本(?:(?:总数|数量|总额))?(?:为|是|：|:)?([\d,.]+)股",
            "正式发行文件披露的可参与优先配售股本",
        ),
        (
            r"(?:发行人|公司)?现有(?:A股)?(?:股本|总股本)(?:为|是|：|:)?([\d,.]+)股",
            "正式发行文件披露的现有总股本",
        ),
    )
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str, int]] = set()
    for pattern, basis in patterns:
        for match in re.finditer(pattern, compact):
            shares = float(match.group(1).replace(",", ""))
            if shares < 1_000_000:
                continue
            value = int(round(shares))
            key = (value, basis, match.start())
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "value": value,
                    "asOfDate": "",
                    "rawText": clean_text(match.group(0)),
                    "basis": basis,
                    "position": match.start(),
                }
            )
    dated = parse_latest_share_snapshot(text)
    if dated:
        dated = {
            **dated,
            "basis": "正式发行文件披露的截至日公司总股本",
            "position": -1,
        }
        candidates.append(dated)
    return candidates


def select_formal_share_snapshot(
    candidates: list[dict[str, Any]],
    amount_yuan: float | None = None,
    allocation_per_share: float | None = None,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    selected: dict[str, Any]
    selection: dict[str, Any]
    if amount_yuan and allocation_per_share:
        ranked = sorted(
            candidates,
            key=lambda item: abs(
                (float(item["value"]) * float(allocation_per_share) - float(amount_yuan))
                / float(amount_yuan)
            ),
        )
        selected = dict(ranked[0])
        allocated = float(selected["value"]) * float(allocation_per_share)
        gap_pct = abs(allocated - float(amount_yuan)) / float(amount_yuan) * 100
        # A four-decimal allocation ratio introduces a tiny rounding gap. A larger
        # discrepancy usually means total shares were mistaken for the eligible base.
        if gap_pct > 0.5:
            return None
        selection = {
            "method": "发行额×每股配售额一致性择优",
            "candidateCount": len(candidates),
            "allocatedAmountYuan": round(allocated, 2),
            "finalAmountYuan": round(float(amount_yuan), 2),
            "gapPct": round(gap_pct, 6),
        }
    else:
        eligible = [
            item for item in candidates if "可参与优先配售股本" in item.get("basis", "")
        ]
        selected = dict(max(eligible or candidates, key=lambda item: item.get("position", -1)))
        selection = {
            "method": "正式文件标签优先",
            "candidateCount": len(candidates),
        }
    selected.pop("position", None)
    selected["selection"] = selection
    return selected


def parse_formal_terms(text: str) -> dict[str, Any]:
    """Extract deterministic issuance inputs from a formal prospectus/notice."""
    compact = re.sub(r"\s+", "", text)
    amount = parse_registration_amount(text)

    def decimal(patterns: tuple[str, ...]) -> float | None:
        for pattern in patterns:
            match = re.search(pattern, compact)
            if match:
                return float(match.group(1).replace(",", ""))
        return None

    def chinese_date(patterns: tuple[str, ...]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, compact)
            if match:
                return iso_date(match.group(1))
        return None

    def text_value(patterns: tuple[str, ...]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, compact, re.I)
            if match:
                return clean_text(match.group(1)).strip("“”‘’\"'：:，,。；;")
        return None

    def integer_value(patterns: tuple[str, ...]) -> int | None:
        value = text_value(patterns)
        if not value:
            return None
        if value.isdigit():
            return int(value)
        digits = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        return digits.get(value)

    def preceding_date(label_pattern: str, max_distance: int = 160) -> str | None:
        for label in re.finditer(label_pattern, compact):
            prefix = compact[max(0, label.start() - max_distance) : label.start()]
            matches = list(re.finditer(r"20\d{2}年\d{1,2}月\d{1,2}日", prefix))
            if matches:
                return iso_date(matches[-1].group(0))
        return None

    allocation_per_share = decimal(
        (
            r"每股配售([\d,.]+)元(?:可转债)?",
            r"每股优先配售(?:金额|面值)?(?:为)?([\d,.]+)元",
        )
    )
    shares = select_formal_share_snapshot(
        parse_formal_share_candidates(text),
        (amount or {}).get("valueYuan"),
        allocation_per_share,
    )

    record_date = chinese_date(
        (
            # PDF tables are sometimes extracted by column rather than by row,
            # producing "date + T-1 + activities + record-date label + next
            # row's date".  Prefer the explicit T-1 date in that layout before
            # considering the conventional label-followed-by-date wording.
            r"(20\d{2}年\d{1,2}月\d{1,2}日)(?:周[一二三四五六日天])?[（(]?T-1日[）)]?[^0-9]{0,40}股权登记日",
            r"股权登记日[：:]?[（(]?(20\d{2}年\d{1,2}月\d{1,2}日)",
        )
    )
    subscription_date = chinese_date(
        (
            r"(?:原股东优先配售认购日|原股东优先配售日|优先配售日|网上申购日|申购日)(?:[、，,和与]?(?:网上申购日|申购日))?[（(]?T日[）)]?(?:同为|为|即|指|：|:)?[（(]?(20\d{2}年\d{1,2}月\d{1,2}日)",
            r"(?:原股东优先配售认购日|原股东优先配售日|优先配售日|网上申购日|申购日)(?:[、，,和与]?(?:网上申购日|申购日))?(?:同为|为|即|指|：|:)?[（(]?(20\d{2}年\d{1,2}月\d{1,2}日)",
        )
    ) or preceding_date(r"(?:原股东优先配售认购日|网上申购日)", max_distance=48)

    consistency: dict[str, Any] = {"status": "not_evaluated"}
    if amount and shares and allocation_per_share:
        expected = float(amount["valueYuan"])
        allocated = float(shares["value"]) * allocation_per_share
        gap_pct = abs(allocated - expected) / expected * 100
        consistency = {
            "status": "pass" if gap_pct <= 0.5 else "conflict",
            "allocatedAmountYuan": round(allocated, 2),
            "finalAmountYuan": round(expected, 2),
            "gapPct": round(gap_pct, 6),
            "tolerancePct": 0.5,
        }

    return {
        "finalAmount": amount,
        "shareSnapshot": shares,
        "allocationPerShare": allocation_per_share,
        "initialConversionPrice": decimal(
            (
                r"初始转股价格(?:确定为|为|：|:)([\d,.]+)元/股",
                r"初始转股价(?:为|：|:)([\d,.]+)元/股",
            )
        ),
        "recordDate": record_date,
        "subscriptionDate": subscription_date,
        "bondShortName": text_value(
            (
                r"(?:本次发行的)?(?:可转换公司债券|可转债)(?:的)?简称(?:为|：|:)?[“”‘’\"']?([\u4e00-\u9fffA-Za-z0-9]{2,12})",
                r"债券简称(?:为|：|:)?[“”‘’\"']?([\u4e00-\u9fffA-Za-z0-9]{2,12})",
            )
        ),
        "bondCode": text_value(
            (
                r"(?:债券代码|可转债代码)(?:为|：|:)?[“”‘’\"']?(\d{6})",
            )
        ),
        "subscriptionShortName": text_value(
            (r"申购简称(?:为|：|:)?[“”‘’\"']?([\u4e00-\u9fffA-Za-z0-9]{2,12})",)
        ),
        "subscriptionCode": text_value(
            (r"申购代码(?:为|：|:)?[“”‘’\"']?(\d{6})",)
        ),
        "allocationShortName": text_value(
            (r"配售简称(?:为|：|:)?[“”‘’\"']?([\u4e00-\u9fffA-Za-z0-9]{2,12})",)
        ),
        "allocationCode": text_value(
            (r"配售代码(?:为|：|:)?[“”‘’\"']?(\d{6})",)
        ),
        "creditRating": text_value(
            (
                r"(?:本次发行的)?(?:可转换公司债券|可转债)(?:信用)?(?:级别|评级)(?:为|：|:)?(AAA|AA\+|AA-|AA|A\+|A-|A)",
            )
        ),
        "termYears": integer_value(
            (
                r"(?:本次发行的)?(?:可转换公司债券|可转债)(?:的)?期限为自发行之日起([一二三四五六七八九十\d]+)年",
                r"债券期限(?:为|：|:)?([一二三四五六七八九十\d]+)年",
            )
        ),
        "consistency": consistency,
    }


FORMAL_TERM_FIELDS = (
    "finalAmount",
    "shareSnapshot",
    "allocationPerShare",
    "initialConversionPrice",
    "recordDate",
    "subscriptionDate",
    "bondShortName",
    "bondCode",
    "subscriptionShortName",
    "subscriptionCode",
    "allocationShortName",
    "allocationCode",
    "creditRating",
    "termYears",
)


def merge_formal_term_sets(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge independently parsed official documents with field-level provenance."""
    merged: dict[str, Any] = {}
    field_sources: dict[str, list[dict[str, Any]]] = {}
    conflicts: list[dict[str, Any]] = []

    def normalized(field: str, value: Any) -> Any:
        if field == "finalAmount":
            return round(float(value["valueYuan"]), 2)
        if field == "shareSnapshot":
            return int(value["value"])
        if field in ("allocationPerShare", "initialConversionPrice"):
            return round(float(value), 8)
        if field == "termYears":
            return int(value)
        return str(value)

    for field in FORMAL_TERM_FIELDS:
        values: list[dict[str, Any]] = []
        for document in documents:
            terms = document.get("terms") or {}
            value = terms.get(field)
            if value is None or value == "":
                continue
            source = dict(document.get("source") or {})
            if isinstance(value, dict) and value.get("rawText"):
                source["rawText"] = value["rawText"]
            values.append(
                {
                    "value": value,
                    "normalized": normalized(field, value),
                    "source": source,
                }
            )
        if not values:
            continue
        merged[field] = values[0]["value"]
        chosen = values[0]["normalized"]
        field_sources[field] = [
            item["source"] for item in values if item["normalized"] == chosen
        ]
        alternatives = []
        seen_values = {chosen}
        for item in values[1:]:
            if item["normalized"] in seen_values:
                continue
            seen_values.add(item["normalized"])
            alternatives.append(
                {"value": item["normalized"], "source": item["source"]}
            )
        if alternatives:
            conflicts.append(
                {
                    "field": field,
                    "chosen": chosen,
                    "chosenSource": values[0]["source"],
                    "alternatives": alternatives,
                }
            )

    amount = (merged.get("finalAmount") or {}).get("valueYuan")
    shares = (merged.get("shareSnapshot") or {}).get("value")
    allocation = merged.get("allocationPerShare")
    consistency: dict[str, Any] = {"status": "not_evaluated"}
    if amount and shares and allocation:
        allocated = float(shares) * float(allocation)
        gap_pct = abs(allocated - float(amount)) / float(amount) * 100
        consistency = {
            "status": "pass" if gap_pct <= 0.5 else "conflict",
            "allocatedAmountYuan": round(allocated, 2),
            "finalAmountYuan": round(float(amount), 2),
            "gapPct": round(gap_pct, 6),
            "tolerancePct": 0.5,
        }
    merged.update(
        {
            "consistency": consistency,
            "fieldSources": field_sources,
            "conflicts": conflicts,
            "documentCount": len(documents),
            "documents": [dict(item.get("source") or {}) for item in documents],
        }
    )
    return merged


def supplemental_formal_records(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Select complementary official formal documents, not API-order duplicates."""
    primary_url = str((candidate.get("primary") or {}).get("url") or "")
    cninfo_url = str((candidate.get("cninfo") or {}).get("url") or "")

    def kind(record: dict[str, Any]) -> str:
        title = normalized_title(record.get("title") or "")
        if "募集说明书" in title:
            return "prospectus"
        if "优先配售" in title:
            return "priority_allocation"
        if "发行公告" in title:
            return "issuance_announcement"
        return title

    selected: list[dict[str, Any]] = []
    seen_kinds = {
        kind(record)
        for record in (candidate.get("primary"), candidate.get("cninfo"))
        if record
    }
    records = sorted(
        (
            record
            for record in candidate.get("records") or []
            if record.get("stage") == "formal"
            and record.get("url")
            and record.get("url") not in (primary_url, cninfo_url)
        ),
        key=lambda record: (
            0 if record.get("source") in ("sse", "szse") else 1,
            0 if "募集说明书" in record.get("title", "") else 1,
            record.get("title", ""),
        ),
    )
    for record in records:
        record_kind = kind(record)
        if record_kind in seen_kinds:
            continue
        seen_kinds.add(record_kind)
        selected.append(record)
        if len(selected) >= 3:
            break
    return selected


def cninfo_company_documents(
    client: HttpClient,
    endpoint: str,
    code: str,
    company: str,
    end: dt.datetime,
    lookback_days: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = end.date() - dt.timedelta(days=lookback_days)
    plate = "sh" if code.startswith(("6", "68", "9")) else "sz"
    column = "sse" if plate == "sh" else "szse"
    payload = post_cninfo_form(
        client,
        endpoint,
        {
            "pageNum": 1,
            "pageSize": 50,
            "column": column,
            "tabName": "fulltext",
            "plate": plate,
            "stock": "",
            "searchkey": f"{company} 向不特定对象发行可转换公司债券",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{start.isoformat()}~{end.date().isoformat()}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
    )
    documents: list[dict[str, Any]] = []
    for row in payload.get("announcements") or []:
        if clean_text(row.get("secCode")) != code:
            continue
        adjunct = str(row.get("adjunctUrl") or "")
        documents.append(
            {
                "code": code,
                "company": clean_text(row.get("secName")),
                "title": clean_text(row.get("announcementTitle")),
                "date": iso_date(row.get("announcementTime")),
                "url": absolute_url("https://static.cninfo.com.cn/", adjunct),
                "sourceId": clean_text(row.get("announcementId") or adjunct),
            }
        )
    return documents, payload


def choose_registration_document(
    documents: list[dict[str, Any]], event_date: str
) -> dict[str, Any] | None:
    def score(item: dict[str, Any]) -> tuple[int, str]:
        title = normalized_title(item.get("title", ""))
        points = 0
        if "募集说明书注册稿" in title:
            points = 50
        elif "募集说明书修订稿" in title:
            points = 40
        elif "募集说明书申报稿" in title:
            points = 30
        elif "可转换公司债券预案" in title:
            points = 20
        if any(word in title for word in ("提示性公告", "申请文件", "更新说明")):
            points -= 100
        if item.get("date", "") > event_date:
            points -= 10
        return points, item.get("date", "")

    ranked = sorted(documents, key=score, reverse=True)
    return ranked[0] if ranked and score(ranked[0])[0] > 0 else None


def eastmoney_secid(code: str) -> str:
    market = "1" if code.startswith(("6", "68", "9")) else "0"
    return f"{market}.{code}"


def compact_company_label(value: Any, max_chars: int = 10) -> str:
    text = clean_text(value)
    text = re.sub(r"^(?:其中|主营)[:：]", "", text)
    text = re.sub(r"[（(]其他[）)]$", "", text)
    text = text.strip(" -—_/，,；;。")
    return text[:max_chars]


def parse_company_profile(
    survey_payload: dict[str, Any],
    business_payload: dict[str, Any],
    quote_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    survey = ((survey_payload.get("jbzl") or [{}])[0]) or {}
    quote_data = (quote_payload or {}).get("data") or {}
    industry_raw = clean_text(
        survey.get("EM2016")
        or survey.get("INDUSTRYCSRC1")
        or quote_data.get("f127")
    )
    industry_parts = [
        item for item in re.split(r"[-—/]", industry_raw) if clean_text(item)
    ]
    industry = compact_company_label(industry_parts[-1] if industry_parts else "")

    candidates = []
    for row in business_payload.get("zygcfx") or []:
        item_name = clean_text(row.get("ITEM_NAME"))
        operation_type = clean_text(row.get("MAINOP_TYPE"))
        if operation_type not in ("1", "2"):
            continue
        if not item_name or "其他" in item_name or item_name in ("合计", "营业收入"):
            continue
        candidates.append(row)
    latest_date = max(
        (clean_text(row.get("REPORT_DATE")) for row in candidates), default=""
    )
    latest = [
        row
        for row in candidates
        if clean_text(row.get("REPORT_DATE")) == latest_date
    ]
    product_rows = [row for row in latest if clean_text(row.get("MAINOP_TYPE")) == "2"]
    ranked = product_rows or latest
    ranked.sort(
        key=lambda row: (
            -float(row.get("MBI_RATIO") or 0),
            int(row.get("RANK") or 9999),
        )
    )
    main_business_raw = clean_text(ranked[0].get("ITEM_NAME")) if ranked else ""
    if not industry_raw:
        industry_rows = [
            row
            for row in business_payload.get("zygcfx") or []
            if clean_text(row.get("MAINOP_TYPE")) == "1"
            and clean_text(row.get("ITEM_NAME"))
            and "其他" not in clean_text(row.get("ITEM_NAME"))
        ]
        industry_date = max(
            (clean_text(row.get("REPORT_DATE")) for row in industry_rows), default=""
        )
        industry_latest = [
            row
            for row in industry_rows
            if clean_text(row.get("REPORT_DATE")) == industry_date
        ]
        industry_latest.sort(
            key=lambda row: (
                -float(row.get("MBI_RATIO") or 0),
                int(row.get("RANK") or 9999),
            )
        )
        industry_raw = (
            clean_text(industry_latest[0].get("ITEM_NAME"))
            if industry_latest
            else ""
        )
        industry_parts = [
            item for item in re.split(r"[-—/]", industry_raw) if clean_text(item)
        ]
        industry = compact_company_label(
            industry_parts[-1] if industry_parts else ""
        )
    main_business = compact_company_label(main_business_raw)
    missing = []
    if not industry:
        missing.append("industry")
    if not main_business:
        missing.append("mainBusiness")
    return {
        "status": "complete" if not missing else "incomplete",
        "industry": industry,
        "mainBusiness": main_business,
        "industryRaw": industry_raw,
        "mainBusinessRaw": main_business_raw,
        "mainBusinessReportDate": latest_date[:10],
        "mainBusinessIncomeRatioPct": (
            round(float(ranked[0].get("MBI_RATIO") or 0) * 100, 2)
            if ranked
            else None
        ),
        "missingFields": missing,
    }


def fetch_company_profile(
    client: HttpClient, code: str, output_dir: Path
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exchange_prefix = "SH" if code.startswith(("6", "68", "9")) else "SZ"
    query = urllib.parse.urlencode({"code": exchange_prefix + code})
    survey_url = COMPANY_SURVEY_URL + "?" + query
    business_url = BUSINESS_ANALYSIS_URL + "?" + query
    quote_url = COMPANY_INDUSTRY_FALLBACK_URL + "?" + urllib.parse.urlencode(
        {"secid": eastmoney_secid(code), "fields": "f57,f58,f127"}
    )
    headers = {"Referer": "https://emweb.securities.eastmoney.com/"}
    warnings: list[str] = []
    survey: dict[str, Any] = {}
    business: dict[str, Any] = {}
    quote: dict[str, Any] = {}

    def fetch_json_with_content_retry(url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                payload = client.get_json(url, headers=headers)
                if not isinstance(payload, dict) or not payload:
                    raise ValueError("接口返回空对象")
                return payload
            except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.6 * attempt)
        raise RuntimeError(f"内容校验失败（3次）：{last_error}")

    try:
        survey = fetch_json_with_content_retry(survey_url)
        atomic_write_json(output_dir / "company-survey-raw.json", survey)
    except Exception as exc:
        warnings.append(f"公司概况接口失败：{exc}")
    try:
        business = fetch_json_with_content_retry(business_url)
        atomic_write_json(output_dir / "business-analysis-raw.json", business)
    except Exception as exc:
        warnings.append(f"主营构成接口失败：{exc}")
    survey_row = ((survey.get("jbzl") or [{}])[0]) or {}
    if not (survey_row.get("EM2016") or survey_row.get("INDUSTRYCSRC1")):
        try:
            quote = fetch_json_with_content_retry(quote_url)
            atomic_write_json(output_dir / "company-industry-fallback-raw.json", quote)
        except Exception as exc:
            warnings.append(f"行业备用接口失败：{exc}")
    profile = parse_company_profile(survey, business, quote)
    profile.update(
        {
            "provider": "东方财富F10公司概况与主营构成",
            "surveyUrl": survey_url,
            "businessAnalysisUrl": business_url,
            "industryFallbackUrl": quote_url,
            "warnings": warnings,
        }
    )
    atomic_write_json(output_dir / "company-profile.json", profile)
    return profile


def parse_company_concepts(payload: dict[str, Any], limit: int = 5) -> list[str]:
    """Return ranked, company-specific concepts from Eastmoney F10."""
    rows = [
        row
        for row in payload.get("ssbk") or []
        if clean_text(row.get("IS_PRECISE")) == "1" and clean_text(row.get("BOARD_NAME"))
    ]
    rows.sort(key=lambda row: int(row.get("BOARD_RANK") or 9999))
    concepts: list[str] = []
    for row in rows:
        name = clean_text(row.get("BOARD_NAME"))
        if name and name not in concepts:
            concepts.append(name[:12])
        if len(concepts) >= limit:
            break
    return concepts


def fetch_company_concepts(
    client: HttpClient, code: str, output_dir: Path
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exchange_prefix = "SH" if code.startswith(("6", "68", "9")) else "SZ"
    url = COMPANY_CONCEPT_URL + "?" + urllib.parse.urlencode(
        {"code": exchange_prefix + code}
    )
    payload = client.get_json(
        url, headers={"Referer": "https://emweb.securities.eastmoney.com/"}
    )
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError("题材概念接口返回空对象")
    atomic_write_json(output_dir / "company-concepts-raw.json", payload)
    result = {
        "status": "complete",
        "concepts": parse_company_concepts(payload),
        "provider": "东方财富F10核心题材",
        "url": url,
    }
    if not result["concepts"]:
        result["status"] = "unavailable"
        result["warning"] = "题材概念接口未返回公司特定概念"
    atomic_write_json(output_dir / "company-concepts.json", result)
    return result


def fetch_market_snapshot(
    client: HttpClient, code: str, end: dt.datetime, event_dir: Path
) -> dict[str, Any]:
    market_cutoff = previous_market_day(end.date())
    secid = eastmoney_secid(code)
    exchange_prefix = "SH" if secid.startswith("1.") else "SZ"
    quote_url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/CapitalStockStructure/PageAjax?"
        + urllib.parse.urlencode({"code": exchange_prefix + code})
    )
    history_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(
        {
            "secid": secid,
            "klt": "101",
            "fqt": "0",
            "lmt": "45",
            "end": market_cutoff.strftime("%Y%m%d"),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
    )
    headers = {"Referer": "https://quote.eastmoney.com/"}
    quote = client.get_json(quote_url, headers=headers)
    atomic_write_json(event_dir / "market-quote-raw.json", quote)

    current_capital = (quote.get("gbjg") or [{}])[0]
    capital_history = quote.get("lngbbd") or []
    capital_date = clean_text((capital_history[0] if capital_history else {}).get("END_DATE"))[:10]
    rows: list[dict[str, Any]] = []
    history_provider = "东方财富不复权日线"
    security_name = ""
    warnings: list[str] = []
    try:
        history = client.get_json(history_url, headers=headers)
        atomic_write_json(event_dir / "market-history-raw.json", history)
        security_name = clean_text((history.get("data") or {}).get("name"))
        for raw in (history.get("data") or {}).get("klines") or []:
            parts = raw.split(",")
            if len(parts) < 11 or parts[0] > market_cutoff.isoformat():
                continue
            rows.append(
                {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volumeLots": float(parts[5]),
                    "turnoverYuan": float(parts[6]),
                    "changePct": float(parts[8]),
                }
            )
    except Exception as exc:
        warnings.append(f"东方财富日线失败，已切换搜狐证券：{exc}")
        start_date = (market_cutoff - dt.timedelta(days=90)).strftime("%Y%m%d")
        sohu_url = "https://q.stock.sohu.com/hisHq?" + urllib.parse.urlencode(
            {
                "code": f"cn_{code}",
                "start": start_date,
                "end": market_cutoff.strftime("%Y%m%d"),
                "stat": "1",
                "order": "D",
                "period": "d",
                "rt": "json",
            }
        )
        sohu_text, _ = client.get_text(
            sohu_url, headers={"Referer": "https://q.stock.sohu.com/"}
        )
        sohu = json.loads(sohu_text)
        atomic_write_json(event_dir / "market-history-sohu-raw.json", sohu)
        for parts in (sohu[0].get("hq") if sohu else []) or []:
            if len(parts) < 10 or parts[0] > market_cutoff.isoformat():
                continue
            rows.append(
                {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[6]),
                    "low": float(parts[5]),
                    "volumeLots": float(parts[7]),
                    "turnoverYuan": float(parts[8]) * 10_000,
                    "changePct": float(parts[4].rstrip("%")),
                }
            )
        rows.sort(key=lambda item: item["date"])
        history_provider = "搜狐证券不复权日线（东方财富日线失败后的备用源）"
        history_url = sohu_url
    if not rows:
        raise RuntimeError(f"行情接口未返回{code}在{market_cutoff.isoformat()}之前的日线")
    latest = rows[-1]
    closes = [row["close"] for row in rows]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    return {
        "provider": history_provider,
        "securityCode": code,
        "securityName": security_name,
        "totalShares": int(round(float(current_capital.get("TOTAL_SHARES") or 0))),
        "floatingShares": int(round(float(current_capital.get("UNLIMITED_SHARES") or 0))),
        "quoteUpdatedAt": capital_date,
        "latestClose": latest["close"],
        "latestCloseDate": latest["date"],
        "history": rows[-21:],
        "return10Pct": round((closes[-1] / closes[-11] - 1) * 100, 2) if len(closes) >= 11 else None,
        "return20Pct": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
        "dailyVolatility20Pct": round(statistics.stdev(returns[-20:]) * 100, 2) if len(returns) >= 2 else None,
        "quoteUrl": quote_url,
        "historyUrl": history_url,
        "warnings": warnings,
    }


def registration_calculation(
    amount_yuan: float,
    total_shares: int,
    close_price: float,
    conversion_price: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allocation = amount_yuan / total_shares
    n0 = 1000 / allocation
    conservative_shares = int(math.ceil(n0 / 100) * 100)
    capital = conservative_shares * close_price
    leverage = 1000 / capital
    break_even = {
        str(gain): round(leverage * (gain / 100) * 100, 2)
        for gain in (15, 20, 30, 40, 50)
    }
    result = {
        "registeredAmountYuan": round(amount_yuan, 2),
        "totalShares": total_shares,
        "closePrice": close_price,
        "allocationPerShare": round(allocation, 6),
        "allocationPer100Shares": round(allocation * 100, 2),
        "rightsValuePct": round(allocation / close_price * 100, 2),
        "n0Shares": round(n0, 2),
        "conservativeShares": conservative_shares,
        "capitalRequiredYuan": round(capital, 2),
        "bondToCapitalPct": round(leverage * 100, 2),
        "breakEvenStockDropPct": break_even,
    }
    if conversion_price:
        result.update(conversion_price)
    return result


def formal_calculation(
    amount_yuan: float,
    total_shares: int,
    allocation_per_share: float,
    close_price: float,
    initial_conversion_price: float,
) -> dict[str, Any]:
    n0 = 1000 / allocation_per_share
    conservative_shares = int(math.ceil(n0 / 100) * 100)
    capital = conservative_shares * close_price
    leverage = 1000 / capital
    return {
        "finalAmountYuan": round(amount_yuan, 2),
        "totalShares": total_shares,
        "allocationPerShare": round(allocation_per_share, 6),
        "allocationPer100Shares": round(allocation_per_share * 100, 2),
        "closePrice": close_price,
        "initialConversionPrice": initial_conversion_price,
        "stockToConversionPricePct": round(
            close_price / initial_conversion_price * 100, 2
        ),
        "conversionValue": round(close_price / initial_conversion_price * 100, 2),
        "rightsValuePct": round(allocation_per_share / close_price * 100, 2),
        "n0Shares": round(n0, 2),
        "conservativeShares": conservative_shares,
        "capitalRequiredYuan": round(capital, 2),
        "bondToCapitalPct": round(leverage * 100, 2),
        "breakEvenStockDropPct": {
            str(gain): round(leverage * (gain / 100) * 100, 2)
            for gain in (15, 20, 30, 40, 50)
        },
    }


def reference_conversion_price_metrics(
    market: dict[str, Any], as_of_date: str | None = None
) -> dict[str, Any] | None:
    """Estimate the pre-issuance conversion-price floor from market turnover data.

    The actual initial conversion price is not fixed until the formal prospectus is
    published.  This proxy uses the statutory lower-bound formula as if the next
    trading day were the pricing date: max(20-day VWAP, previous-day VWAP).
    """
    rows = []
    for row in market.get("history") or []:
        if as_of_date and str(row.get("date") or "") > as_of_date:
            continue
        turnover = float(row.get("turnoverYuan") or 0)
        volume_shares = float(row.get("volumeLots") or 0) * 100
        if turnover > 0 and volume_shares > 0:
            rows.append(
                {
                    "date": row.get("date", ""),
                    "turnoverYuan": turnover,
                    "volumeShares": volume_shares,
                }
            )
    if len(rows) < 20:
        return None
    window = rows[-20:]
    twenty_day_average = sum(row["turnoverYuan"] for row in window) / sum(
        row["volumeShares"] for row in window
    )
    previous_day_average = (
        window[-1]["turnoverYuan"] / window[-1]["volumeShares"]
    )
    raw_floor = max(twenty_day_average, previous_day_average)
    price_floor = math.ceil((raw_floor - 1e-10) * 100) / 100
    latest_close = float(market.get("latestClose") or 0)
    return {
        "referenceConversionPrice": price_floor,
        "referenceConversionPrice20DayAverage": round(twenty_day_average, 4),
        "referenceConversionPricePreviousDayAverage": round(previous_day_average, 4),
        "stockToConversionPricePct": (
            round(latest_close / price_floor * 100, 2) if latest_close else None
        ),
        "referenceConversionPriceAsOfDate": window[-1]["date"],
        "conversionPriceStatus": "pre_issuance_reference",
    }


def calculation_html(
    code: str,
    company: str,
    calculation: dict[str, Any],
    amount_source: dict[str, Any],
    shares_source: dict[str, Any],
    market: dict[str, Any],
    company_profile: dict[str, Any],
) -> str:
    gains = calculation["breakEvenStockDropPct"]
    document_label = amount_source.get("documentLabel") or "可转债申报文件"
    stress_rows = "".join(
        f"<tr><td>{gain}%</td><td>{gains[str(gain)]:.2f}%</td></tr>"
        for gain in (15, 20, 30, 40, 50)
    )
    return f"""<section data-generated="calculation" data-calculation-code="{escape(code)}">
<h3>关键数据与计算（脚本取数并校验）</h3>
<table border="1" cellspacing="0" cellpadding="6">
<tr><th>项目</th><th>数值</th><th>口径与日期</th></tr>
<tr><td>行业｜主营业务</td><td>{escape(company_profile['industry'])}<br>{escape(company_profile['mainBusiness'])}</td><td>{escape(company_profile['mainBusinessReportDate'])}最新披露主营构成；每项不超过10字</td></tr>
<tr><td>募集规模上限</td><td>{calculation['registeredAmountYuan'] / 100000000:.2f}亿元</td><td>{escape(amount_source['documentDate'])}《{escape(document_label)}》</td></tr>
<tr><td>最新总股本</td><td>{calculation['totalShares']:,}股</td><td>{escape(shares_source['asOf'])}；{escape(shares_source['verification'])}</td></tr>
<tr><td>最近收盘价 S</td><td>{calculation['closePrice']:.2f}元</td><td>{escape(market['latestCloseDate'])}不复权收盘价</td></tr>
<tr><td>参考转股价下限 P0</td><td>{calculation['referenceConversionPrice']:.2f}元</td><td>截至{escape(calculation['referenceConversionPriceAsOfDate'])}：max（前20日成交均价{calculation['referenceConversionPrice20DayAverage']:.4f}元，前1日成交均价{calculation['referenceConversionPricePreviousDayAverage']:.4f}元），向上取整到0.01元</td></tr>
<tr><td>正股现价/转股价</td><td>{calculation['stockToConversionPricePct']:.0f}%</td><td>S ÷ P0；正式发行前使用参考转股价下限，发行后改用公告的实际初始转股价</td></tr>
<tr><td>理论每股配债 p</td><td>{calculation['allocationPerShare']:.4f}元/股</td><td>募集规模上限 ÷ 最新总股本</td></tr>
<tr><td>每100股理论可配面值</td><td>{calculation['allocationPer100Shares']:.2f}元</td><td>100 × p</td></tr>
<tr><td>理论百元含权率</td><td>{calculation['rightsValuePct']:.2f}%</td><td>p ÷ S</td></tr>
<tr><td>理论配1手所需股数 N0</td><td>{calculation['n0Shares']:.2f}股</td><td>1000 ÷ p</td></tr>
<tr><td>保守稳1手股数 N</td><td>{calculation['conservativeShares']:,}股</td><td>向上取整到100股；最终以发行公告实际配售比例为准</td></tr>
<tr><td>对应正股资金 M</td><td>{calculation['capitalRequiredYuan']:,.2f}元</td><td>N × S</td></tr>
<tr><td>每千元转债/正股资金 H</td><td>{calculation['bondToCapitalPct']:.2f}%</td><td>1000 ÷ M</td></tr>
</table>
<p><strong>正股盈亏平衡跌幅 D*</strong>（忽略交易成本，约等于 H × 转债上市涨幅）：</p>
<table border="1" cellspacing="0" cellpadding="6"><tr><th>假设转债上市涨幅</th><th>正股可承受跌幅 D*</th></tr>{stress_rows}</table>
<p>行情压力参考：近10日累计涨跌 {market.get('return10Pct')}%，近20日累计涨跌 {market.get('return20Pct')}%，20日单日波动率约 {market.get('dailyVolatility20Pct')}%。以上为正式发行前的早期估算，不代表最终配售资格；最终发行规模、每股优先配售额、股权登记日、申购日和转股价仍以正式发行公告为准。</p>
<p>数据来源：<a href="{escape(amount_source['url'])}">巨潮资讯《{escape(document_label)}》</a>；<a href="{escape(market['historyUrl'])}">{escape(market['provider'])}</a>；<a href="{escape(market['quoteUrl'])}">东方财富F10股本结构</a>。</p>
</section>"""


def formal_calculation_html(
    code: str,
    calculation: dict[str, Any],
    terms: dict[str, Any],
    market: dict[str, Any],
    company_profile: dict[str, Any],
    source: dict[str, Any],
) -> str:
    stress_rows = "".join(
        f"<tr><td>{gain}%</td><td>{calculation['breakEvenStockDropPct'][str(gain)]:.2f}%</td></tr>"
        for gain in (15, 20, 30, 40, 50)
    )
    record_date = terms.get("recordDate") or "以正式文件为准"
    subscription_date = terms.get("subscriptionDate") or "以正式文件为准"
    return f"""<section data-generated="formal-calculation" data-calculation-code="{escape(code)}">
<h3>关键数据与计算（脚本取数并校验）</h3>
<table border="1" cellspacing="0" cellpadding="6">
<tr><th>项目</th><th>数值</th><th>口径与日期</th></tr>
<tr><td>行业｜主营业务</td><td>{escape(company_profile['industry'])}<br>{escape(company_profile['mainBusiness'])}</td><td>{escape(company_profile['mainBusinessReportDate'])} 最新披露主营构成；每项不超过10字</td></tr>
<tr><td>最终发行规模</td><td>{calculation['finalAmountYuan'] / 100000000:.2f}亿元</td><td>{escape(source.get('date', ''))} 正式发行文件</td></tr>
<tr><td>可配售股份基数</td><td>{calculation['totalShares']:,}股</td><td>{escape((terms.get('shareSnapshot') or {}).get('asOfDate') or source.get('date', ''))} 正式文件披露总股本</td></tr>
<tr><td>实际每股优先配售额 p</td><td>{calculation['allocationPerShare']:.4f}元/股</td><td>正式文件披露实际比例，不用发行额÷股本替代</td></tr>
<tr><td>最新已完成交易日收盘价 S</td><td>{calculation['closePrice']:.2f}元</td><td>{escape(market['latestCloseDate'])} 不复权收盘价；盘中运行时不使用当日未收盘价格</td></tr>
<tr><td>初始转股价</td><td>{calculation['initialConversionPrice']:.2f}元</td><td>{escape(source.get('date', ''))} 正式发行文件</td></tr>
<tr><td>正股现价/转股价</td><td>{calculation['stockToConversionPricePct']:.0f}%</td><td>S÷初始转股价；转股价值为 {calculation['conversionValue']:.2f}元</td></tr>
<tr><td>每100股理论可配面值</td><td>{calculation['allocationPer100Shares']:.2f}元</td><td>100×p</td></tr>
<tr><td>理论百元含权率</td><td>{calculation['rightsValuePct']:.2f}%</td><td>p÷S</td></tr>
<tr><td>理论配1手所需股数 N0</td><td>{calculation['n0Shares']:.2f}股</td><td>1000÷p</td></tr>
<tr><td>保守稳1手股数 N</td><td>{calculation['conservativeShares']:,}股</td><td>按100股交易单位向上取整；尾数获配仍以交易所规则为准</td></tr>
<tr><td>对应正股资金 M</td><td>{calculation['capitalRequiredYuan']:,.2f}元</td><td>N×S；另需预留1,000元配债款</td></tr>
<tr><td>每千元转债/正股资金 H</td><td>{calculation['bondToCapitalPct']:.2f}%</td><td>1000÷M，非收益率</td></tr>
<tr><td>股权登记日/申购日</td><td>{escape(record_date)} / {escape(subscription_date)}</td><td>正式发行日程</td></tr>
</table>
<p><strong>正股盈亏平衡跌幅 D*</strong>（忽略交易成本，约等于 H×转债上市涨幅）：</p>
<table border="1" cellspacing="0" cellpadding="6"><tr><th>假设转债上市涨幅</th><th>正股可承受跌幅 D*</th></tr>{stress_rows}</table>
<p>行情压力参考：近10日累计涨跌 {market.get('return10Pct')}%，近20日累计涨跌 {market.get('return20Pct')}%，20日单日波动率约 {market.get('dailyVolatility20Pct')}%。数据截止 {escape(market['latestCloseDate'])}，均为程序抓取并校验，不使用盘中价格。</p>
<p>数据来源：<a href="{escape(source.get('url', ''))}">正式发行文件</a>；<a href="{escape(market['historyUrl'])}">{escape(market['provider'])}</a>；<a href="{escape(market['quoteUrl'])}">股本结构与行情</a>。</p>
</section>"""


def enrich_formal_evidence(
    client: HttpClient,
    config: dict[str, Any],
    candidate: dict[str, Any],
    window_end: dt.datetime,
    event_dir: Path,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    missing: list[str] = []
    warnings: list[str] = []
    source = candidate.get("primary") or candidate.get("cninfo") or {}
    parsed_documents: list[dict[str, Any]] = []
    for document in documents:
        document_path = Path(str(document.get("path") or ""))
        if not document_path.exists():
            continue
        try:
            parsed_documents.append(
                {
                    "terms": parse_formal_terms(pdf_text(document_path)),
                    "source": {
                        key: document.get(key)
                        for key in (
                            "layer",
                            "source",
                            "sourceId",
                            "title",
                            "date",
                            "url",
                            "path",
                            "sha256",
                        )
                        if document.get(key) is not None
                    },
                }
            )
        except Exception as exc:
            warnings.append(
                f"正式发行文件解析失败：{document.get('title') or document_path.name}；{exc}"
            )
    terms = merge_formal_term_sets(parsed_documents) if parsed_documents else {}
    if not parsed_documents:
        warnings.append("正式发行文件未成功下载")
    amount = (terms.get("finalAmount") or {}).get("valueYuan")
    shares = int(((terms.get("shareSnapshot") or {}).get("value") or 0))
    allocation = terms.get("allocationPerShare")
    conversion_price = terms.get("initialConversionPrice")
    try:
        market = fetch_market_snapshot(client, candidate["code"], window_end, event_dir)
    except Exception as exc:
        market = None
        warnings.append(f"行情补证失败：{exc}")
    company_profile = fetch_company_profile(client, candidate["code"], event_dir / "company-profile")
    warnings.extend(company_profile.get("warnings") or [])
    try:
        company_concepts = fetch_company_concepts(
            client, candidate["code"], event_dir / "company-concepts"
        )
        if company_concepts.get("warning"):
            warnings.append(str(company_concepts["warning"]))
    except Exception as exc:
        company_concepts = {
            "status": "unavailable",
            "concepts": [],
            "warning": f"题材概念补证失败：{exc}",
        }
        warnings.append(company_concepts["warning"])
    if not amount:
        missing.append("finalAmount")
    if not shares:
        missing.append("totalShares")
    if not allocation:
        missing.append("allocationPerShare")
    if not conversion_price:
        missing.append("initialConversionPrice")
    for field in ("subscriptionDate", "bondShortName", "bondCode", "subscriptionCode"):
        if not terms.get(field):
            missing.append(field)
    if terms.get("conflicts"):
        missing.append("formalTermConflict")
        warnings.append(
            "正式发行文件字段冲突："
            + ", ".join(item["field"] for item in terms["conflicts"])
        )
    if (terms.get("consistency") or {}).get("status") == "conflict":
        missing.append("allocationConsistency")
        warnings.append("发行额、可配售股本与每股配售额未通过一致性校验")
    if not market or not market.get("latestClose"):
        missing.append("latestClose")
    missing.extend(company_profile.get("missingFields") or [])
    result: dict[str, Any] = {
        "status": "incomplete" if missing else "complete",
        "missingFields": sorted(set(missing)),
        "warnings": warnings,
        "formalTerms": terms,
        "marketSnapshot": market,
        "companyProfile": company_profile,
        "companyConcepts": company_concepts,
        "source": source,
    }
    if not missing and market:
        calculation = formal_calculation(
            float(amount),
            shares,
            float(allocation),
            float(market["latestClose"]),
            float(conversion_price),
        )
        result.update(
            {
                "inputs": {
                    "finalAmount": terms["finalAmount"],
                    "totalShares": terms["shareSnapshot"],
                    "allocationPerShare": allocation,
                    "latestClose": {
                        "value": market["latestClose"],
                        "date": market["latestCloseDate"],
                        "priceType": "不复权收盘价",
                    },
                    "initialConversionPrice": conversion_price,
                    "companyProfile": company_profile,
                    "companyConcepts": company_concepts,
                },
                "results": calculation,
                "html": formal_calculation_html(
                    candidate["code"], calculation, terms, market, company_profile, source
                ),
            }
        )
    return result


def enrich_registration_evidence(
    client: HttpClient,
    config: dict[str, Any],
    candidate: dict[str, Any],
    window_end: dt.datetime,
    event_dir: Path,
) -> dict[str, Any]:
    missing: list[str] = []
    warnings: list[str] = []
    documents: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    amount: dict[str, Any] | None = None
    official_shares: dict[str, Any] | None = None
    supplement_download: dict[str, Any] | None = None
    market: dict[str, Any] | None = None
    conversion_price: dict[str, Any] | None = None
    company_profile: dict[str, Any] | None = None
    try:
        documents, raw = cninfo_company_documents(
            client,
            config["officialSources"]["cninfoAnnouncements"],
            candidate["code"],
            candidate["company"],
            window_end,
            int(config.get("enrichment", {}).get("cninfoLookbackDays", 800)),
        )
        atomic_write_json(event_dir / "supplemental-cninfo-query.json", raw)
        selected = choose_registration_document(documents, candidate["date"])
        if selected:
            target = event_dir / "registration-prospectus.pdf"
            supplement_download = download_evidence(
                client, selected["url"], target, "https://www.cninfo.com.cn/"
            )
            text = pdf_text(target)
            amount = parse_registration_amount(text)
            official_shares = parse_latest_share_snapshot(text)
        else:
            missing.append("registrationProspectus")
    except Exception as exc:
        warnings.append(f"官方注册稿补证失败：{exc}")

    try:
        market = fetch_market_snapshot(client, candidate["code"], window_end, event_dir)
    except Exception as exc:
        warnings.append(f"行情补证失败：{exc}")

    company_profile = fetch_company_profile(client, candidate["code"], event_dir)
    warnings.extend(company_profile.get("warnings") or [])

    if not amount:
        missing.append("registeredAmount")
    if not market or not market.get("latestClose"):
        missing.append("latestClose")
    if market:
        conversion_price = reference_conversion_price_metrics(market)
    if not conversion_price:
        missing.append("referenceConversionPrice")
    missing.extend(company_profile.get("missingFields") or [])
    market_shares = int(market.get("totalShares") or 0) if market else 0
    total_shares = market_shares or int((official_shares or {}).get("value") or 0)
    if not total_shares:
        missing.append("totalShares")
    if market_shares and official_shares:
        gap = abs(market_shares - official_shares["value"]) / official_shares["value"]
        if gap > 0.005:
            warnings.append(
                f"行情接口总股本{market_shares}与官方文件{official_shares['value']}差异{gap:.2%}"
            )
    if market:
        warnings.extend(market.get("warnings", []))

    result: dict[str, Any] = {
        "status": "incomplete" if missing else "complete",
        "missingFields": sorted(set(missing)),
        "warnings": warnings,
        "supplementalDocuments": documents,
        "selectedDocument": selected,
        "supplementalDownload": supplement_download,
        "officialShareSnapshot": official_shares,
        "marketSnapshot": market,
        "companyProfile": company_profile,
        "parsedRegisteredAmount": (
            {
                "valueYuan": amount["valueYuan"],
                "rawText": amount["rawText"],
                "url": selected["url"],
                "documentDate": selected["date"],
                "documentLabel": selected["title"],
            }
            if amount and selected
            else None
        ),
    }
    if not missing and amount and market:
        calc = registration_calculation(
            amount["valueYuan"],
            total_shares,
            float(market["latestClose"]),
            conversion_price,
        )
        amount_source = {
            "url": selected["url"],
            "documentDate": selected["date"],
            "documentLabel": selected["title"],
            "rawText": amount["rawText"],
        }
        shares_source = {
            "value": total_shares,
            "asOf": market.get("quoteUpdatedAt") or official_shares.get("asOfDate", ""),
            "officialValue": official_shares.get("value") if official_shares else None,
            "officialAsOfDate": official_shares.get("asOfDate") if official_shares else None,
            "verification": (
                "官方注册稿与行情接口总股本一致"
                if market_shares and official_shares and market_shares == official_shares["value"]
                else "采用官方注册稿披露股本；行情股本接口未完成交叉核验"
                if official_shares and not market_shares
                else "采用最新行情股本快照"
            ),
        }
        result.update(
            {
                "inputs": {
                    "registeredAmount": amount_source,
                    "totalShares": shares_source,
                    "latestClose": {
                        "value": market["latestClose"],
                        "date": market["latestCloseDate"],
                        "priceType": "不复权收盘价",
                    },
                    "referenceConversionPrice": conversion_price,
                    "companyProfile": company_profile,
                },
                "results": calc,
                "html": calculation_html(
                    candidate["code"],
                    candidate["company"],
                    calc,
                    amount_source,
                    shares_source,
                    market,
                    company_profile,
                ),
            }
        )
    return result


def build_evidence_pack(
    client: HttpClient,
    config: dict[str, Any],
    candidate: dict[str, Any],
    source_statuses: list[SourceStatus],
    window_start: dt.datetime,
    window_end: dt.datetime,
    run_dir: Path,
) -> Path:
    company = candidate.get("company") or candidate.get("code") or "unknown"
    event_dir = run_dir / safe_file_name(f"{candidate['code']}_{company}_{candidate['stage']}")
    event_dir.mkdir(parents=True, exist_ok=True)
    downloads: list[dict[str, Any]] = []
    for layer_name in ("primary", "cninfo", "csrc"):
        record = candidate.get(layer_name)
        if not record or not record.get("url"):
            continue
        url = record["url"]
        suffix = ".pdf" if ".pdf" in url.lower() else ".html"
        target = event_dir / f"{layer_name}{suffix}"
        try:
            item = download_evidence(
                client,
                url,
                target,
                "https://www.sse.com.cn/"
                if record.get("market") == "SSE"
                else "https://www.szse.cn/"
                if record.get("market") == "SZSE"
                else "https://www.csrc.gov.cn/",
            )
            item["layer"] = layer_name
            item.update(
                {
                    "source": record.get("source"),
                    "sourceId": record.get("source_id"),
                    "title": record.get("title"),
                    "date": record.get("date"),
                }
            )
            downloads.append(item)
        except Exception as exc:
            downloads.append(
                {
                    "layer": layer_name,
                    "url": url,
                    "source": record.get("source"),
                    "sourceId": record.get("source_id"),
                    "title": record.get("title"),
                    "date": record.get("date"),
                    "error": str(exc),
                }
            )
    if candidate.get("stage") == "formal":
        for index, record in enumerate(supplemental_formal_records(candidate), start=1):
            url = record["url"]
            suffix = ".pdf" if ".pdf" in url.lower() else ".html"
            layer_name = f"formal-supplement-{index}"
            target = event_dir / f"{layer_name}{suffix}"
            try:
                item = download_evidence(
                    client,
                    url,
                    target,
                    "https://www.sse.com.cn/"
                    if record.get("market") == "SSE"
                    else "https://www.szse.cn/"
                    if record.get("market") == "SZSE"
                    else "https://www.cninfo.com.cn/",
                )
                item.update(
                    {
                        "layer": layer_name,
                        "source": record.get("source"),
                        "sourceId": record.get("source_id"),
                        "title": record.get("title"),
                        "date": record.get("date"),
                    }
                )
                downloads.append(item)
            except Exception as exc:
                downloads.append(
                    {
                        "layer": layer_name,
                        "url": url,
                        "source": record.get("source"),
                        "sourceId": record.get("source_id"),
                        "title": record.get("title"),
                        "date": record.get("date"),
                        "error": str(exc),
                    }
                )
    calculation = None
    company_profile = None
    if candidate.get("stage") == "registration":
        calculation = enrich_registration_evidence(
            client, config, candidate, window_end, event_dir
        )
        company_profile = calculation.get("companyProfile")
    else:
        formal_documents: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for item in downloads:
            if not item.get("pdfValidated") or not item.get("path"):
                continue
            digest = str(item.get("sha256") or item["path"])
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            formal_documents.append(item)
        calculation = enrich_formal_evidence(
            client, config, candidate, window_end, event_dir, formal_documents
        )
        company_profile = calculation.get("companyProfile")
    pack = {
        "schemaVersion": 1,
        "createdAt": window_end.isoformat(),
        "checkWindow": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "candidate": candidate,
        "sourceStatuses": [asdict(status) for status in source_statuses],
        "downloads": downloads,
        "calculation": calculation,
        "companyProfile": company_profile,
        "analysisStatus": "pending",
        "reportingRule": "未完成Codex深度分析和邮件发送前，不得标记为已报告",
    }
    path = event_dir / "evidence.json"
    atomic_write_json(path, pack)
    return path


def partition_event_evidence(
    candidates: list[dict[str, Any]], evidence_paths: list[Path]
) -> tuple[list[tuple[dict[str, Any], Path]], list[dict[str, Any]]]:
    """Keep complete events deliverable while incomplete peers remain retryable."""
    complete_events: list[tuple[dict[str, Any], Path]] = []
    incomplete_events: list[dict[str, Any]] = []
    for candidate, evidence_path in zip(candidates, evidence_paths):
        evidence_pack = read_json(evidence_path, {})
        calculation = evidence_pack.get("calculation") or {}
        if calculation.get("status") == "complete":
            complete_events.append((candidate, evidence_path))
        else:
            incomplete_events.append(
                {
                    "candidate": candidate,
                    "evidencePath": str(evidence_path.resolve()),
                    "missingFields": calculation.get("missingFields") or ["calculation"],
                    "warnings": calculation.get("warnings") or [],
                }
            )
    return complete_events, incomplete_events


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def source_rows(statuses: list[SourceStatus]) -> str:
    rows = []
    labels = {
        "sse": "上交所原始公告",
        "szse": "深交所原始公告",
        "cninfo": "巨潮资讯",
        "csrc": "证监会官网搜索",
    }
    for status in statuses:
        if status.source not in labels:
            continue
        verdict = "成功" if status.ok else "失败"
        rows.append(
            "<tr>"
            f"<td>{escape(labels[status.source])}</td>"
            f"<td>{escape(verdict)}</td>"
            f"<td>{status.count}</td>"
            f"<td>{escape(status.detail)}</td>"
            "</tr>"
        )
    return "".join(rows)


def previous_market_day(day: dt.date) -> dt.date:
    current = day - dt.timedelta(days=1)
    while True:
        closed, _ = cached_closed_dates(current)
        if current.weekday() < 5 and current not in closed:
            return current
        current -= dt.timedelta(days=1)


def market_days_between(start: dt.date, end: dt.date) -> int:
    """Count exchange trading days after start and through end."""
    if end <= start:
        return 0
    current = start + dt.timedelta(days=1)
    count = 0
    while current <= end:
        closed, _ = cached_closed_dates(current)
        if current.weekday() < 5 and current not in closed:
            count += 1
        current += dt.timedelta(days=1)
    return count


def fetch_inventory_history(
    client: HttpClient,
    code: str,
    start: dt.date,
    end: dt.date,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    exchange = "sh" if code.startswith(("6", "68", "9")) else "sz"
    quote_key = exchange + code
    tencent_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode(
        {
            "param": f"{quote_key},day,{start.isoformat()},{end.isoformat()},1000,qfq"
        }
    )
    try:
        payload = client.get_json(
            tencent_url, headers={"Referer": "https://gu.qq.com/"}
        )
        atomic_write_json(output_dir / "inventory-history-tencent-qfq.json", payload)
        node = (payload.get("data") or {}).get(quote_key) or {}
        for parts in node.get("qfqday") or node.get("day") or []:
            if len(parts) < 6:
                continue
            rows.append(
                {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                }
            )
        rows.sort(key=lambda item: item["date"])
        if rows:
            return {
                "provider": "腾讯证券前复权日线",
                "url": tencent_url,
                "rows": rows,
                "warnings": warnings,
            }
        warnings.append("腾讯证券前复权日线返回空数据，已切换备用源")
    except Exception as exc:
        warnings.append(f"腾讯证券前复权日线失败，已切换搜狐证券：{exc}")
    rows = []
    sohu_url = "https://q.stock.sohu.com/hisHq?" + urllib.parse.urlencode(
        {
            "code": f"cn_{code}",
            "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "stat": "1",
            "order": "D",
            "period": "d",
            "rt": "json",
        }
    )
    provider = "搜狐证券不复权日线"
    source_url = sohu_url
    try:
        text, _ = client.get_text(
            sohu_url, headers={"Referer": "https://q.stock.sohu.com/"}
        )
        payload = json.loads(text)
        atomic_write_json(output_dir / "inventory-history-sohu.json", payload)
        for parts in (payload[0].get("hq") if payload else []) or []:
            if len(parts) < 10:
                continue
            rows.append(
                {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[6]),
                    "low": float(parts[5]),
                    "changePct": float(parts[4].rstrip("%")),
                }
            )
    except Exception as exc:
        warnings.append(f"搜狐证券日线失败，已切换东方财富：{exc}")
        secid = eastmoney_secid(code)
        source_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(
            {
                "secid": secid,
                "klt": "101",
                "fqt": "0",
                "lmt": "1000",
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
        )
        provider = "东方财富不复权日线（搜狐失败后的备用源）"
        payload = client.get_json(
            source_url, headers={"Referer": "https://quote.eastmoney.com/"}
        )
        atomic_write_json(output_dir / "inventory-history-eastmoney.json", payload)
        for raw in (payload.get("data") or {}).get("klines") or []:
            parts = raw.split(",")
            if len(parts) < 11:
                continue
            rows.append(
                {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "changePct": float(parts[8]),
                }
            )
    rows.sort(key=lambda item: item["date"])
    if not rows:
        raise RuntimeError(f"{code}在{start}至{end}没有可用日线")
    return {
        "provider": provider,
        "url": source_url,
        "rows": rows,
        "warnings": warnings,
    }


def registration_entry(candidate: dict[str, Any]) -> dict[str, Any]:
    source = candidate.get("primary") or candidate.get("cninfo") or {}
    return {
        "code": candidate.get("code", ""),
        "company": candidate.get("company", ""),
        "market": source.get("market", ""),
        "registrationDate": candidate.get("date", ""),
        "registrationTitle": source.get("title", ""),
        "registrationUrl": source.get("url", ""),
        "registeredAmountYuan": None,
        "amountSource": None,
        "lastMetrics": None,
    }


def backfill_pending_entries(
    client: HttpClient,
    config: dict[str, Any],
    now: dt.datetime,
    raw_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    settings = config.get("inventory", {})
    lookback_days = int(settings.get("lookbackDays", 370))
    validity_days = int(settings.get("approvalValidityDays", 365))
    start = now - dt.timedelta(days=lookback_days)
    sources = config["officialSources"]
    endpoint = sources["cninfoAnnouncements"]
    registrations: list[Announcement] = []
    exchange_records: list[Announcement] = []
    raw: dict[str, Any] = {
        "registrationSearch": {},
        "companyChecks": {},
        "warnings": [],
    }
    exchange_fetches = (
        (
            "sse",
            fetch_sse,
            sources["sseAnnouncements"],
        ),
        (
            "szse",
            fetch_szse,
            sources["szseAnnouncements"],
        ),
    )
    history_pages = max(int(config.get("maxPages", 8)), 12)
    for source_name, fetcher, source_endpoint in exchange_fetches:
        records, pages = fetcher(
            client,
            source_endpoint,
            start,
            now,
            history_pages,
        )
        exchange_records.extend(records)
        registrations.extend(record for record in records if record.stage == "registration")
        raw["registrationSearch"][source_name] = pages
    for column, plate in (("sse", "sh"), ("szse", "sz")):
        try:
            records, pages = fetch_cninfo_market(
                client,
                endpoint,
                start,
                now,
                column,
                plate,
                history_pages,
                "同意注册 可转换公司债券",
            )
            registrations.extend(record for record in records if record.stage == "registration")
            raw["registrationSearch"][f"cninfo_{plate}"] = pages
        except Exception as exc:
            raw["registrationSearch"][f"cninfo_{plate}"] = {"error": str(exc)}
            raw["warnings"].append(f"巨潮{plate}历史注册检索失败，采用交易所结果：{exc}")
    latest: dict[str, Announcement] = {}
    for record in registrations:
        if not re.fullmatch(r"\d{6}", record.code):
            continue
        previous = latest.get(record.code)
        if previous is None or record.date > previous.date:
            latest[record.code] = record
    entries: dict[str, dict[str, Any]] = {}
    for code, record in sorted(latest.items()):
        registration_day = dt.date.fromisoformat(record.date)
        if (now.date() - registration_day).days > validity_days:
            continue
        try:
            documents, company_raw = cninfo_company_documents(
                client,
                endpoint,
                code,
                record.company,
                now,
                max(lookback_days, 800),
            )
        except Exception as exc:
            documents = []
            company_raw = {"error": str(exc)}
            raw["warnings"].append(
                f"{record.company}巨潮公司公告复核失败，采用交易所正式发行检索结果：{exc}"
            )
        formal = [
            record_dict(document)
            for document in exchange_records
            if document.code == code
            and document.stage == "formal"
            and document.date >= record.date
        ]
        for document in documents:
            stage, _ = classify_title(document.get("title", ""))
            if stage == "formal" and document.get("date", "") >= record.date:
                formal.append(document)
        raw["companyChecks"][code] = {
            "registration": record_dict(record),
            "documents": documents,
            "formalDocuments": formal,
            "query": company_raw,
        }
        if formal:
            continue
        candidate = {
            "code": code,
            "company": record.company,
            "date": record.date,
            "primary": None,
            "cninfo": record_dict(record),
        }
        entries[code] = registration_entry(candidate)
    atomic_write_json(raw_dir / "inventory-backfill.json", raw)
    return entries, raw


def apply_inventory_events(
    entries: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]
) -> None:
    for candidate in candidates:
        if candidate.get("stage") == "registration" and candidate.get("code"):
            old = entries.get(candidate["code"])
            new = registration_entry(candidate)
            if old and old.get("registrationDate") == new.get("registrationDate"):
                new["registeredAmountYuan"] = old.get("registeredAmountYuan")
                new["amountSource"] = old.get("amountSource")
                new["lastMetrics"] = old.get("lastMetrics")
            entries[candidate["code"]] = new
    for candidate in candidates:
        if candidate.get("stage") == "formal" and candidate.get("code"):
            entries.pop(candidate["code"], None)


def preserve_inventory_enrichment(
    rebuilt: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> None:
    """Keep verified enrichment when a full refresh rediscovers the same event."""
    cached_fields = (
        "registeredAmountYuan",
        "amountSource",
        "officialTotalShares",
        "companyProfile",
        "lastMetrics",
        "metricsError",
    )
    for code, entry in rebuilt.items():
        old = previous.get(code) or {}
        if old.get("registrationDate") != entry.get("registrationDate"):
            continue
        for field_name in cached_fields:
            if old.get(field_name) not in (None, ""):
                entry[field_name] = old[field_name]


def calculate_inventory_metrics(
    entry: dict[str, Any],
    market: dict[str, Any],
    history: dict[str, Any],
    previous_day: dt.date,
    company_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registration_day = dt.date.fromisoformat(entry["registrationDate"])
    rows = [row for row in history["rows"] if row["date"] <= previous_day.isoformat()]
    basis_candidates = [row for row in rows if row["date"] <= entry["registrationDate"]]
    if not basis_candidates or not rows:
        raise RuntimeError("缺少同意注册公告日或上一交易日的收盘价")
    basis = basis_candidates[-1]
    latest = rows[-1]
    since_rows = [row for row in rows if row["date"] >= basis["date"]]
    highest_close = max(row["close"] for row in since_rows)
    total_shares = int(market.get("totalShares") or 0)
    amount = float(entry.get("registeredAmountYuan") or 0)
    if not total_shares or not amount:
        raise RuntimeError("募集规模上限或最新总股本缺失")
    per_share = amount / total_shares
    n0_shares = 1000 / per_share
    shares_for_one_lot = int(math.floor(n0_shares + 0.5))
    conversion_price = reference_conversion_price_metrics(
        market, previous_day.isoformat()
    )
    profile = company_profile or {}
    recent10 = rows[-11:] if len(rows) >= 11 else rows
    return {
        "asOfDate": latest["date"],
        "latestClose": latest["close"],
        "registrationBasisDate": basis["date"],
        "registrationBasisClose": basis["close"],
        "tradingDaysElapsed": market_days_between(registration_day, previous_day),
        "approvalDaysRemainingEstimate": max(
            0, 365 - (previous_day - registration_day).days
        ),
        "cumulativeChangePct": round((latest["close"] / basis["close"] - 1) * 100, 2),
        "recent10ChangePct": (
            round((recent10[-1]["close"] / recent10[0]["close"] - 1) * 100, 2)
            if len(recent10) >= 2
            else None
        ),
        "drawdownFromHighPct": round((latest["close"] / highest_close - 1) * 100, 2),
        "totalShares": total_shares,
        "allocationPerShare": round(per_share, 6),
        "allocationPer100Shares": round(per_share * 100, 2),
        "rightsValuePer100Yuan": round(per_share / latest["close"] * 100, 2),
        "rightsValuePct": round(per_share / latest["close"] * 100, 2),
        "referenceConversionPrice": (
            conversion_price["referenceConversionPrice"] if conversion_price else None
        ),
        "stockToConversionPricePct": (
            conversion_price["stockToConversionPricePct"] if conversion_price else None
        ),
        "referenceConversionPriceAsOfDate": (
            conversion_price["referenceConversionPriceAsOfDate"]
            if conversion_price
            else None
        ),
        "sharesForOneLot": shares_for_one_lot,
        "industry": profile.get("industry"),
        "mainBusiness": profile.get("mainBusiness"),
        "mainBusinessReportDate": profile.get("mainBusinessReportDate"),
        "historyProvider": history["provider"],
        "historyUrl": history["url"],
        "warnings": history.get("warnings", [])
        + market.get("warnings", [])
        + ([] if conversion_price else ["参考转股价缺少20个交易日成交额或成交量"]),
    }


def refresh_pending_inventory(
    client: HttpClient,
    config: dict[str, Any],
    now: dt.datetime,
    candidates: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
    force_full_refresh: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    inventory = read_json(
        INVENTORY_PATH,
        {"schemaVersion": 1, "lastFullRefreshAt": None, "entries": {}},
    )
    settings = config.get("inventory", {})
    refresh_days = int(settings.get("fullRefreshDays", 7))
    last_refresh = inventory.get("lastFullRefreshAt")
    full_refresh = force_full_refresh or not inventory.get("entries") or not last_refresh
    if last_refresh and not force_full_refresh:
        try:
            full_refresh = (now - dt.datetime.fromisoformat(last_refresh).astimezone(now.tzinfo)).days >= refresh_days
        except ValueError:
            full_refresh = True
    raw_dir = RAW_DIR / run_id / "inventory"
    errors: list[str] = []
    if full_refresh:
        try:
            entries, _ = backfill_pending_entries(client, config, now, raw_dir)
            preserve_inventory_enrichment(entries, inventory.get("entries") or {})
            inventory["entries"] = entries
            inventory["lastFullRefreshAt"] = now.isoformat()
        except Exception as exc:
            errors.append(f"存量池历史回填失败：{exc}")
    entries = inventory.setdefault("entries", {})
    apply_inventory_events(entries, candidates)
    previous_day = previous_market_day(now.date())
    for code, entry in sorted(entries.items()):
        code_dir = raw_dir / code
        enriched_market = None
        company_profile = fetch_company_profile(client, code, code_dir / "company-profile")
        if company_profile.get("status") == "complete":
            entry["companyProfile"] = company_profile
        elif (entry.get("companyProfile") or {}).get("status") == "complete":
            cached = entry["companyProfile"]
            cached_warnings = list(cached.get("warnings") or [])
            cached_warnings.append(
                "本次行业主营刷新失败，沿用上次成功结果："
                + "；".join(company_profile.get("warnings") or company_profile.get("missingFields") or [])
            )
            company_profile = {**cached, "warnings": cached_warnings}
        else:
            errors.append(
                f"{entry['company']}行业主营补证失败："
                + ",".join(company_profile.get("missingFields") or [])
            )
        if not entry.get("registeredAmountYuan"):
            try:
                candidate = {
                    "code": code,
                    "company": entry["company"],
                    "market": entry.get("market", ""),
                    "stage": "registration",
                    "date": entry["registrationDate"],
                }
                enriched = enrich_registration_evidence(
                    client, config, candidate, now, code_dir / "enrichment"
                )
                parsed_amount = enriched.get("parsedRegisteredAmount")
                if parsed_amount:
                    entry["registeredAmountYuan"] = parsed_amount["valueYuan"]
                    entry["amountSource"] = parsed_amount
                official_shares = enriched.get("officialShareSnapshot") or {}
                if official_shares.get("value"):
                    entry["officialTotalShares"] = official_shares["value"]
                enriched_market = enriched.get("marketSnapshot")
                if enriched.get("status") != "complete" and not parsed_amount:
                    errors.append(
                        f"{entry['company']}关键数据补证不完整：{','.join(enriched.get('missingFields') or [])}"
                    )
            except Exception as exc:
                errors.append(f"{entry['company']}募集规模补证失败：{exc}")
        try:
            try:
                market = enriched_market or fetch_market_snapshot(client, code, now, code_dir)
            except Exception as market_exc:
                fallback_shares = int(entry.get("officialTotalShares") or 0)
                if not fallback_shares:
                    raise
                market = {
                    "totalShares": fallback_shares,
                    "warnings": [f"行情股本接口失败，采用申报文件股本：{market_exc}"],
                }
            history = fetch_inventory_history(
                client,
                code,
                dt.date.fromisoformat(entry["registrationDate"]) - dt.timedelta(days=10),
                previous_day,
                code_dir,
            )
            entry["lastMetrics"] = calculate_inventory_metrics(
                entry, market, history, previous_day, company_profile
            )
            entry["metricsError"] = None
        except Exception as exc:
            entry["metricsError"] = str(exc)
            errors.append(f"{entry['company']}存量指标更新失败：{exc}")
    inventory["lastUpdatedAt"] = now.isoformat()
    inventory["metricsAsOfDate"] = previous_day.isoformat()
    if not dry_run:
        atomic_write_json(INVENTORY_PATH, inventory)
    atomic_write_json(raw_dir / "inventory-snapshot.json", inventory)
    return inventory, errors


def signed_pct(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{number:+.2f}%"


def signed_pct_html(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    color = "#c62828" if number > 0 else "#188038" if number < 0 else "#5f6368"
    return f'<span style="color:{color};font-weight:600">{number:+.2f}%</span>'


def build_pending_inventory_html(
    inventory: dict[str, Any], errors: list[str]
) -> str:
    entries = list((inventory.get("entries") or {}).values())
    entries.sort(
        key=lambda item: (
            (item.get("lastMetrics") or {}).get("cumulativeChangePct", float("inf")),
            item.get("registrationDate", ""),
        )
    )
    if not entries:
        rows = '<tr><td colspan="8" style="padding:10px;text-align:center;color:#5f6368">当前没有“已同意注册、尚未正式发行”的存量股票。</td></tr>'
    else:
        rendered = []
        for entry in entries:
            metrics = entry.get("lastMetrics") or {}
            if metrics:
                industry_business = (
                    f"{escape(metrics['industry'])}<br>{escape(metrics['mainBusiness'])}"
                    if metrics.get("industry") and metrics.get("mainBusiness")
                    else "待补证"
                )
                rights = f"{metrics['rightsValuePer100Yuan']:.2f}元<br><span style=\"color:#5f6368\">({metrics['rightsValuePct']:.2f}%)</span>"
                stock_to_conversion = (
                    f"{metrics['stockToConversionPricePct']:.0f}%"
                    if metrics.get("stockToConversionPricePct") is not None
                    else "待补证"
                )
                registration = f"{escape(entry['registrationDate'])}<br><span style=\"color:#5f6368\">{metrics['tradingDaysElapsed']}个交易日<br>有效期约剩{metrics['approvalDaysRemainingEstimate']}天</span>"
                cumulative = f"{signed_pct_html(metrics['cumulativeChangePct'])}<br><span style=\"color:#5f6368\">收盘{metrics['latestClose']:.2f}元</span>"
                timing = f"近10日 {signed_pct_html(metrics['recent10ChangePct'])}<br><span style=\"color:#5f6368\">距高点 {signed_pct_html(metrics['drawdownFromHighPct'])}</span>"
                shares_for_one_lot = f"{metrics['sharesForOneLot']:,}股"
            else:
                industry_business = rights = stock_to_conversion = registration = cumulative = timing = shares_for_one_lot = "待补证"
            rendered.append(
                "<tr>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px\"><strong>{escape(entry['company'])}</strong><br><span style=\"color:#5f6368\">{escape(entry['code'])}</span></td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px\">{industry_business}</td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px\">{rights}</td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px;text-align:center\">{stock_to_conversion}</td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px\">{registration}</td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px\">{cumulative}</td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px\">{timing}</td>"
                f"<td style=\"white-space:nowrap;border:1px solid #d8dee8;padding:7px;text-align:center\">{shares_for_one_lot}</td>"
                "</tr>"
            )
        rows = "".join(rendered)
    warning = ""
    if errors:
        warning = (
            '<p style="color:#b06000;font-size:12px"><strong>存量数据提示：</strong>'
            + escape("；".join(errors))
            + "</p>"
        )
    as_of = escape(inventory.get("metricsAsOfDate") or "—")
    return f"""<section data-generated="pending-inventory" style="margin-top:24px">
<h3>已同意注册、尚未正式发行的存量候选</h3>
<p style="font-size:13px;color:#5f6368">行情截至 {as_of} 收盘；按“注册后累计涨幅”从低到高排列。</p>
<div style="width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch">
<table style="border-collapse:collapse;width:100%;min-width:900px;font-size:12px;line-height:1.45">
<thead><tr><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">股票</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">行业/<br>主营业务</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">每百元含权</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">正股现价/<br>转股价*</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">注册日/已过</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">注册后涨跌</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">短期位置</th><th style="border:1px solid #d8dee8;padding:7px;background:#f7f8fa">配售1手（10张）<br>所需股数</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p style="font-size:12px;color:#5f6368">口径：行业取公司概况的细分行业；主营业务取最新定期报告主营构成中收入占比最高的产品类别，每项最多10字。每百元含权金额＝募集规模上限÷最新总股本÷最新收盘价×100，数值与含权率百分比相同；*正式发行前尚无实际初始转股价，本列使用参考转股价下限＝max（截至上一交易日的前20日成交均价，前1日成交均价），正股现价取上一交易日收盘价，百分比保留0位小数；配售1手所需股数沿用理论值 N0＝1000÷理论每股配债额，仅四舍五入显示到个位数，不按100股整手调整。累计涨跌、近10日涨跌和高点回撤使用前复权日线；注册后涨跌以公告日（非交易日则取此前最近交易日）收盘为基准；“已过交易日”不含公告日；有效期剩余天数按公告日起365天估算。以上均为正式发行前估算，最终以发行公告实际转股价、发行规模和配售比例为准。</p>
{warning}</section>"""


def build_no_new_html(
    start: dt.datetime,
    end: dt.datetime,
    statuses: list[SourceStatus],
    inventory_html: str = "",
) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;color:#202124;line-height:1.65;max-width:820px;margin:24px auto;padding:0 18px}}
.summary{{background:#f2f7ff;border-left:4px solid #1769aa;padding:14px 16px;margin:16px 0}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d8dee8;padding:8px 10px;text-align:left}}th{{background:#f7f8fa}}.muted{{color:#5f6368;font-size:13px}}
</style></head><body>
<h2>可转债配债每日监控</h2>
<div class="summary"><strong>结论：</strong>检查区间内未发现新的“同意注册公告”或“正式发行公告”。</div>
<p><strong>检查区间：</strong>{escape(start.isoformat())} 至 {escape(end.isoformat())}（北京时间）</p>
<table><thead><tr><th>核验层</th><th>状态</th><th>检索记录数</th><th>结果</th></tr></thead><tbody>{source_rows(statuses)}</tbody></table>
<p>三层结果已按标题规则、排除词和公司/阶段匹配进行交叉核验；本次没有形成待分析证据包。后续交易日继续监控。</p>
{inventory_html}
<p class="muted">注：若任一关键官方来源访问失败，系统不会把本邮件误发为“无新增”，而会改为发送监控异常提示。</p>
</body></html>"""


def build_failure_html(
    start: dt.datetime, end: dt.datetime, statuses: list[SourceStatus], errors: list[str]
) -> str:
    items = "".join(
        f"<li>{escape(strip_terminal_escapes(error))}</li>" for error in errors
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>
<h2>可转债配债监控异常</h2>
<p><strong>结论：</strong>本次检查不具备发送“昨日无新公告”的证据完整性，已停止无新增结论并保留重试。</p>
<p><strong>检查区间：</strong>{escape(start.isoformat())} 至 {escape(end.isoformat())}（北京时间）</p>
<ul>{items}</ul>
<p>请以证据包和下次重试结果为准。</p></body></html>"""


def send_email(config: dict[str, Any], subject: str, body_file: Path) -> dict[str, Any]:
    script = (ROOT / config["email"]["sendScript"]).resolve()
    if not script.exists():
        raise RuntimeError(f"邮件发送脚本不存在：{script}")
    command = [
        "pwsh.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Subject",
        subject,
        "-BodyFile",
        str(body_file.resolve()),
        "-Html",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        detail = strip_terminal_escapes(completed.stderr).strip() or strip_terminal_escapes(
            completed.stdout
        ).strip()
        raise RuntimeError(detail)
    output = completed.stdout.strip().splitlines()[-1]
    return json.loads(output)


def subscription_reminder_subject(entry: dict[str, Any]) -> str:
    return f"【可转债申购】请今天申购新债：{entry['bondShortName']}"


def build_subscription_reminder_html(entry: dict[str, Any], slot: str) -> str:
    def display(value: Any, suffix: str = "") -> str:
        return escape(f"{value}{suffix}") if value not in (None, "") else "未披露"

    amount = entry.get("issueAmountYuan")
    amount_text = f"{float(amount) / 100000000:.2f}亿元" if amount else "未披露"
    close = entry.get("stockClose")
    close_text = f"{float(close):.2f}元" if close else "未披露"
    conversion_price = entry.get("initialConversionPrice")
    conversion_text = f"{float(conversion_price):.2f}元" if conversion_price else "未披露"
    ratio = entry.get("stockToConversionPricePct")
    ratio_text = f"{float(ratio):.0f}%" if ratio else "未披露"
    concepts = "、".join(entry.get("concepts") or []) or "暂无可靠概念数据"
    term_years = entry.get("termYears")
    term_text = f"{int(term_years)}年" if term_years else "未披露"
    source_url = clean_text(entry.get("officialUrl"))
    if not source_url.startswith(("https://", "http://")):
        source_url = ""
    source_title = clean_text(entry.get("sourceTitle")) or "正式发行文件"
    source_html = (
        f'<a href="{escape(source_url)}">{escape(source_title)}</a>'
        if source_url
        else escape(source_title)
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2937;line-height:1.55;margin:16px}}h2{{font-size:20px;margin:0 0 10px}}h3{{font-size:16px;margin:18px 0 6px}}table{{border-collapse:collapse;width:100%;max-width:720px}}th,td{{border:1px solid #d8dee8;padding:7px;text-align:left;vertical-align:top}}th{{width:34%;background:#f6f8fa}}.action{{background:#fff7d6;border-left:4px solid #e0a800;padding:10px;margin:10px 0}}.muted{{color:#5f6368;font-size:12px}}</style></head><body data-reminder-slot="{escape(slot)}">
<h2>今日新债申购提醒：{escape(entry['bondShortName'])}</h2>
<div class="action"><strong>申购代码：{display(entry.get('subscriptionCode'))}</strong><br>请在券商“新债申购”入口核对简称与代码后操作；不要把申购代码与原股东配售代码混用。</div>
<p><strong>本次提醒批次：</strong>{escape(slot)}（北京时间）</p>
<h3>新债基本信息</h3><table>
<tr><th>新债简称 / 代码</th><td>{display(entry.get('bondShortName'))} / {display(entry.get('bondCode'))}</td></tr>
<tr><th>申购简称 / 代码</th><td>{display(entry.get('subscriptionShortName'))} / <strong>{display(entry.get('subscriptionCode'))}</strong></td></tr>
<tr><th>申购日</th><td>{display(entry.get('subscriptionDate'))}</td></tr>
<tr><th>发行规模</th><td>{escape(amount_text)}</td></tr>
<tr><th>初始转股价</th><td>{escape(conversion_text)}</td></tr>
<tr><th>债项评级 / 期限</th><td>{display(entry.get('creditRating'))} / {escape(term_text)}</td></tr>
<tr><th>原股东配售</th><td>{display(entry.get('allocationShortName'))} / {display(entry.get('allocationCode'))}</td></tr>
</table>
<h3>正股与题材</h3><table>
<tr><th>正股简称 / 代码</th><td>{display(entry.get('stockName'))} / {display(entry.get('stockCode'))}</td></tr>
<tr><th>最近收盘价</th><td>{escape(close_text)}（{display(entry.get('stockCloseDate'))}）</td></tr>
<tr><th>正股现价 / 转股价</th><td>{escape(ratio_text)}</td></tr>
<tr><th>行业</th><td>{display(entry.get('industry'))}</td></tr>
<tr><th>主营业务</th><td>{display(entry.get('mainBusiness'))}</td></tr>
<tr><th>题材概念</th><td>{escape(concepts)}</td></tr>
</table>
<p><strong>正式发行依据：</strong>{source_html}</p>
<p class="muted">网上申购通常无需预缴申购资金，中签后需按券商通知及时缴款；最终规则、时间与代码以正式公告和券商申购页面为准。本邮件为信息提醒，不构成投资建议。</p>
</body></html>"""


def reminder_terms_from_evidence(
    pack: dict[str, Any], *, force_reparse: bool = False
) -> dict[str, Any]:
    calculation = pack.get("calculation") or {}
    current = dict(calculation.get("formalTerms") or {})
    if not force_reparse:
        return current
    parsed_documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in pack.get("downloads") or []:
        path = Path(str(document.get("path") or ""))
        if not document.get("pdfValidated") or not path.exists():
            continue
        digest = str(document.get("sha256") or path)
        if digest in seen:
            continue
        seen.add(digest)
        try:
            parsed_documents.append(
                {
                    "terms": parse_formal_terms(pdf_text(path)),
                    "source": {
                        key: document.get(key)
                        for key in ("layer", "title", "date", "url", "sha256")
                        if document.get(key) is not None
                    },
                }
            )
        except Exception:
            continue
    if parsed_documents:
        return merge_formal_term_sets(parsed_documents)
    return {}


def build_subscription_reminder_entry(
    pack: dict[str, Any],
    now: dt.datetime,
    concept_client: HttpClient | None = None,
    evidence_path: Path | None = None,
    *,
    force_reparse: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    candidate = pack.get("candidate") or {}
    if candidate.get("stage") != "formal":
        return None, []
    calculation = pack.get("calculation") or {}
    terms = reminder_terms_from_evidence(pack, force_reparse=force_reparse)
    validation_errors: list[str] = []
    if calculation.get("status") != "complete":
        validation_errors.append("formalCalculationIncomplete")
    if force_reparse and not terms:
        validation_errors.append("formalTermsReparse")
    if terms.get("conflicts"):
        validation_errors.append("formalTermConflict")
    if (terms.get("consistency") or {}).get("status") == "conflict":
        validation_errors.append("allocationConsistency")
    try:
        subscription_date = dt.date.fromisoformat(str(terms.get("subscriptionDate")))
    except (TypeError, ValueError):
        subscription_date = None
    if subscription_date and subscription_date < now.date():
        return None, []
    market = calculation.get("marketSnapshot") or {}
    profile = calculation.get("companyProfile") or pack.get("companyProfile") or {}
    concept_info = calculation.get("companyConcepts") or {}
    source = calculation.get("source") or candidate.get("primary") or candidate.get("cninfo") or {}
    stock_name = market.get("securityName") or candidate.get("company")
    required_values = {
        "stockCode": candidate.get("code"),
        "stockName": stock_name,
        "subscriptionDate": terms.get("subscriptionDate"),
        "bondShortName": terms.get("bondShortName"),
        "bondCode": terms.get("bondCode"),
        "subscriptionCode": terms.get("subscriptionCode"),
    }
    missing = [field for field, value in required_values.items() if not value]
    missing.extend(validation_errors)
    if missing:
        return None, sorted(set(missing))
    if not concept_info and concept_client and candidate.get("code") and evidence_path:
        try:
            concept_info = fetch_company_concepts(
                concept_client,
                str(candidate["code"]),
                evidence_path.parent / "company-concepts",
            )
        except Exception:
            concept_info = {}
    subscription_date = dt.date.fromisoformat(str(terms["subscriptionDate"]))
    results = calculation.get("results") or {}
    amount = (terms.get("finalAmount") or {}).get("valueYuan")
    entry_seed = "|".join(
        [
            str(candidate["code"]),
            subscription_date.isoformat(),
            str(terms["subscriptionCode"]),
        ]
    )
    entry_key = hashlib.sha256(entry_seed.encode("utf-8")).hexdigest()[:24]
    entry = {
        "reminderKey": entry_key,
        "eventKey": candidate.get("eventKey"),
        "announcementDate": candidate.get("date"),
        "stockCode": str(candidate["code"]),
        "stockName": stock_name,
        "bondShortName": terms.get("bondShortName"),
        "bondCode": terms.get("bondCode"),
        "subscriptionShortName": terms.get("subscriptionShortName"),
        "subscriptionCode": terms.get("subscriptionCode"),
        "allocationShortName": terms.get("allocationShortName"),
        "allocationCode": terms.get("allocationCode"),
        "subscriptionDate": subscription_date.isoformat(),
        "recordDate": terms.get("recordDate"),
        "issueAmountYuan": amount,
        "initialConversionPrice": terms.get("initialConversionPrice"),
        "creditRating": terms.get("creditRating"),
        "termYears": terms.get("termYears"),
        "stockClose": market.get("latestClose"),
        "stockCloseDate": market.get("latestCloseDate"),
        "stockToConversionPricePct": results.get("stockToConversionPricePct"),
        "conversionValue": results.get("conversionValue"),
        "industry": profile.get("industry"),
        "mainBusiness": profile.get("mainBusiness"),
        "concepts": list(concept_info.get("concepts") or []),
        "conceptProvider": concept_info.get("provider"),
        "officialUrl": source.get("url"),
        "sourceTitle": source.get("title"),
        "status": "active",
        "updatedAt": now.isoformat(),
        "sentSlots": {},
    }
    return entry, []


def subscription_reminder_due_slots(
    entry: dict[str, Any], now: dt.datetime
) -> list[str]:
    if entry.get("status") == "superseded" or entry.get("supersededBy"):
        return []
    try:
        subscription_date = dt.date.fromisoformat(str(entry.get("subscriptionDate")))
    except (TypeError, ValueError):
        return []
    local_time = now.timetz().replace(tzinfo=None)
    if subscription_date != now.date() or local_time >= SUBSCRIPTION_REMINDER_CUTOFF:
        return []
    sent = entry.get("sentSlots") or {}
    due = []
    for slot in SUBSCRIPTION_REMINDER_SLOTS:
        slot_time = dt.time.fromisoformat(slot)
        if local_time >= slot_time and slot not in sent:
            due.append(slot)
    return due


def refresh_subscription_reminder_queue(
    queue: dict[str, Any],
    evidence_paths: Iterable[Path],
    now: dt.datetime,
    concept_client: HttpClient | None = None,
    *,
    force_reparse: bool = False,
) -> dict[str, Any]:
    entries = queue.setdefault("entries", {})
    added_or_updated = 0
    incomplete: list[dict[str, Any]] = []
    for evidence_path in evidence_paths:
        try:
            pack = read_json(evidence_path, {})
            entry, missing = build_subscription_reminder_entry(
                pack,
                now,
                concept_client,
                evidence_path,
                force_reparse=force_reparse,
            )
        except Exception as exc:
            incomplete.append({"evidence": str(evidence_path), "error": str(exc)})
            continue
        if missing:
            incomplete.append(
                {"evidence": str(evidence_path), "missingFields": missing}
            )
        if not entry:
            continue
        existing = entries.get(entry["reminderKey"]) or {}
        for field, value in existing.items():
            if field in {
                "sentSlots",
                "status",
                "supersededBy",
                "supersededAt",
                "updatedAt",
            }:
                continue
            if entry.get(field) in (None, "", []) and value not in (None, "", []):
                entry[field] = value
        entry["sentSlots"] = dict(existing.get("sentSlots") or {})
        for old_key, old_entry in list(entries.items()):
            if old_key == entry["reminderKey"]:
                continue
            same_event = bool(
                entry.get("eventKey")
                and old_entry.get("eventKey") == entry.get("eventKey")
            )
            same_bond = bool(
                entry.get("stockCode")
                and entry.get("bondCode")
                and old_entry.get("stockCode") == entry.get("stockCode")
                and old_entry.get("bondCode") == entry.get("bondCode")
            )
            if same_event or same_bond:
                # A corrected event changes the reminder key.  Keep optional,
                # previously verified enrichment when a rebuild cannot refresh
                # it, while never carrying date/code identity fields forward.
                for field in ("concepts", "conceptProvider"):
                    if entry.get(field) in (None, "", []) and old_entry.get(field) not in (
                        None,
                        "",
                        [],
                    ):
                        entry[field] = old_entry[field]
                old_entry["status"] = "superseded"
                old_entry["supersededBy"] = entry["reminderKey"]
                old_entry["supersededAt"] = now.isoformat()
        entries[entry["reminderKey"]] = entry
        added_or_updated += 1
    expiry = now.date() - dt.timedelta(days=30)
    for key, entry in list(entries.items()):
        try:
            if dt.date.fromisoformat(str(entry.get("subscriptionDate"))) < expiry:
                del entries[key]
        except (TypeError, ValueError):
            del entries[key]
    queue.update(
        {
            "schemaVersion": 1,
            "updatedAt": now.isoformat(),
        }
    )
    return {
        "addedOrUpdated": added_or_updated,
        "incomplete": incomplete,
        "queueCount": len(entries),
    }


def discover_subscription_evidence(
    evidence_dir: Path = EVIDENCE_DIR,
    as_of: dt.date | None = None,
    lookback_days: int = 30,
) -> list[Path]:
    paths = sorted(evidence_dir.glob("*/*/*/evidence.json"))
    if as_of is None:
        return paths
    cutoff = as_of - dt.timedelta(days=lookback_days)
    selected: list[Path] = []
    for path in paths:
        try:
            event_day = dt.date.fromisoformat(path.relative_to(evidence_dir).parts[0])
        except (ValueError, IndexError):
            selected.append(path)
            continue
        if event_day >= cutoff:
            selected.append(path)
    return selected


def queue_subscription_evidence_paths(
    evidence_paths: Iterable[Path],
    now: dt.datetime,
    *,
    queue_path: Path = SUBSCRIPTION_REMINDER_PATH,
    lock_path: Path = SUBSCRIPTION_REMINDER_LOCK_PATH,
    dry_run: bool = False,
    force_reparse: bool = False,
) -> dict[str, Any]:
    with subscription_reminder_lock(lock_path):
        queue = read_json(queue_path, {"schemaVersion": 1, "entries": {}})
        result = refresh_subscription_reminder_queue(
            queue, evidence_paths, now, force_reparse=force_reparse
        )
        if not dry_run:
            atomic_write_json(queue_path, queue)
        return result


def run_subscription_reminders(
    config: dict[str, Any],
    now: dt.datetime,
    *,
    queue_path: Path = SUBSCRIPTION_REMINDER_PATH,
    lock_path: Path = SUBSCRIPTION_REMINDER_LOCK_PATH,
    evidence_dir: Path = EVIDENCE_DIR,
    reports_dir: Path = REPORTS_DIR,
    send_func: Any = send_email,
    dry_run: bool = False,
    send_due: bool = True,
    concept_client: HttpClient | None = None,
    refresh_evidence: bool = False,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    with subscription_reminder_lock(lock_path):
        queue = read_json(queue_path, {"schemaVersion": 1, "entries": {}})
        if refresh_evidence:
            refresh = refresh_subscription_reminder_queue(
                queue,
                discover_subscription_evidence(evidence_dir, now.date()),
                now,
                concept_client,
                force_reparse=True,
            )
            if not dry_run:
                atomic_write_json(queue_path, queue)
        else:
            refresh = {
                "skipped": True,
                "reason": "queue_only",
                "queueCount": len(queue.get("entries") or {}),
            }
        if send_due:
            for entry in sorted(
                (queue.get("entries") or {}).values(),
                key=lambda item: (item.get("subscriptionDate", ""), item.get("stockCode", "")),
            ):
                for slot in subscription_reminder_due_slots(entry, now):
                    subject = subscription_reminder_subject(entry)
                    report = (
                        reports_dir
                        / str(entry["subscriptionDate"])
                        / f"subscription-reminder-{entry['reminderKey']}-{slot.replace(':', '')}.html"
                    )
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text(
                        build_subscription_reminder_html(entry, slot), encoding="utf-8"
                    )
                    action = {
                        "reminderKey": entry["reminderKey"],
                        "slot": slot,
                        "subject": subject,
                        "report": str(report.resolve()),
                    }
                    if dry_run:
                        action["status"] = "would_send"
                        actions.append(action)
                        continue
                    try:
                        delivery = send_func(config, subject, report)
                        entry.setdefault("sentSlots", {})[slot] = {
                            "sentAt": now.isoformat(),
                            "duplicateAccepted": bool(delivery.get("duplicate")),
                        }
                        atomic_write_json(queue_path, queue)
                        action["status"] = "sent"
                        action["duplicateAccepted"] = bool(delivery.get("duplicate"))
                        actions.append(action)
                    except Exception as exc:
                        action["status"] = "failed"
                        action["error"] = strip_terminal_escapes(exc)
                        actions.append(action)
                        errors.append(
                            f"{entry.get('stockCode')} {slot} 申购提醒发送失败：{strip_terminal_escapes(exc)}"
                        )
    return {
        "ok": not errors,
        "runAt": now.isoformat(),
        "sendDue": send_due,
        "dryRun": dry_run,
        "refresh": refresh,
        "actions": actions,
        "errors": errors,
    }


def invoke_event_runner(config: dict[str, Any], evidence_path: Path) -> dict[str, Any]:
    runner = (ROOT / config["eventRunner"]).resolve()
    command = [
        shutil.which("pwsh.exe") or "pwsh.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(runner),
        "-EvidencePath",
        str(evidence_path.resolve()),
    ]
    # PowerShell 7 emits UTF-8. Keep the child shell and Python decoder aligned;
    # Windows PowerShell 5.1 would emit the local code page and corrupt Chinese
    # diagnostics/model output when decoded as UTF-8.
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=2700,
    )
    stdout = strip_terminal_escapes(completed.stdout)
    stderr = strip_terminal_escapes(completed.stderr)
    return {
        "ok": completed.returncode == 0,
        "returnCode": completed.returncode,
        "stdout": stdout[-12000:],
        "stderr": stderr[-12000:],
    }


def save_raw(run_id: str, name: str, value: Any) -> Path:
    path = RAW_DIR / run_id / f"{name}.json"
    atomic_write_json(path, value)
    return path


def determine_window(
    now: dt.datetime, state: dict[str, Any], explicit_start: str | None
) -> dt.datetime:
    timezone = now.tzinfo
    if explicit_start:
        parsed = dt.datetime.fromisoformat(explicit_start)
        return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)
    cutoff = state.get("lastSuccessfulCutoff")
    if cutoff:
        return dt.datetime.fromisoformat(cutoff).astimezone(timezone)
    previous = now.date() - dt.timedelta(days=1)
    return dt.datetime.combine(previous, dt.time.min, timezone)


def validate_core_sources(statuses: list[SourceStatus]) -> list[str]:
    required = {"sse", "szse", "cninfo", "csrc"}
    by_source = {status.source: status for status in statuses}
    errors = []
    for source in required:
        status = by_source.get(source)
        if status is None or not status.ok:
            errors.append(f"{source} 官方来源检索失败：{status.detail if status else '未执行'}")
    return errors


def main() -> int:
    # Scheduled tasks redirect stdout/stderr.  Force UTF-8 so structured results
    # containing non-GBK characters cannot turn an otherwise successful run into
    # a console-encoding failure after state and email have already completed.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="可转债配债混合监控（纯脚本日检）")
    parser.add_argument("--dry-run", action="store_true", help="不发邮件、不改状态、不调用模型")
    parser.add_argument("--no-model", action="store_true", help="发现事件只保存证据包")
    parser.add_argument("--as-of", help="模拟北京时间，ISO格式")
    parser.add_argument("--window-start", help="显式检查起点，ISO格式")
    parser.add_argument("--skip-calendar-online-check", action="store_true")
    reminder_modes = parser.add_mutually_exclusive_group()
    reminder_modes.add_argument(
        "--subscription-reminders",
        action="store_true",
        help="仅处理申购日08:50/09:50提醒，不执行公告日检",
    )
    reminder_modes.add_argument(
        "--rebuild-subscription-reminders",
        action="store_true",
        help="从既有正式发行证据重建未来申购提醒队列，不发送邮件",
    )
    parser.add_argument(
        "--replay-reported",
        action="store_true",
        help="仅用于dry-run回放已报告事件",
    )
    args = parser.parse_args()
    if args.replay_reported and not args.dry_run:
        parser.error("--replay-reported 只能与 --dry-run 一起使用")

    config = read_json(CONFIG_PATH, {})
    timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    if args.as_of:
        now = dt.datetime.fromisoformat(args.as_of)
        now = now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    else:
        now = dt.datetime.now(timezone)
    client = HttpClient(
        int(config.get("sourceTimeoutSeconds", 35)),
        int(config.get("sourceRetries", 3)),
    )
    if args.subscription_reminders or args.rebuild_subscription_reminders:
        result = run_subscription_reminders(
            config,
            now,
            dry_run=args.dry_run,
            send_due=args.subscription_reminders,
            concept_client=client if args.rebuild_subscription_reminders else None,
            refresh_evidence=args.rebuild_subscription_reminders,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 4
    run_id = now.strftime("%Y%m%dT%H%M%S")
    state = read_json(STATE_PATH, {"schemaVersion": 1, "reportedEventKeys": []})
    start = determine_window(now, state if not args.dry_run else {}, args.window_start)
    sources = config["officialSources"]
    statuses: list[SourceStatus] = []
    errors: list[str] = []
    raw_index: dict[str, Any] = {}

    try:
        is_trading_day, calendar_statuses, calendar_raw = check_trading_day(
            client, sources, now.date()
        )
        statuses.extend(calendar_statuses)
        raw_index["calendar"] = calendar_raw
        if not args.skip_calendar_online_check:
            failed_calendars = [status for status in calendar_statuses if not status.ok]
            if failed_calendars:
                errors.extend(
                    f"{status.source}：{status.detail}" for status in failed_calendars
                )
        if not is_trading_day:
            result = {
                "ok": True,
                "tradingDay": False,
                "message": "今日非交易日，本次不生成日报",
                "date": now.date().isoformat(),
                "calendarSources": [asdict(status) for status in calendar_statuses],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        errors.append(f"交易日判断失败：{exc}")

    records: list[Announcement] = []
    fetchers = (
        ("sse", lambda: fetch_sse(client, sources["sseAnnouncements"], start, now, int(config["maxPages"]))),
        ("szse", lambda: fetch_szse(client, sources["szseAnnouncements"], start, now, int(config["maxPages"]))),
        ("cninfo", lambda: fetch_cninfo(client, sources["cninfoAnnouncements"], start, now, int(config["maxPages"]))),
        ("csrc", lambda: fetch_csrc_search(client, sources["csrcSearch"], "同意注册 可转换公司债券")),
    )
    for source_name, fetcher in fetchers:
        try:
            source_records, raw = fetcher()
            if source_name == "csrc":
                source_records = [
                    record for record in source_records if date_in_window(record.date, start, now)
                ]
            records.extend(source_records)
            raw_index[source_name] = raw
            statuses.append(
                SourceStatus(
                    source=source_name,
                    ok=True,
                    count=len(source_records),
                    detail=(
                        f"检索成功；目标候选 {sum(r.stage != 'none' for r in source_records)} 条"
                        + (cninfo_transport_detail(raw) if source_name == "cninfo" else "")
                    ),
                    url=sources[
                        "sseAnnouncements"
                        if source_name == "sse"
                        else "szseAnnouncements"
                        if source_name == "szse"
                        else "cninfoAnnouncements"
                        if source_name == "cninfo"
                        else "csrcSearch"
                    ],
                )
            )
        except Exception as exc:
            statuses.append(
                SourceStatus(source=source_name, ok=False, detail=str(exc))
            )
            raw_index[source_name] = {"error": str(exc)}

    # 对交易所/巨潮命中的公司再做一次证监会站内精确搜索。这样即使注册批复
    # 早于本次日检窗口，正式发行事件仍能附上第三层注册证据。
    csrc_status = next((status for status in statuses if status.source == "csrc"), None)
    preliminary = [
        record
        for record in records
        if record.source != "csrc" and record.stage != "none"
    ]
    exact_raw: dict[str, Any] = {}
    if csrc_status and csrc_status.ok and preliminary:
        seen_companies: set[str] = set()
        exact_matches = 0
        for record in preliminary:
            company = record.company or company_from_title(record.title)
            lookup_key = normalized_title(company)
            if not company or lookup_key in seen_companies:
                continue
            seen_companies.add(lookup_key)
            try:
                exact_records, exact_page = fetch_csrc_search(
                    client, sources["csrcSearch"], f"{company} 可转换公司债券"
                )
                matches = [
                    item
                    for item in exact_records
                    if item.stage == "registration" and company_matches(item, company)
                ]
                records.extend(matches)
                exact_matches += len(matches)
                exact_raw[company] = {
                    "matches": [asdict(item) for item in matches],
                    "pageSha256": hashlib.sha256(exact_page.encode("utf-8")).hexdigest(),
                }
            except Exception as exc:
                exact_raw[company] = {"error": str(exc)}
        csrc_status.detail += f"；候选公司精确复核命中 {exact_matches} 条"
    raw_index["csrc_exact"] = exact_raw

    raw_path = save_raw(run_id, "source_responses", raw_index)
    audit_path = save_raw(
        run_id,
        "classification_audit",
        [
            {
                "source": record.source,
                "code": record.code,
                "company": record.company,
                "date": record.date,
                "title": record.title,
                "stage": record.stage,
                "reason": record.reason,
                "url": record.url,
            }
            for record in records
        ],
    )
    errors.extend(validate_core_sources(statuses))
    reports_day_dir = REPORTS_DIR / now.date().isoformat()
    reports_day_dir.mkdir(parents=True, exist_ok=True)

    if errors:
        report = reports_day_dir / f"monitor-failure-{run_id}.html"
        report.write_text(build_failure_html(start, now, statuses, errors), encoding="utf-8")
        send_result = None
        if not args.dry_run:
            subject = config["email"]["failureSubjectPrefix"] + now.date().isoformat()
            send_result = send_email(config, subject, report)
        result = {
            "ok": False,
            "tradingDay": True,
            "errors": errors,
            "report": str(report.resolve()),
            "email": send_result,
            "raw": str(raw_path.resolve()),
            "audit": str(audit_path.resolve()),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    candidates = group_candidates(records)
    reported = set(state.get("reportedEventKeys", []))
    new_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        event_seed = "|".join(
            [candidate["groupKey"], candidate["stage"], candidate["date"]]
        )
        event_key = hashlib.sha256(event_seed.encode("utf-8")).hexdigest()[:24]
        candidate["eventKey"] = event_key
        if event_key not in reported or args.replay_reported:
            new_candidates.append(candidate)

    inventory_errors: list[str] = []
    try:
        inventory, inventory_errors = refresh_pending_inventory(
            client, config, now, candidates, run_id, args.dry_run
        )
        inventory_html = build_pending_inventory_html(inventory, inventory_errors)
    except Exception as exc:
        inventory = {
            "schemaVersion": 1,
            "entries": {},
            "metricsAsOfDate": None,
        }
        inventory_errors = [f"存量候选池生成失败：{exc}"]
        inventory_html = build_pending_inventory_html(inventory, inventory_errors)

    evidence_paths: list[Path] = []
    model_results: list[dict[str, Any]] = []
    reminder_queue_result: dict[str, Any] | None = None
    if new_candidates:
        run_dir = EVIDENCE_DIR / now.date().isoformat() / run_id
        for candidate in new_candidates:
            evidence_path = build_evidence_pack(
                client, config, candidate, statuses, start, now, run_dir
            )
            evidence_paths.append(evidence_path)
        reminder_queue_result = queue_subscription_evidence_paths(
            evidence_paths, now, dry_run=args.dry_run
        )
        reminder_delivery_result = None
        if (
            not args.dry_run
            and int(reminder_queue_result.get("addedOrUpdated") or 0) > 0
        ):
            # The monitor and reminder tasks share an AtLogOn trigger.  If the
            # reminder task checks an empty queue before a delayed monitor run
            # discovers today's formal notice, there may be no later trigger.
            # Dispatch due slots immediately after the durable queue write.
            reminder_delivery_result = run_subscription_reminders(config, now)
        complete_events, incomplete_events = partition_event_evidence(
            new_candidates, evidence_paths
        )
        manifest_path = run_dir / "event-batch.json"
        atomic_write_json(
            manifest_path,
            {
                "schemaVersion": 1,
                "createdAt": now.isoformat(),
                "eventCount": len(complete_events),
                "allEventCount": len(new_candidates),
                "evidencePaths": [str(path.resolve()) for _, path in complete_events],
                "allEvidencePaths": [str(path.resolve()) for path in evidence_paths],
                "incompleteEvents": incomplete_events,
                "inventoryHtml": inventory_html,
                "subscriptionReminderQueue": reminder_queue_result,
                "subscriptionReminderDelivery": reminder_delivery_result,
                "rule": "一天只调用一次Codex；仅完整证据包进入分析，失败事件隔离重试",
            },
        )
        if reminder_delivery_result and not reminder_delivery_result.get("ok"):
            model_results.append(
                {
                    "ok": False,
                    "kind": "subscription_reminder_delivery",
                    "stderr": "申购提醒即时补发失败："
                    + "；".join(reminder_delivery_result.get("errors") or ["未知错误"]),
                    "eventKeys": [item["eventKey"] for item in new_candidates],
                    "manifestPath": str(manifest_path.resolve()),
                }
            )
        if complete_events and not args.dry_run and not args.no_model:
            model_result = invoke_event_runner(config, manifest_path)
            model_result["eventKeys"] = [item["eventKey"] for item, _ in complete_events]
            model_result["manifestPath"] = str(manifest_path.resolve())
            model_results.append(model_result)
            if model_result["ok"] and (
                not reminder_delivery_result or reminder_delivery_result.get("ok")
            ):
                reported.update(item["eventKey"] for item, _ in complete_events)
        if incomplete_events:
            model_results.append(
                {
                    "ok": False,
                    "kind": "evidence_preflight",
                    "stderr": "；".join(
                        f"{item['candidate'].get('code')}关键计算数据不完整："
                        + ", ".join(item["missingFields"])
                        for item in incomplete_events
                    ),
                    "eventKeys": [
                        item["candidate"]["eventKey"] for item in incomplete_events
                    ],
                    "manifestPath": str(manifest_path.resolve()),
                }
            )
        failure_email = None
        model_ok = all(item.get("ok") for item in model_results) if model_results else True
        if not args.dry_run and not args.no_model and not model_ok:
            event_errors = [
                "已发现目标公告并保存证据包，但事件日 Codex、事件邮件或申购提醒发送失败。",
                *[
                    (item.get("stderr") or item.get("stdout") or "未知事件分析错误")[-3000:]
                    for item in model_results
                    if not item.get("ok")
                ],
            ]
            failure_report = reports_day_dir / f"event-failure-{run_id}.html"
            failure_report.write_text(
                build_failure_html(start, now, statuses, event_errors), encoding="utf-8"
            )
            failure_email = send_email(
                config,
                config["email"]["failureSubjectPrefix"] + now.date().isoformat(),
                failure_report,
            )
        if not args.dry_run:
            state["reportedEventKeys"] = sorted(reported)
            if model_ok and not args.no_model:
                state["lastSuccessfulCutoff"] = now.isoformat()
            state["lastRunAt"] = now.isoformat()
            state["lastRunResult"] = (
                "event" if model_results and model_ok else
                "event_failed" if model_results else
                "event_evidence_only"
            )
            atomic_write_json(STATE_PATH, state)
        result = {
            "ok": model_ok,
            "tradingDay": True,
            "newEvents": len(new_candidates),
            "candidates": new_candidates,
            "evidencePaths": [str(path.resolve()) for path in evidence_paths],
            "eventManifest": str(manifest_path.resolve()),
            "modelResults": model_results,
            "completeEventCount": len(complete_events),
            "incompleteEvents": incomplete_events,
            "failureEmail": failure_email,
            "inventoryCount": len(inventory.get("entries") or {}),
            "inventoryErrors": inventory_errors,
            "subscriptionReminderQueue": reminder_queue_result,
            "subscriptionReminderDelivery": reminder_delivery_result,
            "raw": str(raw_path.resolve()),
            "audit": str(audit_path.resolve()),
            "dryRun": args.dry_run,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 3

    report = reports_day_dir / f"no-new-{run_id}.html"
    report.write_text(
        build_no_new_html(start, now, statuses, inventory_html), encoding="utf-8"
    )
    send_result = None
    if not args.dry_run:
        send_result = send_email(config, config["email"]["noNewSubject"], report)
        state["lastSuccessfulCutoff"] = now.isoformat()
        state["lastRunAt"] = now.isoformat()
        state["lastRunResult"] = "no_new"
        state["reportedEventKeys"] = sorted(reported)
        atomic_write_json(STATE_PATH, state)
    result = {
        "ok": True,
        "tradingDay": True,
        "newEvents": 0,
        "checkedRecords": len(records),
        "candidateRecords": 0,
        "inventoryCount": len(inventory.get("entries") or {}),
        "inventoryErrors": inventory_errors,
        "report": str(report.resolve()),
        "email": send_result,
        "sourceStatuses": [asdict(status) for status in statuses],
        "raw": str(raw_path.resolve()),
        "audit": str(audit_path.resolve()),
        "dryRun": args.dry_run,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
