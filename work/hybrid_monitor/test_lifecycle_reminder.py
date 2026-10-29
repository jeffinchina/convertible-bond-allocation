import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from monitor import Announcement
from lifecycle_reminder import (
    _fetch_complete_window,
    _master_entry,
    build_lifecycle_entry,
    build_lifecycle_html,
    classify_lifecycle_title,
    fetch_szse_lifecycle,
    is_official_exchange_url,
    lifecycle_document_matches,
    lifecycle_subject,
    lifecycle_source_key,
    parse_lifecycle_terms,
    reminder_due,
    run_lifecycle_reminders,
    upsert_entries,
)


TZ = ZoneInfo("Asia/Shanghai")
SCRIPT_DIR = Path(__file__).parent
RUNNER_PATH = SCRIPT_DIR / "Run-BondLifecycleReminder.ps1"


def config() -> dict:
    return {
        "timezone": "Asia/Shanghai",
        "sourceTimeoutSeconds": 1,
        "sourceRetries": 1,
        "maxPages": 1,
        "officialSources": {
            "sseAnnouncements": "https://query.sse.com.cn/official",
            "szseAnnouncements": "https://www.szse.cn/official",
        },
    }


def announcement(
    *,
    source: str = "sse",
    kind: str = "draw",
    code: str = "600001",
    company: str = "测试股份",
    date: str = "2026-10-29",
) -> Announcement:
    if kind == "draw":
        title = "测试股份向不特定对象发行可转换公司债券网上中签结果公告"
    else:
        title = "测试股份向不特定对象发行可转换公司债券上市公告书"
    url = (
        "https://www.sse.com.cn/disclosure/test.pdf"
        if source == "sse"
        else "https://disc.static.szse.cn/download/test.pdf"
    )
    return Announcement(
        source=source,
        market="SSE" if source == "sse" else "SZSE",
        code=code,
        company=company,
        title=title,
        date=date,
        url=url,
        source_id=f"{source}-{kind}-{code}-{date}",
    )


def draw_pdf_text() -> str:
    return (
        "测试股份向不特定对象发行可转换公司债券网上中签结果公告。"
        "可转换公司债券简称：测试转债，债券代码：110999。"
        "缴款日：2026年10月29日。每个中签号码只能认购1手，合计1,000元。"
        "中签投资者应确保资金账户在缴款日有足额认购资金。"
    )


def listing_pdf_text(listing_date: str = "2026年10月29日") -> str:
    return (
        "测试股份向不特定对象发行可转换公司债券上市公告书。"
        "可转换公司债券简称：测试转债，债券代码：110999。"
        f"上市日期：{listing_date}。本次债券将在上海证券交易所上市交易。"
        "本次发行的可转换公司债券期限为六年。"
    )


def queue_entry(kind: str = "draw", code: str = "110999") -> dict:
    target = "2026-10-29"
    return {
        "reminderKey": f"{kind}-{code}",
        "kind": kind,
        "sourceKey": f"source-{kind}-{code}",
        "stockCode": "600001",
        "stockName": "测试股份",
        "bondShortName": "测试转债" if code == "110999" else "样例转债",
        "bondCode": code,
        "targetDate": target,
        "announcementDate": "2026-10-29" if kind == "draw" else "2026-10-25",
        "paymentDate": "2026-10-29" if kind == "draw" else None,
        "listingDate": target if kind == "listing" else None,
        "lotCount": 1,
        "lotAmountYuan": 1000,
        "issueAmountYuan": 500_000_000,
        "initialConversionPrice": 10.5,
        "creditRating": "AA",
        "termYears": 6,
        "stockClose": 11.2,
        "stockCloseDate": "2026-10-28",
        "conversionValue": 106.67,
        "industry": "专用设备",
        "mainBusiness": "测试设备",
        "concepts": ["机器人", "新能源"],
        "officialSource": "sse",
        "officialTitle": "测试股份可转换公司债券公告",
        "officialUrl": "https://www.sse.com.cn/disclosure/test.pdf",
        "officialSha256": "a" * 64,
        "status": "active",
    }


