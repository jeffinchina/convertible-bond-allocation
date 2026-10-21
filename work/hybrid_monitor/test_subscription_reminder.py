import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from monitor import (
    build_subscription_reminder_html,
    parse_company_concepts,
    parse_formal_terms,
    queue_subscription_evidence_paths,
    refresh_subscription_reminder_queue,
    run_subscription_reminders,
    subscription_reminder_due_slots,
    subscription_reminder_subject,
)


TZ = ZoneInfo("Asia/Shanghai")


def sample_evidence(subscription_date: str = "2026-10-20") -> dict:
    return {
        "candidate": {
            "stage": "formal",
            "code": "301018",
            "company": "申菱环境",
            "date": "2026-10-16",
            "eventKey": "formal-event",
            "primary": {
                "title": "申菱环境向不特定对象发行可转换公司债券发行公告",
                "url": "https://example.test/formal.pdf",
            },
        },
        "downloads": [],
        "calculation": {
            "status": "complete",
            "formalTerms": {
                "finalAmount": {"valueYuan": 1_000_000_000},
                "initialConversionPrice": 29.12,
                "recordDate": "2026-10-19",
                "subscriptionDate": subscription_date,
                "bondShortName": "申菱转债",
                "bondCode": "123290",
                "subscriptionShortName": "申菱发债",
                "subscriptionCode": "371018",
                "allocationShortName": "申菱配债",
                "allocationCode": "381018",
                "creditRating": "AA",
                "termYears": 6,
            },
            "marketSnapshot": {
                "securityName": "申菱环境",
                "latestClose": 31.88,
                "latestCloseDate": "2026-10-19",
            },
            "companyProfile": {
                "industry": "制冷空调设备",
                "mainBusiness": "特种空调",
            },
            "companyConcepts": {
                "provider": "东方财富F10核心题材",
                "concepts": ["液冷服务器", "空气能热泵", "东数西算"],
            },
            "results": {
                "stockToConversionPricePct": 109.48,
                "conversionValue": 109.48,
            },
            "source": {
                "title": "申菱环境可转债发行公告",
                "url": "https://example.test/formal.pdf",
            },
        },
    }


