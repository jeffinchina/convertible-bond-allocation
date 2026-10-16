import datetime as dt
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from monitor import (
    Announcement,
    CNINFO_QUERY_FALLBACK_ENDPOINTS,
    HttpClient,
    build_pending_inventory_html,
    build_failure_html,
    backfill_pending_entries,
    calculation_html,
    calculate_inventory_metrics,
    classify_title,
    cninfo_transport_detail,
    download_evidence,
    fetch_cninfo_market,
    formal_calculation,
    formal_calculation_html,
    group_candidates,
    iso_date,
    invoke_event_runner,
    market_days_between,
    merge_formal_term_sets,
    parse_company_profile,
    parse_formal_terms,
    parse_latest_share_snapshot,
    parse_registration_amount,
    partition_event_evidence,
    preserve_inventory_enrichment,
    reference_conversion_price_metrics,
    registration_calculation,
    strip_terminal_escapes,
    supplemental_formal_records,
)


class ClassificationTests(unittest.TestCase):
    def assert_stage(self, title: str, expected: str) -> None:
        stage, reason = classify_title(title)
        self.assertEqual(stage, expected, reason)

    def test_registration_variants(self) -> None:
        self.assert_stage(
            "五洲交通关于向不特定对象发行可转换公司债券申请获得中国证监会同意注册批复的公告",
            "registration",
        )

    def test_http_text_decompresses_gzip_even_without_header(self) -> None:
        client = HttpClient(1, 1)
        payload = '{"主营业务":"汽车行业"}'.encode("utf-8")
        client.request = lambda url, headers=None: (  # type: ignore[method-assign]
            gzip.compress(payload),
            {"content-type": "application/json"},
            url,
        )
        text, _ = client.get_text("https://example.test/data")
        self.assertEqual(text, payload.decode("utf-8"))
        self.assert_stage(
            "关于收到中国证券监督管理委员会同意向不特定对象发行可转换公司债券注册批复的公告",
            "registration",
        )

    def test_cninfo_403_uses_official_fallback_and_reuses_it(self) -> None:
        primary = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
        fallback = CNINFO_QUERY_FALLBACK_ENDPOINTS[0]

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def post_form(self, url, form, *, headers=None):
                self.calls.append(url)
                if url == primary:
                    raise RuntimeError("HTTP Error 403: Forbidden")
                return (
                    json.dumps(
                        {
                            "announcements": [
                                {
                                    "announcementTitle": "向不特定对象发行可转换公司债券发行公告",
                                    "announcementTime": "2026-09-15",
                                    "secCode": "002997",
                                    "secName": "瑞鹄模具",
                                    "adjunctUrl": "final.pdf",
                                    "announcementId": "ruihu-final",
                                }
                            ],
                            "totalpages": 1,
                        },
                        ensure_ascii=False,
                    ),
                    url,
                )

        client = FakeClient()
        start = dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)
        records, pages = fetch_cninfo_market(
            client, primary, start, end, "szse", "sz", 1
        )
        self.assertEqual(client.calls, [primary, fallback])
        self.assertEqual(records[0].code, "002997")
        self.assertEqual(records[0].stage, "formal")
        self.assertTrue(pages[0]["_fetchMeta"]["fallbackUsed"])
        self.assertEqual(pages[0]["_fetchMeta"]["usedEndpoint"], fallback)
        self.assertIn("官方备用域名", cninfo_transport_detail(pages))

        client.calls.clear()
        fetch_cninfo_market(client, primary, start, end, "sse", "sh", 1)
        self.assertEqual(client.calls, [fallback])

    def test_cninfo_fails_closed_when_all_official_endpoints_fail(self) -> None:
        primary = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

        class FailingClient:
            def post_form(self, url, form, *, headers=None):
                raise RuntimeError("HTTP Error 403: Forbidden")

        with self.assertRaisesRegex(
            RuntimeError, "巨潮公告主站与官方备用域名均失败"
        ):
            fetch_cninfo_market(
                FailingClient(),
                primary,
                dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
                "szse",
                "sz",
                1,
            )

    def test_cninfo_pdf_uses_secure_browser_compatible_fallback(self) -> None:
        url = "https://static.cninfo.com.cn/finalpage/2026-09-15/test.PDF"

        class BlockedClient:
            timeout = 1
            retries = 1

            def request(self, url, *, headers=None):
                raise RuntimeError("HTTP Error 403: Forbidden")

        with patch(
            "monitor.download_cninfo_with_browser_compatibility",
            return_value=(
                b"%PDF-1.4\n%test",
                {"content-type": "application/pdf"},
                url,
            ),
        ), patch.object(Path, "write_bytes") as write_bytes:
            target = Path("document.pdf")
            result = download_evidence(
                BlockedClient(), url, target, "https://www.cninfo.com.cn/"
            )
        write_bytes.assert_called_once_with(b"%PDF-1.4\n%test")
        self.assertTrue(result["pdfValidated"])
        self.assertTrue(result["transportFallback"])
        self.assertEqual(result["transport"], "curl_cffi_chrome_https")
        self.assertIn("403", result["primaryTransportError"])

    def test_formal_variants(self) -> None:
        self.assert_stage("向不特定对象发行可转换公司债券发行公告", "formal")
        self.assert_stage("向不特定对象发行可转换公司债券募集说明书", "formal")
        self.assert_stage(
            "向不特定对象发行可转换公司债券原股东优先配售提示性公告", "formal"
        )

    def test_exclusions(self) -> None:
        titles = [
            "向不特定对象发行可转换公司债券预案",
            "关于向不特定对象发行可转换公司债券申请获得受理的公告",
            "向不特定对象发行可转换公司债券审核问询函的回复",
            "关于向不特定对象发行可转换公司债券提交募集说明书（注册稿）等申请文件的提示性公告",
            "向不特定对象发行可转换公司债券募集说明书（注册稿）",
            "壹连科技：向不特定对象发行可转换公司债券募集说明书（上会稿）",
            "向不特定对象发行可转换公司债券募集说明书（审核稿）",
            "可转换公司债券上市公告书",
            "可转换公司债券发行结果公告",
            "向特定对象发行可转换公司债券购买资产注册的批复",
            "可转换公司债券2026年付息公告",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assert_stage(title, "none")

    def test_multi_source_hearing_drafts_do_not_form_an_event(self) -> None:
        title = "向不特定对象发行可转换公司债券募集说明书（上会稿）"
        stage, _ = classify_title(title)
        records = [
            Announcement(
                source=source,
                market="SZSE",
                code="301631",
                company="壹连科技",
                title=title,
                date="2026-09-08",
                url=f"https://example.test/{source}.pdf",
                source_id=source,
                stage=stage,
            )
            for source in ("szse", "cninfo")
        ]
        self.assertEqual(stage, "none")
        self.assertEqual(group_candidates(records), [])

    def test_terminal_styles_are_removed_from_failure_diagnostics(self) -> None:
        styled = "\x1b[31;1mException: \x1b[0m事件关键计算数据不完整"
        self.assertEqual(strip_terminal_escapes(styled), "Exception: 事件关键计算数据不完整")
        body = build_failure_html(
            dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc),
            [],
            [styled],
        )
        self.assertNotIn("\x1b", body)
        self.assertNotIn("[31;1m", body)
        self.assertIn("Exception: 事件关键计算数据不完整", body)

    def test_event_runner_cleans_captured_powershell_stderr(self) -> None:
        completed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="\x1b[31;1mException: \x1b[0m测试异常\n",
        )
        with patch("monitor.subprocess.run", return_value=completed):
            result = invoke_event_runner(
                {"eventRunner": "Run-EventAnalysis.ps1"}, Path("event-batch.json")
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["stderr"], "Exception: 测试异常\n")

    def test_registration_inputs_are_parsed_from_official_text(self) -> None:
        text = (
            "本次发行的募集资金总额不超过人民币 37,000.00 万元（含本数）。"
            "截至 2025 年 12 月 31 日，公司总股本为 180,136,352 股。"
        )
        amount = parse_registration_amount(text)
        shares = parse_latest_share_snapshot(text)
        self.assertIsNotNone(amount)
        self.assertIsNotNone(shares)
        self.assertEqual(amount["valueYuan"], 370_000_000)
        self.assertEqual(shares["value"], 180_136_352)
        self.assertEqual(shares["asOfDate"], "2025-12-31")

    def test_registration_calculation_is_actionable(self) -> None:
        result = registration_calculation(370_000_000, 180_136_352, 29.12)
        self.assertAlmostEqual(result["allocationPerShare"], 2.054, places=3)
        self.assertEqual(result["conservativeShares"], 500)
        self.assertEqual(result["capitalRequiredYuan"], 14_560)
        self.assertAlmostEqual(result["rightsValuePct"], 7.05, places=2)
        self.assertEqual(set(result["breakEvenStockDropPct"]), {"15", "20", "30", "40", "50"})

    def test_reference_conversion_price_uses_higher_vwap(self) -> None:
        history = []
        for day in range(1, 21):
            history.append(
                {
                    "date": f"2026-08-{day:02d}",
                    "volumeLots": 1,
                    "turnoverYuan": 1_200 if day == 20 else 1_000,
                }
            )
        metrics = reference_conversion_price_metrics(
            {"latestClose": 11.0, "history": history}
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["referenceConversionPrice"], 12.0)
        self.assertEqual(metrics["stockToConversionPricePct"], 91.67)

    def test_reference_conversion_price_excludes_intraday_row(self) -> None:
        history = [
            {
                "date": f"2026-08-{day:02d}",
                "volumeLots": 1,
                "turnoverYuan": 1_000,
            }
            for day in range(1, 21)
        ]
        history.append(
            {"date": "2026-08-21", "volumeLots": 1, "turnoverYuan": 5_000}
        )
        metrics = reference_conversion_price_metrics(
            {"latestClose": 10.0, "history": history}, "2026-08-20"
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["referenceConversionPrice"], 10.0)

    def test_proposal_amount_sentence_is_parsed(self) -> None:
        text = "本次发行可转换公司债券的募集资金总额为不超过人民币 102,339.50 万元（含本数）"
        amount = parse_registration_amount(text)
        self.assertIsNotNone(amount)
        self.assertEqual(amount["valueYuan"], 1_023_395_000)

    def test_registration_amount_pdf_variants_are_parsed(self) -> None:
        variants = [
            ("预计募集资金量（含发行费用）不超过人民币96,000.00万元（含本数）", 960_000_000),
            ("公司本次拟向不特定对象发行可转换公司债券的募集资金不超过 96,000.00 万元", 960_000_000),
            ("公司本次拟发行可转换债券的金额为不超过 96,000.00 万元", 960_000_000),
            ("本次拟发行可转换公司债券的募集资金总额不超过人民币 192,000.00 万元", 1_920_000_000),
            ("本次拟发行的可转换公司债券募集资金总额不超过人民币 69,500 万 元", 695_000_000),
            ("本次可转债预计募集资金总额为不超过人民币55,000.00万元", 550_000_000),
            ("本 次拟发行的可转换公司债券募集资金总额不超过人民币68,600.00万元", 686_000_000),
            ("公司本次发行 可转债 拟募集资金总额不超过人民币65,041.42万元", 650_414_200),
        ]
        for text, expected in variants:
            with self.subTest(text=text):
                amount = parse_registration_amount(text)
                self.assertIsNotNone(amount)
                self.assertEqual(amount["valueYuan"], expected)

    def test_formal_terms_are_parsed_from_prospectus_text(self) -> None:
        text = (
            "本次发行的募集资金总额不超过人民币37,000.00万元。"
            "截至2026年6月30日，公司总股本为180,136,352股。"
            "每股配售2.0539元可转债。"
            "本次发行的可转债的初始转股价格为31.10元/股。"
            "股权登记日（2026年9月9日，T-1日）。网上申购日：2026年9月10日。"
        )
        terms = parse_formal_terms(text)
        self.assertEqual(terms["finalAmount"]["valueYuan"], 370_000_000)
        self.assertEqual(terms["shareSnapshot"]["value"], 180_136_352)
        self.assertEqual(terms["allocationPerShare"], 2.0539)
        self.assertEqual(terms["initialConversionPrice"], 31.10)
        self.assertEqual(terms["recordDate"], "2026-09-09")
        self.assertEqual(terms["subscriptionDate"], "2026-09-10")

    def test_ruihu_formal_issuance_wording_is_parsed(self) -> None:
        text = (
            "本次发行人民币 6.86 亿元可转债，每张面值为人民币 100 元。"
            "本次发行的可转债总额为人民币 6.86 亿元。"
            "发行人现有 A 股总股本 209,321,325 股，公司无回购专户库存股，"
            "可参与本次发行优先配售的 A 股股本为 209,321,325 股。"
            "每股配售 3.2772 元面值可转债。"
            "初始转股价格：29.18 元/股。"
            "股权登记日（2026 年 9 月 16 日，T-1 日）。"
            "网上申购日：2026 年 9 月 17 日。"
        )
        terms = parse_formal_terms(text)
        self.assertEqual(terms["finalAmount"]["valueYuan"], 686_000_000)
        self.assertEqual(terms["shareSnapshot"]["value"], 209_321_325)
        self.assertEqual(
            terms["shareSnapshot"]["basis"], "正式发行文件披露的可参与优先配售股本"
        )
        self.assertEqual(terms["allocationPerShare"], 3.2772)
        self.assertEqual(terms["initialConversionPrice"], 29.18)
        self.assertEqual(terms["recordDate"], "2026-09-16")
        self.assertEqual(terms["subscriptionDate"], "2026-09-17")

    def test_guoke_real_formal_issuance_wording_is_parsed(self) -> None:
        text = (
            "本次发行总额为人民币 84,880.00 万元，发行数量为 8,488,000 张。"
            "公司现有总股本 179,425,908 股，无回购专户库存股，"
            "可参与原股东优先配售的股本数量为 179,425,908 股。"
            "原股东可优先配售的可转债数量为其在股权登记日收市后登记在册的"
            "持有公司股份数按每股配售 4.7306 元面值可转债的比例计算。"
            "本次发行的可转债的初始转股价格为 74.50 元/股。"
            "股权登记日：2026 年 9 月 22 日。"
            "原股东优先配售认购日、网上申购日：2026 年 9 月 23 日。"
        )
        terms = parse_formal_terms(text)
        self.assertEqual(terms["finalAmount"]["valueYuan"], 848_800_000)
        self.assertEqual(terms["shareSnapshot"]["value"], 179_425_908)
        self.assertEqual(terms["allocationPerShare"], 4.7306)
        self.assertEqual(terms["initialConversionPrice"], 74.50)
        self.assertEqual(terms["recordDate"], "2026-09-22")
        self.assertEqual(terms["subscriptionDate"], "2026-09-23")

    def test_formal_schedule_table_keeps_t_minus_one_record_date(self) -> None:
        text = (
            "发行安排2026年10月14日周三T-2日披露募集说明书及发行公告"
            "2026年10月15日周四T-1日网上路演原股东优先配售股权登记日"
            "2026年10月16日周五T日披露发行提示性公告"
            "原股东优先配售认购日（缴付足额资金）网上申购日。"
        )
        terms = parse_formal_terms(text)
        self.assertEqual(terms["recordDate"], "2026-10-15")
        self.assertEqual(terms["subscriptionDate"], "2026-10-16")

    def test_formal_share_snapshot_prefers_eligible_shares_consistent_with_terms(self) -> None:
        text = (
            "本次发行 100,000.00 万元可转债。"
            "公司现有 A 股总股本 317,360,504 股，其中回购专用证券账户持有"
            "5,876,100 股，可参与本次发行优先配售的 A 股股本为 311,484,404 股。"
            "原股东可优先配售的可转债数量按每股配售 3.2104 元面值可转债计算。"
        )
        terms = parse_formal_terms(text)
        amount = terms["finalAmount"]["valueYuan"]
        selected_shares = terms["shareSnapshot"]["value"]
        allocation = terms["allocationPerShare"]
        self.assertEqual(selected_shares, 311_484_404)
        self.assertLess(abs(amount - allocation * selected_shares) / amount, 0.0001)
        self.assertGreater(abs(amount - allocation * 317_360_504) / amount, 0.01)

    def test_laite_real_formal_terms_ignore_historical_reduction_and_use_eligible_base(self) -> None:
        prospectus_text = (
            "本次拟发行的可转换公司债券募集资金总额为人民币52,477.90万元（含本数），"
            "发行数量524,779手。"
            "2026年6月1日，公司审议通过调整发行方案，调减本次向不特定对象发行"
            "可转换公司债券的募集资金6,800.00万元，调整后拟募集资金总额为69,800.00万元。"
            "发行人现有总股本402,437,585股，剔除公司回购专户库存股2,998,449股后，"
            "可参与原股东优先配售的股本总额为399,439,136股。"
            "原股东持有的发行人股份数量按每股配售1.313元面值可转债的比例计算。"
            "初始转股价格为44.82元/股。"
            "股权登记日：2026年9月28日。网上申购日：2026年9月29日。"
        )
        issuance_text = (
            "本次发行52,477.90万元可转债，每张面值100元，共计5,247,790张。"
            "发行人现有总股本402,437,585股，剔除公司回购专户库存股2,998,449股后，"
            "可参与原股东优先配售的股本总额为399,439,136股。"
            "原股东股份数量按每股配售1.313元面值可转债的比例计算。"
            "初始转股价格为44.82元/股。"
            "股权登记日：2026年9月28日。网上申购日：2026年9月29日。"
        )

        prospectus_terms = parse_formal_terms(prospectus_text)
        issuance_terms = parse_formal_terms(issuance_text)
        self.assertEqual(prospectus_terms["finalAmount"]["valueYuan"], 524_779_000)
        self.assertEqual(prospectus_terms["shareSnapshot"]["value"], 399_439_136)
        self.assertEqual(issuance_terms["finalAmount"]["valueYuan"], 524_779_000)
        self.assertEqual(issuance_terms["shareSnapshot"]["value"], 399_439_136)
        self.assertEqual(prospectus_terms["consistency"]["status"], "pass")
        self.assertAlmostEqual(
            prospectus_terms["consistency"]["gapPct"], 0.060104, places=6
        )

        merged = merge_formal_term_sets(
            [
                {
                    "source": {"title": "莱特光电募集说明书", "layer": "primary"},
                    "terms": prospectus_terms,
                },
                {
                    "source": {"title": "莱特光电发行公告", "layer": "cninfo"},
                    "terms": issuance_terms,
                },
            ]
        )
        missing = [
            field
            for field in (
                "finalAmount",
                "shareSnapshot",
                "allocationPerShare",
                "initialConversionPrice",
                "recordDate",
                "subscriptionDate",
            )
            if not merged.get(field)
        ]
        if merged.get("conflicts"):
            missing.append("formalTermConflict")
        if (merged.get("consistency") or {}).get("status") == "conflict":
            missing.append("allocationConsistency")
        self.assertEqual(merged["finalAmount"]["valueYuan"], 524_779_000)
        self.assertEqual(merged["shareSnapshot"]["value"], 399_439_136)
        self.assertEqual(merged["consistency"]["status"], "pass")
        self.assertAlmostEqual(merged["consistency"]["gapPct"], 0.060104, places=6)
        self.assertEqual(merged["conflicts"], [])
        self.assertEqual(missing, [])

    def test_merge_formal_term_sets_complements_fields_and_keeps_sources(self) -> None:
        issuance_source = {
            "title": "国科天成向不特定对象发行可转换公司债券发行公告",
            "url": "https://example.test/issuance.pdf",
            "layer": "primary",
        }
        prospectus_source = {
            "title": "国科天成向不特定对象发行可转换公司债券募集说明书",
            "url": "https://example.test/prospectus.pdf",
            "layer": "formal-supplement-1",
        }
        merged = merge_formal_term_sets(
            [
                {
                    "source": issuance_source,
                    "terms": {
                        "finalAmount": {
                            "valueYuan": 848_800_000,
                            "rawText": "本次发行总额为人民币84,880.00万元",
                        },
                        "allocationPerShare": 4.7306,
                        "recordDate": "2026-09-22",
                    },
                },
                {
                    "source": prospectus_source,
                    "terms": {
                        "shareSnapshot": {
                            "value": 179_425_908,
                            "rawText": "可参与原股东优先配售的股本数量为179,425,908股",
                        },
                        "initialConversionPrice": 74.50,
                        "subscriptionDate": "2026-09-23",
                    },
                },
            ]
        )
        self.assertEqual(merged["finalAmount"]["valueYuan"], 848_800_000)
        self.assertEqual(merged["shareSnapshot"]["value"], 179_425_908)
        self.assertEqual(merged["allocationPerShare"], 4.7306)
        self.assertEqual(merged["initialConversionPrice"], 74.50)
        self.assertEqual(merged["recordDate"], "2026-09-22")
        self.assertEqual(merged["subscriptionDate"], "2026-09-23")
        self.assertEqual(merged["consistency"]["status"], "pass")
        self.assertEqual(merged["conflicts"], [])
        self.assertEqual(merged["fieldSources"]["finalAmount"][0]["url"], issuance_source["url"])
        self.assertIn(
            "84,880.00万元", merged["fieldSources"]["finalAmount"][0]["rawText"]
        )
        self.assertEqual(
            merged["fieldSources"]["shareSnapshot"][0]["url"], prospectus_source["url"]
        )
        self.assertIn(
            "179,425,908股",
            merged["fieldSources"]["shareSnapshot"][0]["rawText"],
        )

    def test_merge_formal_term_sets_reports_conflicting_values(self) -> None:
        merged = merge_formal_term_sets(
            [
                {
                    "source": {"title": "发行公告", "url": "https://example.test/a.pdf"},
                    "terms": {"initialConversionPrice": 74.50},
                },
                {
                    "source": {"title": "募集说明书", "url": "https://example.test/b.pdf"},
                    "terms": {"initialConversionPrice": 75.00},
                },
            ]
        )
        self.assertEqual(merged["initialConversionPrice"], 74.50)
        self.assertEqual(len(merged["conflicts"]), 1)
        conflict = merged["conflicts"][0]
        self.assertEqual(conflict["field"], "initialConversionPrice")
        self.assertEqual(conflict["chosen"], 74.50)
        self.assertEqual(conflict["chosenSource"]["url"], "https://example.test/a.pdf")
        self.assertEqual(conflict["alternatives"][0]["value"], 75.00)
        self.assertEqual(
            conflict["alternatives"][0]["source"]["url"], "https://example.test/b.pdf"
        )

    def test_supplemental_formal_records_orders_and_deduplicates_semantic_types(self) -> None:
        candidate = {
            "primary": {
                "source": "szse",
                "stage": "formal",
                "title": "国科天成向不特定对象发行可转换公司债券发行公告",
                "url": "https://example.test/primary-issuance.pdf",
            },
            "cninfo": {
                "source": "cninfo",
                "stage": "formal",
                "title": "向不特定对象发行可转换公司债券发行公告",
                "url": "https://example.test/cninfo-issuance.pdf",
            },
            "records": [
                {
                    "source": "cninfo",
                    "stage": "formal",
                    "title": "向不特定对象发行可转换公司债券募集说明书",
                    "url": "https://example.test/cninfo-prospectus.pdf",
                },
                {
                    "source": "szse",
                    "stage": "formal",
                    "title": "国科天成向不特定对象发行可转换公司债券募集说明书",
                    "url": "https://example.test/szse-prospectus.pdf",
                },
                {
                    "source": "cninfo",
                    "stage": "formal",
                    "title": "向不特定对象发行可转换公司债券原股东优先配售公告",
                    "url": "https://example.test/cninfo-priority.pdf",
                },
                {
                    "source": "szse",
                    "stage": "formal",
                    "title": "国科天成向不特定对象发行可转换公司债券原股东优先配售公告",
                    "url": "https://example.test/szse-priority.pdf",
                },
                {
                    "source": "szse",
                    "stage": "formal",
                    "title": "国科天成向不特定对象发行可转换公司债券发行公告",
                    "url": "https://example.test/duplicate-issuance.pdf",
                },
            ],
        }
        selected = supplemental_formal_records(candidate)
        self.assertEqual(
            [item["url"] for item in selected],
            [
                "https://example.test/szse-prospectus.pdf",
                "https://example.test/szse-priority.pdf",
            ],
        )

    def test_partition_event_evidence_isolates_complete_and_incomplete_events(self) -> None:
        candidates = [
            {"eventKey": "event-complete", "code": "002913", "company": "奥士康"},
            {"eventKey": "event-incomplete", "code": "301571", "company": "国科天成"},
        ]
        temp_paths: list[Path] = []
        try:
            payloads = (
                {
                    "calculation": {
                        "status": "complete",
                        "missingFields": [],
                        "warnings": [],
                    }
                },
                {
                    "calculation": {
                        "status": "incomplete",
                        "missingFields": ["finalAmount", "totalShares"],
                        "warnings": ["正式发行条款待补齐"],
                    }
                },
            )
            for payload in payloads:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix="-evidence.json",
                    dir=Path(__file__).parent / "runtime",
                    delete=False,
                ) as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                    temp_paths.append(Path(handle.name))
            complete_path, incomplete_path = temp_paths
            complete, incomplete = partition_event_evidence(
                candidates, [complete_path, incomplete_path]
            )

            self.assertEqual(len(complete), 1)
            self.assertEqual(complete[0][0]["eventKey"], "event-complete")
            self.assertEqual(complete[0][1], complete_path)
            self.assertEqual(len(incomplete), 1)
            self.assertEqual(
                incomplete[0]["candidate"]["eventKey"], "event-incomplete"
            )
            self.assertEqual(
                incomplete[0]["missingFields"], ["finalAmount", "totalShares"]
            )
            self.assertEqual(incomplete[0]["warnings"], ["正式发行条款待补齐"])
            self.assertEqual(
                incomplete[0]["evidencePath"], str(incomplete_path.resolve())
            )
        finally:
            for path in temp_paths:
                path.unlink(missing_ok=True)

    def test_formal_calculation_and_html_have_no_missing_market_values(self) -> None:
        calculation = formal_calculation(
            370_000_000,
            180_136_352,
            2.0539,
            29.12,
            31.10,
        )
        self.assertEqual(calculation["n0Shares"], 486.88)
        self.assertEqual(calculation["conservativeShares"], 500)
        self.assertEqual(calculation["capitalRequiredYuan"], 14_560)
        self.assertEqual(calculation["stockToConversionPricePct"], 93.63)
        self.assertEqual(calculation["conversionValue"], 93.63)
        html = formal_calculation_html(
            "300727",
            calculation,
            {
                "recordDate": "2026-09-09",
                "subscriptionDate": "2026-09-10",
                "shareSnapshot": {"asOfDate": "2026-06-30"},
            },
            {
                "latestCloseDate": "2026-09-07",
                "return10Pct": 1.2,
                "return20Pct": 2.3,
                "dailyVolatility20Pct": 1.1,
                "historyUrl": "https://example.test/history",
                "quoteUrl": "https://example.test/quote",
                "provider": "test",
            },
            {
                "industry": "其他化学制品",
                "mainBusiness": "有机硅深加工产品",
                "mainBusinessReportDate": "2026-06-30",
            },
            {"date": "2026-09-08", "url": "https://example.test/prospectus.pdf"},
        )
        self.assertNotIn("待补证", html)
        self.assertIn("29.12元", html)
        self.assertIn("14,560.00元", html)
        self.assertIn("93.63元", html)
        self.assertIn("正式发行文件", html)
        self.assertIn("最新已完成交易日收盘价 S", html)
        self.assertNotIn("公告日前一交易日收盘价 S", html)
        self.assertLess(html.index("行业｜主营业务"), html.index("最终发行规模"))

    def test_cninfo_timestamp_is_beijing_date(self) -> None:
        self.assertEqual(iso_date(1787241600000), "2026-08-21")

    def test_inventory_backfill_unions_exchange_and_cninfo_sources(self) -> None:
        wuzhou = Announcement(
            source="sse",
            market="SSE",
            code="600368",
            company="五洲交通",
            title="五洲交通关于向不特定对象发行可转换公司债券申请获得中国证监会同意注册批复的公告",
            date="2026-08-21",
            url="https://www.sse.com.cn/wuzhou.pdf",
            source_id="wuzhou",
            stage="registration",
            reason="test",
        )
        issued = Announcement(
            source="szse",
            market="SZSE",
            code="300727",
            company="润禾材料",
            title="润禾材料向不特定对象发行可转换公司债券募集说明书",
            date="2026-09-08",
            url="https://www.szse.cn/runhe.pdf",
            source_id="runhe-formal",
            stage="formal",
            reason="test",
        )
        runhe_registration = Announcement(
            source="cninfo",
            market="SZSE",
            code="300727",
            company="润禾材料",
            title="关于向不特定对象发行可转换公司债券申请获得同意注册批复的公告",
            date="2026-08-27",
            url="https://www.cninfo.com.cn/runhe-registration.pdf",
            source_id="runhe-registration",
            stage="registration",
            reason="test",
        )
        config = {
            "maxPages": 8,
            "inventory": {"lookbackDays": 370, "approvalValidityDays": 365},
            "officialSources": {
                "sseAnnouncements": "https://sse.test",
                "szseAnnouncements": "https://szse.test",
                "cninfoAnnouncements": "https://cninfo.test",
            },
        }
        with patch(
            "monitor.fetch_sse", return_value=([wuzhou], [{"source": "sse"}])
        ), patch(
            "monitor.fetch_szse", return_value=([issued], [{"source": "szse"}])
        ), patch(
            "monitor.fetch_cninfo_market",
            side_effect=[([], [{"source": "cninfo-sh"}]), ([runhe_registration], [{"source": "cninfo-sz"}])],
        ), patch(
            "monitor.cninfo_company_documents", return_value=([], {"query": "ok"})
        ), patch(
            "monitor.atomic_write_json"
        ):
            entries, raw = backfill_pending_entries(
                HttpClient(1, 1),
                config,
                dt.datetime(2026, 9, 8, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                Path("ignored-test-output"),
            )
        self.assertIn("600368", entries)
        self.assertNotIn("300727", entries)
        self.assertEqual(entries["600368"]["company"], "五洲交通")
        self.assertEqual(
            set(raw["registrationSearch"]),
            {"sse", "szse", "cninfo_sh", "cninfo_sz"},
        )

    def test_force_inventory_refresh_overrides_recent_refresh_timestamp(self) -> None:
        cached = {
            "schemaVersion": 1,
            "lastFullRefreshAt": "2026-09-08T09:00:00+08:00",
            "entries": {"old": {"code": "old"}},
        }
        rebuilt = {
            "600368": {
                "code": "600368",
                "company": "五洲交通",
                "registrationDate": "2026-08-21",
                "registeredAmountYuan": 1,
            }
        }
        config = {"inventory": {"fullRefreshDays": 7}}
        with patch("monitor.read_json", return_value=cached), patch(
            "monitor.backfill_pending_entries", return_value=(rebuilt, {})
        ) as backfill, patch("monitor.apply_inventory_events"), patch(
            "monitor.fetch_company_profile",
            return_value={"status": "error", "missingFields": ["industry"]},
        ), patch("monitor.fetch_market_snapshot", side_effect=RuntimeError("test")), patch(
            "monitor.atomic_write_json"
        ), patch(
            "monitor.previous_market_day", return_value=dt.date(2026, 9, 7)
        ):
            inventory, _ = __import__("monitor").refresh_pending_inventory(
                HttpClient(1, 1),
                config,
                dt.datetime(2026, 9, 8, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                [],
                "force-test",
                True,
                True,
            )
        backfill.assert_called_once()
        self.assertIn("600368", inventory["entries"])
        self.assertNotIn("old", inventory["entries"])

    def test_inventory_backfill_degrades_when_cninfo_is_unavailable(self) -> None:
        wuzhou = Announcement(
            source="sse",
            market="SSE",
            code="600368",
            company="五洲交通",
            title="五洲交通关于向不特定对象发行可转换公司债券申请获得中国证监会同意注册批复的公告",
            date="2026-08-21",
            url="https://www.sse.com.cn/wuzhou.pdf",
            source_id="wuzhou",
            stage="registration",
            reason="test",
        )
        config = {
            "maxPages": 8,
            "inventory": {"lookbackDays": 370, "approvalValidityDays": 365},
            "officialSources": {
                "sseAnnouncements": "https://sse.test",
                "szseAnnouncements": "https://szse.test",
                "cninfoAnnouncements": "https://cninfo.test",
            },
        }
        with patch("monitor.fetch_sse", return_value=([wuzhou], [])), patch(
            "monitor.fetch_szse", return_value=([], [])
        ), patch("monitor.fetch_cninfo_market", side_effect=TimeoutError("cninfo down")), patch(
            "monitor.cninfo_company_documents", side_effect=TimeoutError("cninfo down")
        ), patch("monitor.atomic_write_json"):
            entries, raw = backfill_pending_entries(
                HttpClient(1, 1),
                config,
                dt.datetime(2026, 9, 8, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                Path("ignored-test-output"),
            )
        self.assertIn("600368", entries)
        self.assertGreaterEqual(len(raw["warnings"]), 3)

    def test_hearing_draft_does_not_remove_registered_company_from_inventory(self) -> None:
        registration = Announcement(
            source="sse",
            market="SSE",
            code="600368",
            company="五洲交通",
            title="五洲交通关于向不特定对象发行可转换公司债券申请获得中国证监会同意注册批复的公告",
            date="2026-08-21",
            url="https://www.sse.com.cn/registration.pdf",
            source_id="registration",
            stage="registration",
            reason="test",
        )
        config = {
            "maxPages": 8,
            "inventory": {"lookbackDays": 370, "approvalValidityDays": 365},
            "officialSources": {
                "sseAnnouncements": "https://sse.test",
                "szseAnnouncements": "https://szse.test",
                "cninfoAnnouncements": "https://cninfo.test",
            },
        }
        hearing_draft = {
            "code": "600368",
            "company": "五洲交通",
            "title": "向不特定对象发行可转换公司债券募集说明书（上会稿）",
            "date": "2026-09-08",
            "url": "https://static.cninfo.com.cn/hearing-draft.pdf",
        }
        with patch("monitor.fetch_sse", return_value=([registration], [])), patch(
            "monitor.fetch_szse", return_value=([], [])
        ), patch("monitor.fetch_cninfo_market", return_value=([], [])), patch(
            "monitor.cninfo_company_documents", return_value=([hearing_draft], {})
        ), patch("monitor.atomic_write_json"):
            entries, raw = backfill_pending_entries(
                HttpClient(1, 1),
                config,
                dt.datetime(2026, 9, 9, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                Path("ignored-test-output"),
            )
        self.assertIn("600368", entries)
        self.assertEqual(raw["companyChecks"]["600368"]["formalDocuments"], [])

    def test_full_refresh_preserves_verified_enrichment_for_same_event(self) -> None:
        rebuilt = {
            "600368": {
                "code": "600368",
                "registrationDate": "2026-08-21",
                "registeredAmountYuan": None,
            }
        }
        previous = {
            "600368": {
                "code": "600368",
                "registrationDate": "2026-08-21",
                "registeredAmountYuan": 3_000_000_000,
                "officialTotalShares": 1_609_653_858,
                "companyProfile": {"status": "complete"},
                "lastMetrics": {"asOfDate": "2026-09-07"},
            }
        }
        preserve_inventory_enrichment(rebuilt, previous)
        self.assertEqual(rebuilt["600368"]["registeredAmountYuan"], 3_000_000_000)
        self.assertEqual(rebuilt["600368"]["officialTotalShares"], 1_609_653_858)
        self.assertEqual(rebuilt["600368"]["lastMetrics"]["asOfDate"], "2026-09-07")

    def test_market_days_exclude_registration_day_and_weekend(self) -> None:
        self.assertEqual(
            market_days_between(dt.date(2026, 8, 27), dt.date(2026, 8, 31)),
            2,
        )

    def test_inventory_metrics_and_mobile_table(self) -> None:
        entry = {
            "code": "300727",
            "company": "润禾材料",
            "registrationDate": "2026-08-27",
            "registeredAmountYuan": 370_000_000,
        }
        market = {
            "totalShares": 180_000_000,
            "latestClose": 11.0,
            "warnings": [],
            "history": [
                {
                    "date": f"2026-08-{day:02d}",
                    "volumeLots": 1,
                    "turnoverYuan": 1_200 if day == 20 else 1_000,
                }
                for day in range(1, 21)
            ],
        }
        history = {
            "provider": "test",
            "url": "https://example.test/history",
            "warnings": [],
            "rows": [
                {"date": "2026-08-27", "close": 10.0},
                {"date": "2026-08-28", "close": 12.0},
                {"date": "2026-08-31", "close": 11.0},
            ],
        }
        company_profile = {
            "status": "complete",
            "industry": "其他化学制品",
            "mainBusiness": "有机硅深加工产品",
            "mainBusinessReportDate": "2026-06-30",
        }
        metrics = calculate_inventory_metrics(
            entry, market, history, dt.date(2026, 8, 31), company_profile
        )
        self.assertEqual(metrics["tradingDaysElapsed"], 2)
        self.assertEqual(metrics["approvalDaysRemainingEstimate"], 361)
        self.assertEqual(metrics["cumulativeChangePct"], 10.0)
        self.assertEqual(metrics["drawdownFromHighPct"], -8.33)
        self.assertEqual(metrics["stockToConversionPricePct"], 91.67)
        self.assertEqual(metrics["sharesForOneLot"], 486)
        self.assertEqual(metrics["industry"], "其他化学制品")
        self.assertEqual(metrics["mainBusiness"], "有机硅深加工产品")
        entry["lastMetrics"] = metrics
        body = build_pending_inventory_html(
            {"entries": {"300727": entry}, "metricsAsOfDate": "2026-08-31"},
            [],
        )
        self.assertIn("每百元含权", body)
        self.assertIn("行业/<br>主营业务", body)
        self.assertIn("其他化学制品<br>有机硅深加工产品", body)
        self.assertIn("注册后涨跌", body)
        self.assertIn("正股现价/<br>转股价", body)
        self.assertIn("配售1手（10张）", body)
        self.assertLess(body.index("股票</th>"), body.index("行业/<br>主营业务"))
        self.assertLess(body.index("行业/<br>主营业务"), body.index("每百元含权"))
        self.assertLess(body.index("每百元含权"), body.index("正股现价/<br>转股价"))
        self.assertLess(body.index("正股现价/<br>转股价"), body.index("注册日/已过"))
        self.assertIn("overflow-x:auto", body)

    def test_company_profile_uses_leaf_industry_and_latest_top_product(self) -> None:
        survey = {
            "jbzl": [
                {
                    "EM2016": "基础化工-化学制品-非常长的其他化学制品分类",
                    "INDUSTRYCSRC1": "制造业",
                }
            ]
        }
        business = {
            "zygcfx": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "MAINOP_TYPE": "2",
                    "ITEM_NAME": "旧产品",
                    "MBI_RATIO": 0.9,
                    "RANK": 1,
                },
                {
                    "REPORT_DATE": "2026-06-30",
                    "MAINOP_TYPE": "1",
                    "ITEM_NAME": "业务分类",
                    "MBI_RATIO": 0.95,
                    "RANK": 1,
                },
                {
                    "REPORT_DATE": "2026-06-30",
                    "MAINOP_TYPE": "2",
                    "ITEM_NAME": "有机硅深加工产品及相关材料",
                    "MBI_RATIO": 0.62,
                    "RANK": 2,
                },
                {
                    "REPORT_DATE": "2026-06-30",
                    "MAINOP_TYPE": "2",
                    "ITEM_NAME": "其他产品",
                    "MBI_RATIO": 0.38,
                    "RANK": 3,
                },
            ]
        }
        profile = parse_company_profile(survey, business)
        self.assertEqual(profile["status"], "complete")
        self.assertEqual(profile["industry"], "非常长的其他化学制品")
        self.assertEqual(profile["mainBusiness"], "有机硅深加工产品及相")
        self.assertEqual(profile["mainBusinessReportDate"], "2026-06-30")
        self.assertLessEqual(len(profile["industry"]), 10)
        self.assertLessEqual(len(profile["mainBusiness"]), 10)

    def test_company_profile_falls_back_to_quote_industry(self) -> None:
        profile = parse_company_profile(
            {},
            {
                "zygcfx": [
                    {
                        "REPORT_DATE": "2026-06-30",
                        "MAINOP_TYPE": "2",
                        "ITEM_NAME": "矿用橡胶耐磨备件",
                        "MBI_RATIO": 0.64,
                        "RANK": 1,
                    }
                ]
            },
            {"data": {"f127": "通用设备"}},
        )
        self.assertEqual(profile["status"], "complete")
        self.assertEqual(profile["industry"], "通用设备")
        self.assertEqual(profile["mainBusiness"], "矿用橡胶耐磨备件")

    def test_company_profile_is_first_calculation_data_row(self) -> None:
        calculation = registration_calculation(
            370_000_000,
            180_136_352,
            29.12,
            {
                "referenceConversionPrice": 30.0,
                "referenceConversionPrice20DayAverage": 29.5,
                "referenceConversionPricePreviousDayAverage": 30.0,
                "stockToConversionPricePct": 97.07,
                "referenceConversionPriceAsOfDate": "2026-08-26",
            },
        )
        body = calculation_html(
            "300727",
            "润禾材料",
            calculation,
            {
                "documentLabel": "募集说明书（注册稿）",
                "documentDate": "2026-08-27",
                "url": "https://example.test/prospectus.pdf",
            },
            {"asOf": "2026-08-26", "verification": "测试核验"},
            {
                "latestCloseDate": "2026-08-26",
                "return10Pct": 1.0,
                "return20Pct": 2.0,
                "dailyVolatility20Pct": 3.0,
                "historyUrl": "https://example.test/history",
                "quoteUrl": "https://example.test/quote",
                "provider": "测试行情",
            },
            {
                "industry": "其他化学制品",
                "mainBusiness": "有机硅深加工产品",
                "mainBusinessReportDate": "2026-06-30",
            },
        )
        header_end = body.index("</tr>")
        profile_row = body.index("行业｜主营业务")
        amount_row = body.index("募集规模上限")
        self.assertLess(header_end, profile_row)
        self.assertLess(profile_row, amount_row)
        self.assertIn("其他化学制品<br>有机硅深加工产品", body)

    def test_exchange_and_cninfo_are_grouped_once(self) -> None:
        records = [
            Announcement(
                source="szse",
                market="SZSE",
                code="002997",
                company="瑞鹄模具",
                title="瑞鹄模具关于向不特定对象发行可转换公司债券申请获得中国证监会同意注册批复的公告",
                date="2026-08-21",
                url="https://www.szse.cn/a.pdf",
                source_id="a",
                stage="registration",
            ),
            Announcement(
                source="cninfo",
                market="SZSE",
                code="002997",
                company="瑞鹄模具",
                title="关于向不特定对象发行可转换公司债券申请获得中国证监会同意注册批复的公告",
                date="2026-08-21",
                url="https://static.cninfo.com.cn/a.pdf",
                source_id="b",
                stage="registration",
            ),
        ]
        groups = group_candidates(records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["crossValidation"]["layer1"], "matched")
        self.assertEqual(groups[0]["crossValidation"]["layer2"], "matched")


if __name__ == "__main__":
    unittest.main()