class LifecycleReminderTests(unittest.TestCase):
    def test_szse_effective_50_row_pages_continue_to_declared_total(self):
        class FakeClient:
            def __init__(self):
                self.pages = []

            def post_json(self, _url, request, **_kwargs):
                self.pages.append(request)
                self_test.assertEqual(request["pageSize"], 50)
                first = (request["pageNum"] - 1) * 50
                rows = [
                    {
                        "id": str(index), "secCode": ["002550"], "secName": ["千红制药"],
                        "title": f"千红制药可转换公司债券公告{index}",
                        "publishTime": "2026-10-08 00:00:00",
                        "attachPath": f"/disc/test-{index}.pdf",
                    }
                    for index in range(first, min(first + 50, 60))
                ]
                return {"announceCount": 60, "data": rows}

        self_test = self
        client = FakeClient()
        now = dt.datetime(2026, 10, 8, 9, 15, tzinfo=TZ)
        records, pages = fetch_szse_lifecycle(client, "https://www.szse.cn/api", now, now, 8)
        self.assertEqual(len(records), 60)
        self.assertEqual(len(pages), 2)
        self.assertEqual([request["pageNum"] for request in client.pages], [1, 2])
        self.assertEqual(records[-1].date, "2026-10-08")
        self.assertEqual(records[-1].code, "002550")
        self.assertTrue(records[-1].url.startswith("https://disc.static.szse.cn/download/"))

    def test_full_window_splits_repeated_pages_and_collects_every_day(self):
        calls = []

        def fetcher(_client, _endpoint, start, end, _max_pages):
            calls.append((start.date(), end.date()))
            if start.date() < end.date():
                record = announcement(date=end.date().isoformat())
                return [record, record], [{"pageHelp": {"total": 2}}]
            return [announcement(date=start.date().isoformat())], [{"pageHelp": {"total": 1}}]

        records, audit = _fetch_complete_window(
            fetcher, object(), "official", dt.datetime(2026, 10, 27, tzinfo=TZ),
            dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ), 8,
        )
        self.assertEqual({item.date for item in records}, {"2026-10-27", "2026-10-28", "2026-10-29"})
        self.assertTrue(audit[0]["split"])
        self.assertEqual(len(calls), 5)

    def test_incomplete_single_day_is_not_mistaken_for_complete(self):
        record = announcement()
        with self.assertRaisesRegex(RuntimeError, "分页不完整"):
            _fetch_complete_window(
                lambda *_: ([record], [{"pageHelp": {"total": 2}}]),
                object(), "official", dt.datetime(2026, 10, 29, tzinfo=TZ),
                dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ), 8,
            )

    def test_source_key_includes_url_code_and_date_despite_sse_category_id(self):
        first = announcement()
        second = announcement(date="2026-10-30", code="600002")
        first.source_id = second.source_id = "可转债"
        self.assertNotEqual(lifecycle_source_key(first), lifecycle_source_key(second))
        second = announcement()
        second.url = "https://www.sse.com.cn/disclosure/correction.pdf"
        second.source_id = first.source_id
        self.assertNotEqual(lifecycle_source_key(first), lifecycle_source_key(second))

    def test_master_data_requires_exact_bond_identity_and_not_future_issuance(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            path = Path(temp_dir) / "master.json"
            old = queue_entry()
            old.update({"bondShortName": "旧债转债", "bondCode": "110001", "subscriptionDate": "2026-10-01"})
            newer = dict(old)
            newer.update({"bondShortName": "新债转债", "bondCode": "110002", "subscriptionDate": "2026-11-01"})
            path.write_text(json.dumps({"entries": {"old": old, "new": newer}}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(_master_entry("600001", None, "旧债转债", "2026-10-29", path)["bondCode"], "110001")
            self.assertEqual(_master_entry("600001", None, "不明转债", "2026-10-29", path), {})
            self.assertEqual(_master_entry("600001", "110002", "新债转债", "2026-10-29", path), {})

    def test_newest_first_correction_cannot_be_reverted_by_old_notice(self):
        old = queue_entry("listing")
        old.update({"reminderKey": "old", "announcementDate": "2026-10-25", "targetDate": "2026-10-29"})
        newer = dict(old)
        newer.update({"reminderKey": "new", "announcementDate": "2026-10-26", "targetDate": "2026-10-30"})
        queue = {"entries": {}}
        now = dt.datetime(2026, 10, 27, 9, 15, tzinfo=TZ)
        upsert_entries(queue, [newer, old], now)
        self.assertEqual(queue["entries"]["old"]["status"], "superseded")
        self.assertEqual(queue["entries"]["new"]["status"], "active")
        replay = dict(old)
        replay.update({"status": "active"})
        replay.pop("supersededBy", None)
        upsert_entries(queue, [replay], now)
        self.assertEqual(queue["entries"]["old"]["status"], "superseded")

    def test_enriching_missing_code_preserves_already_sent_delivery(self):
        old = queue_entry()
        old.update({"reminderKey": "old", "bondCode": None, "sentAt": "2026-10-29T09:15:00+08:00"})
        new = dict(old)
        new.update({"reminderKey": "new", "bondCode": "110999"})
        new.pop("sentAt")
        queue = {"entries": {"old": old}}
        upsert_entries(queue, [new], dt.datetime(2026, 10, 29, 10, 0, tzinfo=TZ))
        self.assertTrue(queue["entries"]["new"]["sentAt"])
        self.assertEqual(queue["entries"]["old"]["status"], "superseded")

    def test_listing_refreshes_market_when_due_then_freezes_smtp_retry_body(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            entry = queue_entry("listing")
            entry.update({"stockClose": 8.0, "stockCloseDate": "2026-10-24"})
            queue_path = root / "queue.json"
            queue_path.write_text(json.dumps({"entries": {entry["reminderKey"]: entry}}, ensure_ascii=False), encoding="utf-8")
            bodies = []

            def fail_send(_config, _subject, body):
                bodies.append(body.read_text(encoding="utf-8"))
                raise RuntimeError("smtp uncertain")

            kwargs = dict(queue_path=queue_path, state_path=root / "state.json", lock_path=root / "lock", reports_dir=root / "reports", raw_dir=root / "raw", evidence_dir=root / "evidence", client=object(), discover=False, send_func=fail_send)
            with patch("lifecycle_reminder.fetch_market_snapshot", return_value={"latestClose": 12.34, "latestCloseDate": "2026-10-28"}) as market:
                run_lifecycle_reminders(config(), dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ), **kwargs)
                run_lifecycle_reminders(config(), dt.datetime(2026, 10, 29, 9, 25, tzinfo=TZ), **kwargs)
            market.assert_called_once()
            self.assertEqual(bodies[0], bodies[1])
            self.assertIn("12.34元", bodies[0])
            self.assertIn("2026-10-28", bodies[0])
            persisted = json.loads(queue_path.read_text(encoding="utf-8"))["entries"][entry["reminderKey"]]
            self.assertNotIn("sentAt", persisted)

    def test_smtp_negative_ack_does_not_mark_sent(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            entry = queue_entry()
            queue_path = root / "queue.json"
            queue_path.write_text(json.dumps({"entries": {entry["reminderKey"]: entry}}, ensure_ascii=False), encoding="utf-8")
            result = run_lifecycle_reminders(config(), dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ), queue_path=queue_path, state_path=root / "state.json", lock_path=root / "lock", reports_dir=root / "reports", raw_dir=root / "raw", client=object(), discover=False, enrich=False, send_func=lambda *_: {"ok": False})
            self.assertFalse(result["ok"])
            stored = json.loads(queue_path.read_text(encoding="utf-8"))["entries"][entry["reminderKey"]]
            self.assertNotIn("sentAt", stored)

    def test_queue_only_run_cannot_advance_discovery_cutoff(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            original = "2026-10-28T09:15:00+08:00"
            state_path.write_text(json.dumps({"lastSuccessfulDiscoveryCutoff": original}), encoding="utf-8")
            result = run_lifecycle_reminders(config(), dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ), queue_path=root / "queue.json", state_path=state_path, lock_path=root / "lock", reports_dir=root / "reports", raw_dir=root / "raw", client=object(), discover=False, enrich=False)
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["lastSuccessfulDiscoveryCutoff"], original)

    def test_classifier_accepts_result_variants_and_listing_but_rejects_rate(self):
        cases = {
            "公司向不特定对象发行可转换公司债券网上中签结果公告": "draw",
            "公司向不特定对象发行可转换公司债券中签号码公告": "draw",
            "公司向不特定对象发行可转换公司债券网上申购摇号抽签结果公告": "draw",
            "公司向不特定对象发行可转换公司债券上市公告书": "listing",
            "公司向不特定对象发行可转换公司债券网上中签率及优先配售结果公告": "none",
            "公司向特定对象发行股票上市公告书": "none",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(classify_lifecycle_title(title)[0], expected)

    def test_only_https_exchange_domains_are_primary(self):
        self.assertTrue(
            is_official_exchange_url(
                "sse", "https://www.sse.com.cn/disclosure/a.pdf"
            )
        )
        self.assertTrue(
            is_official_exchange_url(
                "szse", "https://disc.static.szse.cn/download/a.pdf"
            )
        )
        self.assertFalse(
            is_official_exchange_url(
                "sse", "https://www.sse.com.cn.example.test/a.pdf"
            )
        )
        self.assertFalse(
            is_official_exchange_url(
                "cninfo", "https://static.cninfo.com.cn/a.pdf"
            )
        )
        self.assertFalse(
            is_official_exchange_url("sse", "http://www.sse.com.cn/a.pdf")
        )

    def test_downloaded_pdf_must_match_the_lifecycle_kind(self):
        self.assertTrue(lifecycle_document_matches("draw", draw_pdf_text()))
        self.assertTrue(lifecycle_document_matches("listing", listing_pdf_text()))
        self.assertFalse(lifecycle_document_matches("draw", listing_pdf_text()))
        self.assertFalse(
            lifecycle_document_matches(
                "draw", "测试股份关于股票交易异常波动的公告，可转换公司债券"
            )
        )

    def test_parser_extracts_draw_and_listing_action_fields(self):
        draw = parse_lifecycle_terms(draw_pdf_text(), "draw")
        self.assertEqual(draw["bondShortName"], "测试转债")
        self.assertEqual(draw["bondCode"], "110999")
        self.assertEqual(draw["paymentDate"], "2026-10-29")
        self.assertEqual(draw["lotCount"], 1)
        self.assertEqual(draw["lotQuantity"], 1)
        self.assertEqual(draw["lotUnit"], "手")
        self.assertEqual(draw["lotAmountYuan"], 1000)
        listing = parse_lifecycle_terms(listing_pdf_text(), "listing")
        self.assertEqual(listing["bondShortName"], "测试转债")
        self.assertEqual(listing["bondCode"], "110999")
        self.assertEqual(listing["listingDate"], "2026-10-29")

        szse_draw = parse_lifecycle_terms(
            "公司向不特定对象发行可转换公司债券中签号码公告。"
            "资金账户在2026年10月29日日终有足额认购资金。"
            "每个中签号码只能认购10张（1,000元）测试转债。",
            "draw",
        )
        self.assertEqual(szse_draw["paymentDate"], "2026-10-29")
        self.assertEqual(szse_draw["lotQuantity"], 10)
        self.assertEqual(szse_draw["lotUnit"], "张")
        self.assertEqual(szse_draw["lotCount"], 1)

    def test_sse_style_draw_can_use_alias_without_repeated_bond_code(self):
        text = (
            "测试股份向不特定对象发行可转换公司债券网上中签结果公告。"
            "本次发行的可转换公司债券（以下简称‘测试转债’）网上发行中签摇号。"
            "每个中签号码只能认购1手（1,000元）测试转债。"
        )
        parsed = parse_lifecycle_terms(text, "draw")
        self.assertEqual(parsed["bondShortName"], "测试转债")
        self.assertIsNone(parsed["bondCode"])

    def test_due_boundary_and_cross_day_rules(self):
        entry = queue_entry()
        self.assertFalse(reminder_due(entry, dt.datetime(2026, 10, 29, 9, 14, tzinfo=TZ)))
        self.assertTrue(reminder_due(entry, dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ)))
        self.assertTrue(reminder_due(entry, dt.datetime(2026, 10, 29, 14, 59, tzinfo=TZ)))
        self.assertFalse(reminder_due(entry, dt.datetime(2026, 10, 29, 15, 0, tzinfo=TZ)))
        self.assertFalse(reminder_due(entry, dt.datetime(2026, 10, 30, 9, 15, tzinfo=TZ)))
        entry["sentAt"] = "2026-10-29T09:15:00+08:00"
        self.assertFalse(reminder_due(entry, dt.datetime(2026, 10, 29, 9, 16, tzinfo=TZ)))

    def test_corrected_listing_date_supersedes_old_entry(self):
        now = dt.datetime(2026, 10, 25, 9, 15, tzinfo=TZ)
        old = queue_entry("listing")
        old["targetDate"] = old["listingDate"] = "2026-10-29"
        old["reminderKey"] = "old"
        new = dict(old)
        new["targetDate"] = new["listingDate"] = "2026-10-30"
        new["reminderKey"] = "new"
        queue = {"schemaVersion": 1, "entries": {"old": old}}
        upsert_entries(queue, [new], now)
        self.assertEqual(queue["entries"]["old"]["status"], "superseded")
        self.assertEqual(queue["entries"]["old"]["supersededBy"], "new")
        self.assertEqual(queue["entries"]["new"]["status"], "active")

    def test_missing_bond_codes_do_not_supersede_other_stocks(self):
        now = dt.datetime(2026, 10, 25, 9, 15, tzinfo=TZ)
        first = queue_entry("draw")
        first.update(
            {
                "reminderKey": "first",
                "bondCode": None,
                "stockCode": "600001",
                "bondShortName": "甲方转债",
                "targetDate": "2026-10-25",
            }
        )
        second = dict(first)
        second.update(
            {
                "reminderKey": "second",
                "stockCode": "600002",
                "bondShortName": "乙方转债",
                "targetDate": "2026-10-26",
            }
        )
        queue = {"schemaVersion": 1, "entries": {}}
        upsert_entries(queue, [first, second], now)
        self.assertEqual(queue["entries"]["first"]["status"], "active")
        self.assertEqual(queue["entries"]["second"]["status"], "active")

    def test_exact_titles_mobile_body_and_html_escape(self):
        draw = queue_entry("draw")
        draw["mainBusiness"] = "设备<制造>"
        body = build_lifecycle_html(draw)
        self.assertEqual(lifecycle_subject(draw), "【可转债出签】测试转债")
        self.assertIn("请登录券商账户查询是否中签", body)
        self.assertIn("设备&lt;制造&gt;", body)
        self.assertNotIn("设备<制造>", body)
        listing = queue_entry("listing")
        self.assertEqual(lifecycle_subject(listing), "【可转债上市】测试转债")
        listing_body = build_lifecycle_html(listing)
        self.assertIn("上市首日价格", listing_body)
        self.assertIn("参考转股价值", listing_body)

    def test_non_exchange_record_fails_closed_before_download(self):
        record = {
            "source": "cninfo",
            "code": "600001",
            "company": "测试股份",
            "title": "测试股份向不特定对象发行可转换公司债券中签号码公告",
            "date": "2026-10-29",
            "url": "https://static.cninfo.com.cn/test.pdf",
        }
        entry, missing = build_lifecycle_entry(
            record,
            object(),
            dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ),
            download_func=lambda *_: self.fail("must not download secondary-only notice"),
            enrich=False,
        )
        self.assertIsNone(entry)
        self.assertEqual(missing, ["officialExchangeUrl"])

    def test_missing_listing_date_fails_closed_after_pdf_validation(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)

            def download(_client, url, target, _referer):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-test")
                return {
                    "path": str(target),
                    "url": url,
                    "pdfValidated": True,
                    "sha256": "a" * 64,
                }

            entry, missing = build_lifecycle_entry(
                announcement(kind="listing"),
                object(),
                dt.datetime(2026, 10, 25, 9, 15, tzinfo=TZ),
                evidence_dir=root / "evidence",
                subscription_queue_path=root / "subscription.json",
                download_func=download,
                pdf_text_func=lambda _: listing_pdf_text("").replace("上市日期：。", ""),
                enrich=False,
            )
            self.assertIsNone(entry)
            self.assertIn("targetDate", missing)

    def test_end_to_end_discovers_validates_sends_once_and_persists(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            sent = []

            def fetch_sse_fake(*_args):
                return [announcement()], {"official": "sse"}

            def fetch_szse_fake(*_args):
                return [], {"official": "szse"}

            def download(_client, url, target, _referer):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-test")
                return {
                    "path": str(target),
                    "url": url,
                    "pdfValidated": True,
                    "sha256": "a" * 64,
                }

            kwargs = {
                "queue_path": root / "queue.json",
                "state_path": root / "state.json",
                "lock_path": root / "queue.lock",
                "subscription_queue_path": root / "subscription.json",
                "evidence_dir": root / "evidence",
                "reports_dir": root / "reports",
                "raw_dir": root / "raw",
                "client": object(),
                "fetchers": {"sse": fetch_sse_fake, "szse": fetch_szse_fake},
                "download_func": download,
                "pdf_text_func": lambda _: draw_pdf_text(),
                "send_func": lambda _config, subject, body: (
                    sent.append((subject, body.read_text(encoding="utf-8")))
                    or {"ok": True, "duplicate": False}
                ),
                "enrich": False,
            }
            now = dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ)
            first = run_lifecycle_reminders(config(), now, **kwargs)
            second = run_lifecycle_reminders(config(), now, **kwargs)
            self.assertTrue(first["ok"])
            self.assertEqual(first["addedOrUpdated"], 1)
            self.assertEqual(first["actions"][0]["status"], "sent")
            self.assertEqual(second["actions"], [])
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "【可转债出签】测试转债")
            queue = json.loads((root / "queue.json").read_text(encoding="utf-8"))
            self.assertTrue(next(iter(queue["entries"].values()))["sentAt"])
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["lastRunResult"], "success")
            self.assertIn("lastSuccessfulDiscoveryCutoff", state)

    def test_source_failure_does_not_block_already_verified_due_queue(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            entry = queue_entry()
            (root / "queue.json").write_text(
                json.dumps({"schemaVersion": 1, "entries": {entry["reminderKey"]: entry}}, ensure_ascii=False),
                encoding="utf-8",
            )

            def fail(*_args):
                raise RuntimeError("official endpoint unavailable")

            sent = []
            result = run_lifecycle_reminders(
                config(),
                dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ),
                queue_path=root / "queue.json",
                state_path=root / "state.json",
                lock_path=root / "queue.lock",
                reports_dir=root / "reports",
                raw_dir=root / "raw",
                client=object(),
                fetchers={"sse": fail, "szse": fail},
                send_func=lambda *_: (
                    sent.append(True) or {"ok": True, "duplicate": False}
                ),
                enrich=False,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["actions"][0]["status"], "sent")
            self.assertEqual(len(sent), 1)

    def test_multiple_due_entries_are_isolated_and_duplicate_is_success(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            first = queue_entry("draw", "110999")
            second = queue_entry("listing", "123999")
            queue = {
                "schemaVersion": 1,
                "entries": {first["reminderKey"]: first, second["reminderKey"]: second},
            }
            (root / "queue.json").write_text(
                json.dumps(queue, ensure_ascii=False), encoding="utf-8"
            )

            def send(_config, subject, _body):
                if "出签" in subject:
                    raise RuntimeError("smtp unavailable")
                return {"ok": True, "duplicate": True}

            result = run_lifecycle_reminders(
                config(),
                dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ),
                queue_path=root / "queue.json",
                state_path=root / "state.json",
                lock_path=root / "queue.lock",
                reports_dir=root / "reports",
                raw_dir=root / "raw",
                client=object(),
                send_func=send,
                discover=False,
                enrich=False,
            )
            self.assertFalse(result["ok"])
            statuses = {item["kind"]: item["status"] for item in result["actions"]}
            self.assertEqual(statuses, {"draw": "failed", "listing": "sent"})
            persisted = json.loads((root / "queue.json").read_text(encoding="utf-8"))
            listing = persisted["entries"][second["reminderKey"]]
            draw = persisted["entries"][first["reminderKey"]]
            self.assertTrue(listing["duplicateAccepted"])
            self.assertNotIn("sentAt", draw)

    def test_dry_run_never_sends_or_mutates_queue(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT_DIR) as temp_dir:
            root = Path(temp_dir)
            entry = queue_entry()
            original = {"schemaVersion": 1, "entries": {entry["reminderKey"]: entry}}
            queue_path = root / "queue.json"
            queue_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            result = run_lifecycle_reminders(
                config(),
                dt.datetime(2026, 10, 29, 9, 15, tzinfo=TZ),
                queue_path=queue_path,
                state_path=root / "state.json",
                lock_path=root / "queue.lock",
                reports_dir=root / "reports",
                raw_dir=root / "raw",
                client=object(),
                send_func=lambda *_: self.fail("dry run must not call SMTP"),
                discover=False,
                dry_run=True,
                enrich=False,
            )
            self.assertEqual(result["actions"][0]["status"], "would_send")
            persisted = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertNotIn("sentAt", next(iter(persisted["entries"].values())))
            self.assertFalse((root / "state.json").exists())

    def test_runner_is_utf8_and_invokes_only_lifecycle_program(self):
        script = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("lifecycle_reminder.py", script)
        self.assertIn("PYTHONUTF8", script)
        self.assertIn("bond-lifecycle-reminder-", script)
        self.assertNotIn("monitor.py", script)
        self.assertNotIn("codex", script.lower())


if __name__ == "__main__":
    unittest.main()
