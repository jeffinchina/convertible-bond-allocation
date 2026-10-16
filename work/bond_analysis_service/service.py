from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SERVICE_DIR = Path(__file__).resolve().parent
HYBRID_DIR = SERVICE_DIR.parent / "hybrid_monitor"
REPORT_DIR = SERVICE_DIR / "reports"
CONFIG_PATH = HYBRID_DIR / "config.json"
TIMEZONE = ZoneInfo("Asia/Shanghai")
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
# Fixed identifier embedded by Eastmoney's public quote web client.  It is an
# unauthenticated request parameter, not a user/account credential.
EASTMONEY_PUBLIC_SUGGEST_CLIENT_ID = "D43BF722C8E33BDC906FB84D85E326E8"

if str(HYBRID_DIR) not in sys.path:
    sys.path.insert(0, str(HYBRID_DIR))

from monitor import (  # noqa: E402
    HttpClient,
    atomic_write_json,
    enrich_registration_evidence,
    read_json,
)


ANALYSIS_LOCK = threading.Lock()


def normalize_query(value: str) -> str:
    text = re.sub(r"\s+", "", value or "")
    text = re.sub(r"^(请|麻烦)?帮我分析", "", text)
    text = re.sub(r"^(请|麻烦)?分析", "", text)
    text = re.sub(r"(的)?(可转债)?配债(关键)?数据(分析|报告)?[。！!？?]*$", "", text)
    text = re.sub(r"[。！!？?]+$", "", text)
    if not text:
        raise ValueError("请输入 A 股股票名称或六位证券代码。")
    return text


def resolve_security(client: HttpClient, query: str) -> dict[str, str]:
    normalized = normalize_query(query)
    url = SUGGEST_URL + "?" + urllib.parse.urlencode(
        {
            "input": normalized,
            "type": "14",
            "token": EASTMONEY_PUBLIC_SUGGEST_CLIENT_ID,
        }
    )
    payload = client.get_json(url, headers={"Referer": "https://quote.eastmoney.com/"})
    table = payload.get("QuotationCodeTable") or {}
    candidates = []
    for row in table.get("Data") or []:
        code = str(row.get("Code") or "")
        name = str(row.get("Name") or "")
        if not re.fullmatch(r"\d{6}", code) or row.get("Classify") != "AStock":
            continue
        candidates.append(
            {
                "code": code,
                "company": name,
                "market": "SSE" if code.startswith(("6", "68", "9")) else "SZSE",
                "quoteId": str(row.get("QuoteID") or ""),
                "resolverUrl": url,
            }
        )
    exact = [
        item
        for item in candidates
        if normalized in (item["code"], item["company"])
    ]
    matches = exact or candidates
    if not matches:
        raise ValueError(f"没有找到 A 股股票“{normalized}”，请改用六位证券代码。")
    if len(matches) > 1:
        names = "、".join(f"{item['company']}({item['code']})" for item in matches[:5])
        raise ValueError(f"输入存在多个匹配：{names}。请改用六位证券代码。")
    return matches[0]