class SubscriptionReminderTests(unittest.TestCase):
    def test_formal_terms_include_actionable_subscription_fields(self):
        terms = parse_formal_terms(
            "本次发行的可转换公司债券简称为‘申菱转债’，债券代码为‘123290’。"
            "申购简称为‘申菱发债’，申购代码为‘371018’。"
            "配售简称为‘申菱配债’，配售代码为‘381018’。"
            "本次可转换公司债券信用级别为AA。"
            "本次发行的可转换公司债券的期限为自发行之日起六年。"
            "网上申购日：2026年10月20日。"
        )
        self.assertEqual(terms["bondShortName"], "申菱转债")
        self.assertEqual(terms["bondCode"], "123290")
        self.assertEqual(terms["subscriptionShortName"], "申菱发债")
        self.assertEqual(terms["subscriptionCode"], "371018")
        self.assertEqual(terms["allocationShortName"], "申菱配债")
        self.assertEqual(terms["allocationCode"], "381018")
        self.assertEqual(terms["creditRating"], "AA")
        self.assertEqual(terms["termYears"], 6)

    def test_concepts_only_use_ranked_precise_boards(self):
        concepts = parse_company_concepts(
            {
                "ssbk": [
                    {"BOARD_NAME": "机械设备", "IS_PRECISE": None, "BOARD_RANK": 1},
                    {"BOARD_NAME": "东数西算", "IS_PRECISE": "1", "BOARD_RANK": 4},
                    {"BOARD_NAME": "液冷服务器", "IS_PRECISE": "1", "BOARD_RANK": 2},
                    {"BOARD_NAME": "融资融券", "IS_PRECISE": "0", "BOARD_RANK": 3},
                ]
            }
        )
        self.assertEqual(concepts, ["液冷服务器", "东数西算"])

    def test_due_slots_obey_boundaries_catch_up_and_cutoff(self):
        entry = {"subscriptionDate": "2026-10-20", "sentSlots": {}}
        self.assertEqual(
            subscription_reminder_due_slots(
                entry, dt.datetime(2026, 10, 20, 8, 49, tzinfo=TZ)
            ),
            [],
        )
        self.assertEqual(
            subscription_reminder_due_slots(
                entry, dt.datetime(2026, 10, 20, 8, 50, tzinfo=TZ)
            ),
            ["08:50"],
        )
        self.assertEqual(
            subscription_reminder_due_slots(
                entry, dt.datetime(2026, 10, 20, 9, 50, tzinfo=TZ)
            ),
            ["08:50", "09:50"],
        )
        self.assertEqual(
            subscription_reminder_due_slots(
                entry, dt.datetime(2026, 10, 20, 15, 0, tzinfo=TZ)
            ),
            [],
        )
        self.assertEqual(
            subscription_reminder_due_slots(
                entry, dt.datetime(2026, 10, 21, 9, 50, tzinfo=TZ)
            ),
            [],
        )

    def test_queue_upsert_preserves_sent_slots(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            evidence_path.write_text(
                json.dumps(sample_evidence(), ensure_ascii=False), encoding="utf-8"
            )
            queue = {"schemaVersion": 1, "entries": {}}
            now = dt.datetime(2026, 10, 19, 8, 30, tzinfo=TZ)
            refresh_subscription_reminder_queue(queue, [evidence_path], now)
            entry = next(iter(queue["entries"].values()))
            entry["sentSlots"] = {"08:50": {"sentAt": now.isoformat()}}
            payload = sample_evidence()
            payload["calculation"].pop("companyConcepts")
            evidence_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            refresh_subscription_reminder_queue(queue, [evidence_path], now)
            entry = next(iter(queue["entries"].values()))
            self.assertIn("08:50", entry["sentSlots"])
            self.assertEqual(entry["concepts"][0], "液冷服务器")
            self.assertEqual(len(queue["entries"]), 1)

    def test_incomplete_calculation_and_term_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            queue = {"schemaVersion": 1, "entries": {}}
            now = dt.datetime(2026, 10, 19, 8, 30, tzinfo=TZ)

            incomplete = sample_evidence()
            incomplete["calculation"]["status"] = "incomplete"
            evidence_path.write_text(
                json.dumps(incomplete, ensure_ascii=False), encoding="utf-8"
            )
            result = refresh_subscription_reminder_queue(queue, [evidence_path], now)
            self.assertEqual(queue["entries"], {})
            self.assertIn(
                "formalCalculationIncomplete",
                result["incomplete"][0]["missingFields"],
            )

            conflicted = sample_evidence()
            conflicted["calculation"]["formalTerms"]["conflicts"] = [
                {"field": "subscriptionDate"}
            ]
            evidence_path.write_text(
                json.dumps(conflicted, ensure_ascii=False), encoding="utf-8"
            )
            result = refresh_subscription_reminder_queue(queue, [evidence_path], now)
            self.assertEqual(queue["entries"], {})
            self.assertIn(
                "formalTermConflict", result["incomplete"][0]["missingFields"]
            )

    def test_corrected_event_supersedes_old_queue_entry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            queue = {"schemaVersion": 1, "entries": {}}
            now = dt.datetime(2026, 10, 19, 8, 30, tzinfo=TZ)
            old_payload = sample_evidence("2026-10-20")
            old_payload["calculation"]["formalTerms"]["subscriptionCode"] = "371019"
            evidence_path.write_text(
                json.dumps(old_payload, ensure_ascii=False), encoding="utf-8"
            )
            refresh_subscription_reminder_queue(queue, [evidence_path], now)
            old_key = next(iter(queue["entries"]))

            corrected_payload = sample_evidence("2026-10-21")
            corrected_payload["calculation"].pop("companyConcepts")
            evidence_path.write_text(
                json.dumps(corrected_payload, ensure_ascii=False), encoding="utf-8"
            )
            refresh_subscription_reminder_queue(queue, [evidence_path], now)
            active = [
                entry
                for entry in queue["entries"].values()
                if entry.get("status") == "active"
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["subscriptionDate"], "2026-10-21")
            self.assertEqual(active[0]["concepts"][0], "液冷服务器")
            old_entry = queue["entries"][old_key]
            self.assertEqual(old_entry["status"], "superseded")
            self.assertEqual(old_entry["supersededBy"], active[0]["reminderKey"])
            self.assertEqual(
                subscription_reminder_due_slots(
                    old_entry, dt.datetime(2026, 10, 20, 9, 50, tzinfo=TZ)
                ),
                [],
            )
            # A later rebuild may encounter both the stale and corrected evidence
            # again.  The final corrected entry must be reactivated without
            # inheriting the old supersededBy marker.
            old_path = Path(temp_dir) / "old-evidence.json"
            new_path = Path(temp_dir) / "new-evidence.json"
            old_path.write_text(
                json.dumps(old_payload, ensure_ascii=False), encoding="utf-8"
            )
            new_path.write_text(
                json.dumps(corrected_payload, ensure_ascii=False), encoding="utf-8"
            )
            refresh_subscription_reminder_queue(queue, [old_path, new_path], now)
            active = [
                entry
                for entry in queue["entries"].values()
                if entry.get("status") == "active"
            ]
            self.assertEqual(len(active), 1)
            self.assertNotIn("supersededBy", active[0])
            self.assertEqual(
                subscription_reminder_due_slots(
                    active[0], dt.datetime(2026, 10, 21, 8, 50, tzinfo=TZ)
                ),
                ["08:50"],
            )

    def test_send_two_distinct_batches_once_and_persist_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence" / "day" / "run" / "event" / "evidence.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(sample_evidence(), ensure_ascii=False), encoding="utf-8"
            )
            sent = []

            def fake_send(config, subject, body_file):
                sent.append((subject, body_file.read_text(encoding="utf-8")))
                return {"ok": True, "duplicate": False}

            kwargs = {
                "queue_path": root / "queue.json",
                "lock_path": root / "queue.lock",
                "evidence_dir": root / "evidence",
                "reports_dir": root / "reports",
                "send_func": fake_send,
            }
            (root / "queue.json").write_text(
                json.dumps(self._queue_for(sample_evidence()), ensure_ascii=False),
                encoding="utf-8",
            )
            now = dt.datetime(2026, 10, 20, 9, 50, tzinfo=TZ)
            first = run_subscription_reminders({}, now, **kwargs)
            second = run_subscription_reminders({}, now, **kwargs)
            self.assertTrue(first["ok"])
            self.assertEqual([item["slot"] for item in first["actions"]], ["08:50", "09:50"])
            self.assertEqual(second["actions"], [])
            self.assertEqual(len(sent), 2)
            self.assertEqual(
                {subject for subject, _ in sent},
                {"【可转债申购】请今天申购新债：申菱转债"},
            )
            self.assertNotEqual(sent[0][1], sent[1][1])
            self.assertIn("本次提醒批次：</strong>08:50", sent[0][1])
            self.assertIn("本次提醒批次：</strong>09:50", sent[1][1])
            self.assertIn("液冷服务器、空气能热泵、东数西算", sent[0][1])
            queue = json.loads((root / "queue.json").read_text(encoding="utf-8"))
            slots = next(iter(queue["entries"].values()))["sentSlots"]
            self.assertEqual(set(slots), {"08:50", "09:50"})

    def test_delayed_monitor_enqueue_can_immediately_catch_up_due_slots(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence.json"
            evidence.write_text(
                json.dumps(sample_evidence(), ensure_ascii=False), encoding="utf-8"
            )
            queue_path = root / "queue.json"
            lock_path = root / "queue.lock"
            now = dt.datetime(2026, 10, 20, 10, 30, tzinfo=TZ)
            queued = queue_subscription_evidence_paths(
                [evidence], now, queue_path=queue_path, lock_path=lock_path
            )
            sent = []
            delivered = run_subscription_reminders(
                {},
                now,
                queue_path=queue_path,
                lock_path=lock_path,
                reports_dir=root / "reports",
                send_func=lambda _config, subject, body: (
                    sent.append((subject, body.name))
                    or {"ok": True, "duplicate": False}
                ),
            )
            self.assertEqual(queued["addedOrUpdated"], 1)
            self.assertEqual(
                [item["slot"] for item in delivered["actions"]], ["08:50", "09:50"]
            )
            self.assertEqual(len(sent), 2)

    def test_failure_stays_pending_and_duplicate_result_counts_as_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence" / "day" / "run" / "event" / "evidence.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(sample_evidence(), ensure_ascii=False), encoding="utf-8"
            )
            kwargs = {
                "queue_path": root / "queue.json",
                "lock_path": root / "queue.lock",
                "evidence_dir": root / "evidence",
                "reports_dir": root / "reports",
            }
            (root / "queue.json").write_text(
                json.dumps(self._queue_for(sample_evidence()), ensure_ascii=False),
                encoding="utf-8",
            )
            now = dt.datetime(2026, 10, 20, 8, 50, tzinfo=TZ)

            def fail_send(config, subject, body_file):
                raise RuntimeError("smtp unavailable")

            failed = run_subscription_reminders({}, now, send_func=fail_send, **kwargs)
            self.assertFalse(failed["ok"])
            queue = json.loads((root / "queue.json").read_text(encoding="utf-8"))
            self.assertEqual(next(iter(queue["entries"].values()))["sentSlots"], {})

            retried = run_subscription_reminders(
                {},
                now,
                send_func=lambda *_: {"ok": True, "duplicate": True},
                **kwargs,
            )
            self.assertTrue(retried["ok"])
            self.assertTrue(retried["actions"][0]["duplicateAccepted"])

    def test_normal_send_mode_does_not_scan_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            root = Path(temp_dir)
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(self._queue_for(sample_evidence()), ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "monitor.discover_subscription_evidence",
                side_effect=AssertionError("send mode must not scan evidence"),
            ) as discover:
                result = run_subscription_reminders(
                    {},
                    dt.datetime(2026, 10, 20, 8, 49, tzinfo=TZ),
                    queue_path=queue_path,
                    lock_path=root / "queue.lock",
                    evidence_dir=root / "evidence",
                    reports_dir=root / "reports",
                    send_func=lambda *_: {"ok": True, "duplicate": False},
                )
            discover.assert_not_called()
            self.assertTrue(result["refresh"]["skipped"])
            self.assertEqual(result["refresh"]["reason"], "queue_only")

    def test_rebuild_force_reparses_pdf_and_uses_corrected_terms(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence" / "2026-10-18" / "run" / "event" / "evidence.json"
            pdf = evidence.parent / "formal.pdf"
            evidence.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-test")
            payload = sample_evidence("2026-10-19")
            payload["downloads"] = [
                {"path": str(pdf), "pdfValidated": True, "sha256": "pdf-hash"}
            ]
            evidence.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            corrected_text = (
                "本次发行的可转换公司债券简称为‘申菱转债’，债券代码为‘123290’。"
                "申购简称为‘申菱发债’，申购代码为‘371018’。"
                "网上申购日：2026年10月21日。"
            )
            with patch("monitor.pdf_text", return_value=corrected_text) as extract:
                result = run_subscription_reminders(
                    {},
                    dt.datetime(2026, 10, 20, 8, 30, tzinfo=TZ),
                    queue_path=root / "queue.json",
                    lock_path=root / "queue.lock",
                    evidence_dir=root / "evidence",
                    reports_dir=root / "reports",
                    send_due=False,
                    refresh_evidence=True,
                )
            extract.assert_called_once_with(pdf)
            self.assertEqual(result["refresh"]["addedOrUpdated"], 1)
            queue = json.loads((root / "queue.json").read_text(encoding="utf-8"))
            entry = next(iter(queue["entries"].values()))
            self.assertEqual(entry["subscriptionDate"], "2026-10-21")

    def test_polluted_short_name_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            root = Path(temp_dir)
            evidence_path = root / "evidence.json"
            payload = sample_evidence()
            payload["calculation"]["formalTerms"][
                "bondShortName"
            ] = "申菱转债可转债扩位简称申"
            evidence_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            queue = {"schemaVersion": 1, "entries": {}}
            result = refresh_subscription_reminder_queue(
                queue,
                [evidence_path],
                dt.datetime(2026, 10, 19, 8, 30, tzinfo=TZ),
            )
            self.assertEqual(queue["entries"], {})
            self.assertIn("bondShortNameInvalid", result["incomplete"][0]["missingFields"])

    def test_numbered_bond_short_name_can_enter_queue_and_title(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            payload = sample_evidence("2026-10-22")
            payload["candidate"].update(
                {"code": "000887", "company": "中鼎股份", "eventKey": "zhongding"}
            )
            payload["calculation"]["formalTerms"].update(
                {
                    "bondShortName": "中鼎转03",
                    "bondCode": "127119",
                    "subscriptionShortName": "中鼎发债",
                    "subscriptionCode": "070887",
                    "allocationShortName": "中鼎配债",
                    "allocationCode": "080887",
                }
            )
            evidence_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            queue = {"schemaVersion": 1, "entries": {}}
            result = refresh_subscription_reminder_queue(
                queue,
                [evidence_path],
                dt.datetime(2026, 10, 21, 8, 30, tzinfo=TZ),
            )
            self.assertEqual(result["addedOrUpdated"], 1)
            entry = next(iter(queue["entries"].values()))
            self.assertEqual(entry["bondShortName"], "中鼎转03")
            self.assertEqual(
                subscription_reminder_subject(entry),
                "【可转债申购】请今天申购新债：中鼎转03",
            )

    def test_missing_subscription_code_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            payload = sample_evidence()
            payload["calculation"]["formalTerms"]["subscriptionCode"] = None
            evidence_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            queue = {"schemaVersion": 1, "entries": {}}
            result = refresh_subscription_reminder_queue(
                queue,
                [evidence_path],
                dt.datetime(2026, 10, 19, 8, 30, tzinfo=TZ),
            )
            self.assertEqual(queue["entries"], {})
            self.assertIn("subscriptionCode", result["incomplete"][0]["missingFields"])

    def test_html_escapes_values_and_title_is_exact(self):
        entry = next(
            iter(
                self._queue_for(sample_evidence())["entries"].values()
            )
        )
        entry["mainBusiness"] = "制冷<设备>"
        body = build_subscription_reminder_html(entry, "08:50")
        self.assertNotIn("制冷<设备>", body)
        self.assertIn("制冷&lt;设备&gt;", body)
        self.assertEqual(
            subscription_reminder_subject(entry),
            "【可转债申购】请今天申购新债：申菱转债",
        )

    @staticmethod
    def _queue_for(payload: dict) -> dict:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            evidence_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            queue = {"schemaVersion": 1, "entries": {}}
            refresh_subscription_reminder_queue(
                queue,
                [evidence_path],
                dt.datetime(2026, 10, 19, 8, 30, tzinfo=TZ),
            )
            return queue


if __name__ == "__main__":
    unittest.main()