def report_document(result: dict[str, Any]) -> str:
    security = result["security"]
    generated_at = result["generatedAt"]
    if result["status"] == "complete":
        calculation = result["calculation"]
        body = calculation["html"]
        warnings = calculation.get("warnings") or []
        warning_html = ""
        if warnings:
            warning_html = "<h3>数据源提示</h3><ul>" + "".join(
                f"<li>{html.escape(str(item))}</li>" for item in warnings
            ) + "</ul>"
        summary = (
            f"<p><strong>结论：</strong>已完成 {html.escape(security['company'])}"
            f"（{html.escape(security['code'])}）正式发行前配债关键数据分析。"
            "计算基于最新可得申报文件的募集规模上限，正式发行后必须用实际发行规模和每股配售额重算。</p>"
        )
        content = summary + body + warning_html
    else:
        calculation = result["calculation"]
        missing = "、".join(calculation.get("missingFields") or ["未知关键字段"])
        warnings = "；".join(calculation.get("warnings") or [])
        content = (
            "<h2>报告未生成</h2>"
            f"<p><strong>缺失字段：</strong>{html.escape(missing)}</p>"
            f"<p><strong>补证信息：</strong>{html.escape(warnings or '没有可用的补充信息')}</p>"
            "<p>完整性闸门已阻止输出不具备数据基础的配债计算。</p>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(security['company'])}配债关键数据分析</title>
<style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;max-width:960px;margin:32px auto;padding:0 20px;line-height:1.65;color:#202124}}
h1,h2,h3{{color:#17324d}} table{{border-collapse:collapse;width:100%;margin:14px 0}} th{{background:#edf4fa}}
th,td{{border:1px solid #b9c6d2;padding:8px;text-align:left}} .meta{{color:#5f6368}} .actions{{margin:18px 0}}
a.button{{display:inline-block;padding:8px 14px;background:#1769aa;color:white;text-decoration:none;border-radius:5px}}
</style></head><body>
<h1>{html.escape(security['company'])}（{html.escape(security['code'])}）配债关键数据分析</h1>
<p class="meta">生成时间：{html.escape(generated_at)}（北京时间）｜确定性脚本取数与计算</p>
<div class="actions"><a class="button" href="/">分析另一只股票</a></div>
{content}
</body></html>"""


def analyze_security(query: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
    config = read_json(CONFIG_PATH, {})
    client = HttpClient(
        int(config.get("sourceTimeoutSeconds", 35)),
        int(config.get("sourceRetries", 3)),
    )
    as_of = now or dt.datetime.now(TIMEZONE)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=TIMEZONE)
    security = resolve_security(client, query)
    report_id = f"{as_of.strftime('%Y%m%dT%H%M%S%f')}_{security['code']}"
    artifact_dir = REPORT_DIR / report_id / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate = {
        "code": security["code"],
        "company": security["company"],
        "market": security["market"],
        "stage": "registration",
        "date": as_of.date().isoformat(),
    }
    with ANALYSIS_LOCK:
        calculation = enrich_registration_evidence(
            client, config, candidate, as_of, artifact_dir
        )
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "status": calculation["status"],
        "query": query,
        "security": security,
        "generatedAt": as_of.isoformat(),
        "calculation": calculation,
        "reportId": report_id,
    }
    report_path = REPORT_DIR / report_id / "report.html"
    json_path = REPORT_DIR / report_id / "report.json"
    result["reportPath"] = str(report_path.resolve())
    result["jsonPath"] = str(json_path.resolve())
    atomic_write_json(json_path, result)
    report_path.write_text(report_document(result), encoding="utf-8")
    return result


def home_document(message: str = "") -> str:
    notice = f'<p style="color:#b3261e">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>配债关键数据分析</title>
<style>body{{font-family:"Microsoft YaHei",Arial,sans-serif;max-width:680px;margin:70px auto;padding:0 20px;color:#202124}}
form{{display:flex;gap:10px}}input{{flex:1;padding:12px;font-size:17px}}button{{padding:10px 18px;background:#1769aa;color:#fff;border:0;border-radius:5px;font-size:16px}}
.hint{{color:#5f6368;line-height:1.7}}</style></head><body>
<h1>配债关键数据分析</h1>{notice}
<form method="post" action="/analyze"><input name="query" autofocus required placeholder="输入股票名称或六位代码"><button type="submit">生成报告</button></form>
<p class="hint">示例：四方科技、603339，或“帮我分析四方科技的配债数据”。<br>报告会自动获取注册额度、总股本和最近行情，并按每日监控相同口径计算。</p>
</body></html>"""


class AnalysisHandler(BaseHTTPRequestHandler):
    server_version = "BondAnalysisService/1.2.7"

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, value: Any) -> None:
        self.send_bytes(
            status,
            json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(HTTPStatus.OK, home_document().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/health":
            self.send_json(HTTPStatus.OK, {"ok": True, "service": "bond-analysis", "version": 3})
            return
        match = re.fullmatch(r"/reports/([A-Za-z0-9_]+)/report\.html", parsed.path)
        if match:
            target = REPORT_DIR / match.group(1) / "report.html"
            if target.exists():
                self.send_bytes(HTTPStatus.OK, target.read_bytes(), "text/html; charset=utf-8")
                return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def read_form(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求正文为空或过大。")
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 请求必须是对象。")
            return value
        form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in form.items()}

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/analyze", "/api/analyze"):
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        wants_json = parsed.path == "/api/analyze"
        try:
            payload = self.read_form()
            result = analyze_security(str(payload.get("query") or ""))
            if wants_json:
                self.send_json(HTTPStatus.OK, {"ok": True, **result})
            else:
                location = f"/reports/{result['reportId']}/report.html"
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", location)
                self.end_headers()
        except ValueError as exc:
            if wants_json:
                self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": str(exc)})
            else:
                self.send_bytes(HTTPStatus.UNPROCESSABLE_ENTITY, home_document(str(exc)).encode("utf-8"), "text/html; charset=utf-8")
        except Exception as exc:
            if wants_json:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            else:
                self.send_bytes(HTTPStatus.INTERNAL_SERVER_ERROR, home_document(f"分析失败：{exc}").encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:
        message = f"{self.log_date_time_string()} {self.address_string()} {format % args}\n"
        (SERVICE_DIR / "service.log").open("a", encoding="utf-8").write(message)


def serve(host: str, port: int) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), AnalysisHandler)
    print(json.dumps({"ok": True, "url": f"http://{host}:{port}/"}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="可转债配债关键数据本地微服务")
    parser.add_argument("--serve", action="store_true", help="启动本地 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--analyze", help="一次性分析股票名称、代码或固定范式指令")
    parser.add_argument("--open", action="store_true", dest="open_report", help="完成后打开 HTML 报告")
    args = parser.parse_args()
    if args.serve:
        serve(args.host, args.port)
        return 0
    if args.analyze:
        result = analyze_security(args.analyze)
        print(json.dumps({"ok": True, "status": result["status"], "reportPath": result["reportPath"], "jsonPath": result["jsonPath"]}, ensure_ascii=False))
        if args.open_report and os.name == "nt":
            os.startfile(result["reportPath"])
        return 0 if result["status"] == "complete" else 2
    parser.error("请指定 --serve 或 --analyze")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
