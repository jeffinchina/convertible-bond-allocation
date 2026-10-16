import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const root = process.cwd();
const outputDir = path.join(root, "outputs", "cb_research_20260827");
const outputPath = path.join(outputDir, "近一年可转债配债股价规律分析.xlsx");
const previewDir = path.join(outputDir, "previews");
await fs.mkdir(previewDir, { recursive: true });


function colLetter(indexZeroBased) {
  let n = indexZeroBased + 1;
  let out = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}


function headersFromCsv(csvText) {
  return csvText
    .split(/\r?\n/, 1)[0]
    .replace(/^\uFEFF/, "")
    .split(",")
    .map((x) => x.replace(/^"|"$/g, ""));
}


function formatDataSheet(sheet, headerCount, rowCount, tableName) {
  const lastCol = colLetter(headerCount - 1);
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(4);
  const used = sheet.getRange(`A1:${lastCol}${rowCount}`);
  used.format.font = { name: "Microsoft YaHei", size: 9 };
  used.format.verticalAlignment = "center";
  sheet.getRange(`A1:${lastCol}1`).format = {
    fill: "#17365D",
    font: { name: "Microsoft YaHei", size: 9, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "outside", style: "thin", color: "#8EA9C1" },
  };
  sheet.getRange(`A1:${lastCol}1`).format.rowHeight = 34;
  const table = sheet.tables.add(`A1:${lastCol}${rowCount}`, true, tableName);
  table.style = "TableStyleMedium2";
  table.showBandedRows = true;
  table.showFilterButton = true;
}


const dataCsv = await fs.readFile(path.join(outputDir, "workbook_data.csv"), "utf8");
const statsCsv = await fs.readFile(path.join(outputDir, "workbook_statistics.csv"), "utf8");
const corrCsv = await fs.readFile(path.join(outputDir, "workbook_correlations.csv"), "utf8");
const groupsCsv = await fs.readFile(path.join(outputDir, "workbook_groups.csv"), "utf8");
const regCsv = await fs.readFile(path.join(outputDir, "workbook_regressions.csv"), "utf8");

const dataHeaders = headersFromCsv(dataCsv);
const dataRows = 70; // one header + 69 observations
const idx = Object.fromEntries(dataHeaders.map((name, i) => [name, i]));
const c = (name) => colLetter(idx[name]);

const workbook = await Workbook.fromCSV("近一年可转债配债股价规律分析\n", { sheetName: "摘要" });
await workbook.fromCSV("方法、口径、来源与检查\n", { sheetName: "方法与来源" });
await workbook.fromCSV(dataCsv, { sheetName: "样本底表" });
await workbook.fromCSV(statsCsv, { sheetName: "统计检验" });
await workbook.fromCSV(corrCsv, { sheetName: "相关性" });
await workbook.fromCSV(groupsCsv, { sheetName: "分组结果" });
await workbook.fromCSV(regCsv, { sheetName: "回归稳健性" });

const summary = workbook.worksheets.getItem("摘要");
const methods = workbook.worksheets.getItem("方法与来源");

const dataSheet = workbook.worksheets.getItem("样本底表");
formatDataSheet(dataSheet, dataHeaders.length + 4, dataRows, "ConvertibleBondSample");

// Add formula-driven audit columns to the curated dataset.
const formulaStart = dataHeaders.length;
const fCols = [
  "含权率复算_发行公告日",
  "含权率复算差异",
  "申购日开盘缺口补偿所需转债涨幅",
  "事件顺序异常",
];
dataSheet.getRangeByIndexes(0, formulaStart, 1, fCols.length).values = [fCols];
const rightsCalcCol = colLetter(formulaStart);
const diffCol = colLetter(formulaStart + 1);
const breakevenCol = colLetter(formulaStart + 2);
const orderCheckCol = colLetter(formulaStart + 3);
dataSheet.getRange(`${rightsCalcCol}2`).formulas = [[`=${c("每股配售额_元")}2/${c("发行公告日收盘_前复权")}2`]];
dataSheet.getRange(`${rightsCalcCol}2:${rightsCalcCol}${dataRows}`).fillDown();
dataSheet.getRange(`${diffCol}2`).formulas = [[`=ABS(${rightsCalcCol}2-${c("含权率_发行公告日")}2)`]];
dataSheet.getRange(`${diffCol}2:${diffCol}${dataRows}`).fillDown();
dataSheet.getRange(`${breakevenCol}2`).formulas = [[`=IF(${c("含权率_登记日")}2>0,-${c("申购日开盘缺口")}2/${c("含权率_登记日")}2,"")`]];
dataSheet.getRange(`${breakevenCol}2:${breakevenCol}${dataRows}`).fillDown();
dataSheet.getRange(`${orderCheckCol}2`).formulas = [[
  `=IF(AND(${c("同意注册公告日")}2<=${c("正式发行公告日")}2,${c("正式发行公告日")}2<=${c("股权登记日")}2,${c("股权登记日")}2<=${c("申购日")}2),0,1)`,
]];
dataSheet.getRange(`${orderCheckCol}2:${orderCheckCol}${dataRows}`).fillDown();

// Semantic number formats.
for (const name of ["同意注册公告日", "正式发行公告日", "股权登记日", "申购日"]) {
  dataSheet.getRange(`${c(name)}2:${c(name)}${dataRows}`).format.numberFormat = "yyyy-mm-dd";
}
for (const name of ["发行规模_亿元", "每股配售额_元", "初始转股价_元", "同意注册日收盘_前复权", "发行公告日收盘_前复权", "登记日收盘_前复权"]) {
  dataSheet.getRange(`${c(name)}2:${c(name)}${dataRows}`).format.numberFormat = "0.00";
}
for (const name of [
  "含权率_发行公告日", "含权率_登记日", "两公告区间收益", "同期中证全指收益", "两公告区间超额",
  "发行公告日前区间超额", "发行公告日开盘缺口", "发行公告日日内收益", "发行公告日收益", "发行公告日超额",
  "登记日开盘缺口", "登记日日内收益", "登记日收益", "登记日超额", "申购日开盘缺口", "申购日日内收益",
  "申购日收益", "申购日超额",
]) {
  dataSheet.getRange(`${c(name)}2:${c(name)}${dataRows}`).format.numberFormat = "0.00%;[Red](0.00%);-";
}
dataSheet.getRange(`${rightsCalcCol}2:${breakevenCol}${dataRows}`).format.numberFormat = "0.00%;[Red](0.00%);-";
dataSheet.getRange(`${diffCol}2:${diffCol}${dataRows}`).format.numberFormat = "0.000000%";
dataSheet.getRange(`${orderCheckCol}2:${orderCheckCol}${dataRows}`).format.numberFormat = "0";

// Column sizing for the wide audit sheet.
dataSheet.getRange("A1:D70").format.columnWidth = 12;
dataSheet.getRange("E1:E70").format.columnWidth = 8;
dataSheet.getRange("F1:H70").format.columnWidth = 13;
dataSheet.getRange("I1:L70").format.columnWidth = 13;
dataSheet.getRange("M1:N70").format.columnWidth = 12;
dataSheet.getRange("O1:AI70").format.columnWidth = 13;
dataSheet.getRange("AJ1:AK70").format.columnWidth = 11;
dataSheet.getRange("AL1:AM70").format.columnWidth = 38;
dataSheet.getRange("AN1:AO70").format.columnWidth = 36;
dataSheet.getRange("AP1:AS70").format.columnWidth = 17;
dataSheet.getRange("AL2:AO70").format.wrapText = true;
dataSheet.getRange(`${c("两公告区间收益")}2:${c("申购日超额")}70`).conditionalFormats.add("colorScale", {
  criteria: [
    { type: "lowestValue", color: "#F4CCCC" },
    { type: "percentile", value: 50, color: "#FFFFFF" },
    { type: "highestValue", color: "#D9EAD3" },
  ],
});

// Summary sheet: formula-driven KPIs and two compact charts.
summary.showGridLines = false;
summary.getRange("A1:H1").merge();
summary.getRange("A1").values = [["近一年可转债配债股价规律分析"]];
summary.getRange("A1:H1").format = {
  fill: "#17365D",
  font: { name: "Microsoft YaHei", size: 18, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
summary.getRange("A1:H1").format.rowHeight = 34;
summary.getRange("A2:H2").merge();
summary.getRange("A2").values = [["样本：2025-08-27 至 2026-08-26 完成申购的 69 只可转债；基准：中证全指；收益口径见“方法与来源”。"]];
summary.getRange("A2:H2").format = {
  fill: "#D9EAF7",
  font: { name: "Microsoft YaHei", size: 10, color: "#17365D" },
  wrapText: true,
};
summary.getRange("A2:H2").format.rowHeight = 28;

const kpis = [
  ["A4:B4", "A5:B5", "样本数", `=COUNTA('样本底表'!$A$2:$A$70)`, "0"],
  ["C4:D4", "C5:D5", "两公告区间上涨概率", `=COUNTIF('样本底表'!$${c("两公告区间收益")}$2:$${c("两公告区间收益")}$70,">0")/COUNTA('样本底表'!$${c("两公告区间收益")}$2:$${c("两公告区间收益")}$70)`, "0.0%"],
  ["E4:F4", "E5:F5", "正式发行公告日上涨概率", `=COUNTIF('样本底表'!$${c("发行公告日收益")}$2:$${c("发行公告日收益")}$70,">0")/COUNTA('样本底表'!$${c("发行公告日收益")}$2:$${c("发行公告日收益")}$70)`, "0.0%"],
  ["G4:H4", "G5:H5", "申购日下跌概率", `=COUNTIF('样本底表'!$${c("申购日收益")}$2:$${c("申购日收益")}$70,"<0")/COUNTA('样本底表'!$${c("申购日收益")}$2:$${c("申购日收益")}$70)`, "0.0%"],
];
for (const [labelRange, valueRange, label, formula, numberFormat] of kpis) {
  summary.getRange(labelRange).merge();
  summary.getRange(labelRange.split(":")[0]).values = [[label]];
  summary.getRange(valueRange).merge();
  summary.getRange(valueRange.split(":")[0]).formulas = [[formula]];
  summary.getRange(labelRange).format = {
    fill: "#D9EAD3",
    font: { name: "Microsoft YaHei", size: 9, bold: true, color: "#274E13" },
    horizontalAlignment: "center",
  };
  summary.getRange(valueRange).format = {
    fill: "#F3F8F1",
    font: { name: "Microsoft YaHei", size: 16, bold: true, color: "#000000" },
    horizontalAlignment: "center",
    numberFormat,
    borders: { preset: "outside", style: "thin", color: "#A9D18E" },
  };
}
summary.getRange("A4:H5").format.rowHeight = 25;

summary.getRange("A8:D8").values = [["事件/区间", "均值", "中位数", "上涨概率"]];
const eventRows = [
  ["两公告区间", "两公告区间收益"],
  ["两公告区间超额", "两公告区间超额"],
  ["正式发行公告日", "发行公告日收益"],
  ["股权登记日", "登记日收益"],
  ["申购日", "申购日收益"],
];
for (let i = 0; i < eventRows.length; i += 1) {
  const row = 9 + i;
  const [label, header] = eventRows[i];
  const col = c(header);
  summary.getRange(`A${row}`).values = [[label]];
  summary.getRange(`B${row}`).formulas = [[`=AVERAGE('样本底表'!$${col}$2:$${col}$70)`]];
  summary.getRange(`C${row}`).formulas = [[`=MEDIAN('样本底表'!$${col}$2:$${col}$70)`]];
  summary.getRange(`D${row}`).formulas = [[`=COUNTIF('样本底表'!$${col}$2:$${col}$70,">0")/COUNTA('样本底表'!$${col}$2:$${col}$70)`]];
}
summary.getRange("A8:D13").format.font = { name: "Microsoft YaHei", size: 10 };
summary.getRange("A8:D8").format = {
  fill: "#4472C4",
  font: { name: "Microsoft YaHei", size: 10, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
};
summary.getRange("B9:D13").format.numberFormat = "0.0%;[Red](0.0%);-";
summary.getRange("A8:D13").format.borders = { preset: "inside", style: "thin", color: "#D9E1F2" };

summary.getRange("A16:C16").values = [["含权率分组", "两公告区间上涨概率", "登记日上涨概率"]];
for (let i = 0; i < 4; i += 1) {
  const row = 17 + i;
  const q = `Q${i + 1}`;
  summary.getRange(`A${row}`).values = [[q]];
  summary.getRange(`B${row}`).formulas = [[
    `=COUNTIFS('样本底表'!$${c("含权率四分位")}$2:$${c("含权率四分位")}$70,$A${row},'样本底表'!$${c("两公告区间收益")}$2:$${c("两公告区间收益")}$70,">0")/COUNTIF('样本底表'!$${c("含权率四分位")}$2:$${c("含权率四分位")}$70,$A${row})`,
  ]];
  summary.getRange(`C${row}`).formulas = [[
    `=COUNTIFS('样本底表'!$${c("含权率四分位")}$2:$${c("含权率四分位")}$70,$A${row},'样本底表'!$${c("登记日收益")}$2:$${c("登记日收益")}$70,">0")/COUNTIF('样本底表'!$${c("含权率四分位")}$2:$${c("含权率四分位")}$70,$A${row})`,
  ]];
}
summary.getRange("A16:C20").format.font = { name: "Microsoft YaHei", size: 10 };
summary.getRange("A16:C16").format = {
  fill: "#70AD47",
  font: { name: "Microsoft YaHei", size: 10, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
};
summary.getRange("B17:C20").format.numberFormat = "0.0%";
summary.getRange("A16:C20").format.borders = { preset: "inside", style: "thin", color: "#E2F0D9" };

const takeaways = [
  "1. 两公告区间有正向倾向，但稳定性一般：均值受极端上涨样本影响，区间超额收益的非参数检验未达到5%显著水平。",
  "2. 最清晰的正向节点是首次正式发行公告日：上涨概率约72%，收益主要来自开盘高开，日内继续上涨并不稳定。",
  "3. 股权登记日没有可靠方向：上涨概率约57%，相对中证全指跑赢概率约52%，均未显著偏离随机。",
  "4. 含权率高并不意味着更容易上涨；四分位胜率不单调，相关性、分组检验和稳健回归均不支持“越高越涨”。",
  "5. 申购日是最强风险点：约87%的样本下跌，且开盘缺口均值约-5.6%；配债收益必须与正股隔夜缺口一起核算。",
];
summary.getRange("A23:H23").merge();
summary.getRange("A23").values = [["结论与交易提示"]];
summary.getRange("A23:H23").format = {
  fill: "#17365D",
  font: { name: "Microsoft YaHei", size: 11, bold: true, color: "#FFFFFF" },
};
for (let i = 0; i < takeaways.length; i += 1) {
  const row = 24 + i * 2;
  summary.getRange(`A${row}:H${row + 1}`).merge();
  summary.getRange(`A${row}`).values = [[takeaways[i]]];
  summary.getRange(`A${row}:H${row + 1}`).format = {
    fill: i % 2 === 0 ? "#F7F9FC" : "#FFFFFF",
    font: { name: "Microsoft YaHei", size: 10, color: "#333333" },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: "#D9E1F2" },
  };
  summary.getRange(`A${row}:H${row + 1}`).format.rowHeight = 24;
}
summary.getRange("A34:H35").merge();
summary.getRange("A34").values = [["提示：本研究是历史统计，不是因果识别或收益承诺。含权率应当用作盈亏平衡输入，而不是单独的看涨评分。"]];
summary.getRange("A34:H35").format = {
  fill: "#FFF2CC",
  font: { name: "Microsoft YaHei", size: 9, italic: true, color: "#7F6000" },
  wrapText: true,
  verticalAlignment: "center",
};

const eventChart = summary.charts.add("bar", summary.getRange("A8:C13"));
eventChart.title = "关键阶段收益：均值与中位数";
eventChart.hasLegend = true;
eventChart.yAxis = { numberFormatCode: "0%" };
eventChart.setPosition("J2", "Q17");

const rightsChart = summary.charts.add("line", summary.getRange("A16:C20"));
rightsChart.title = "含权率分组胜率没有单调关系";
rightsChart.hasLegend = true;
rightsChart.yAxis = { numberFormatCode: "0%", min: 0, max: 1 };
rightsChart.xAxis = { axisType: "textAxis" };
rightsChart.setPosition("J19", "Q34");

summary.getRange("A1:H35").format.columnWidth = 15;
summary.getRange("A1:A35").format.columnWidth = 21;
summary.freezePanes.freezeRows(2);

// Methods, definitions, source URLs, and formula-driven checks.
methods.showGridLines = false;
methods.getRange("A1:F1").merge();
methods.getRange("A1").values = [["方法、口径、来源与检查"]];
methods.getRange("A1:F1").format = {
  fill: "#17365D",
  font: { name: "Microsoft YaHei", size: 16, bold: true, color: "#FFFFFF" },
};
methods.getRange("A1:F1").format.rowHeight = 32;
methods.getRange("A3:F3").values = [["项目", "定义/数值", "单位/期望", "容差", "状态", "说明"]];
methods.getRange("A4:A7").values = [["完整样本数"], ["含权率复算最大差异"], ["事件顺序异常数"], ["正式发行公告日期缺失数"]];
methods.getRange("B4").formulas = [["=COUNTA('样本底表'!$A$2:$A$70)"]];
methods.getRange("B5").formulas = [[`=MAX('样本底表'!$${diffCol}$2:$${diffCol}$70)`]];
methods.getRange("B6").formulas = [[`=SUM('样本底表'!$${orderCheckCol}$2:$${orderCheckCol}$70)`]];
methods.getRange("B7").formulas = [[`=COUNTBLANK('样本底表'!$${c("正式发行公告日")}$2:$${c("正式发行公告日")}$70)`]];
methods.getRange("C4:C7").values = [[69], [0], [0], [0]];
methods.getRange("D4:D7").values = [[0], [1e-10], [0], [0]];
for (let row = 4; row <= 7; row += 1) {
  methods.getRange(`E${row}`).formulas = [[`=IF(ABS(B${row}-C${row})<=D${row},"OK","检查")`]];
}
methods.getRange("F4:F7").values = [
  ["发行清单样本数与分析样本数一致"],
  ["每股配售额÷发行公告日收盘价复算"],
  ["同意注册≤发行公告≤登记日≤申购日"],
  ["69只样本均匹配到首次正式发行公告"],
];
methods.getRange("A3:F3").format = {
  fill: "#4472C4",
  font: { name: "Microsoft YaHei", bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
};
methods.getRange("A3:F7").format.borders = { preset: "inside", style: "thin", color: "#D9E1F2" };
methods.getRange("B5:D5").format.numberFormat = "0.000000%";
methods.getRange("E4:E7").conditionalFormats.add("containsText", { text: "OK", format: { fill: "#C6E0B4", font: { bold: true, color: "#006100" } } });
methods.getRange("E4:E7").conditionalFormats.add("containsText", { text: "检查", format: { fill: "#F4CCCC", font: { bold: true, color: "#9C0006" } } });

methods.getRange("A10:F10").merge();
methods.getRange("A10").values = [["核心口径"]];
methods.getRange("A10:F10").format = { fill: "#17365D", font: { name: "Microsoft YaHei", bold: true, color: "#FFFFFF" } };
const definitions = [
  ["样本范围", "申购日位于2025-08-27至2026-08-26且成功匹配同意注册公告、首次正式发行公告与正股行情的全部69只可转债。"],
  ["两公告区间收益", "同意注册公告日收盘（非交易日取此前最近收盘）至首次正式发行公告可交易日收盘；包含正式发行公告日反应。"],
  ["超额收益", "正股收益减同期中证全指（000985.SH）收益；正股使用前复权日线，指数不复权。"],
  ["含权率", "每股优先配售可转债面值 ÷ 正股收盘价；主分析用正式发行公告日收盘价，避免登记日价格与当日收益的机械相关。"],
  ["统计方法", "均值/中位数/胜率、Wilson胜率区间、Bootstrap均值区间、t检验、Wilcoxon、Spearman、Fisher、Mann-Whitney、BH多重检验校正、HC3稳健标准误回归。"],
  ["显著性解释", "p值<0.05仅表示样本内证据较强；相关关系不等于因果，不代表未来仍会重复。"],
];
methods.getRange("A11:B16").values = definitions;
methods.getRange("A11:A16").format = { fill: "#D9EAF7", font: { name: "Microsoft YaHei", bold: true, color: "#17365D" } };
methods.getRange("B11:B16").format = { font: { name: "Microsoft YaHei", size: 10 }, wrapText: true };
methods.getRange("A11:B16").format.borders = { preset: "inside", style: "thin", color: "#D9E1F2" };
methods.getRange("A11:B16").format.rowHeight = 42;

methods.getRange("A19:F19").merge();
methods.getRange("A19").values = [["数据来源（可复制访问）"]];
methods.getRange("A19:F19").format = { fill: "#17365D", font: { name: "Microsoft YaHei", bold: true, color: "#FFFFFF" } };
methods.getRange("A20:C20").values = [["数据项", "来源", "URL"]];
methods.getRange("A21:C25").values = [
  ["可转债发行清单/配售参数", "东方财富可转债数据中心", "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_BOND_CB_LIST"],
  ["上市公司公告索引", "东方财富公告索引（逐条附公告页）", "https://np-anotice-stock.eastmoney.com/api/security/ann"],
  ["正股与中证全指日线", "东方财富历史K线", "https://push2his.eastmoney.com/api/qt/stock/kline/get"],
  ["上交所可转债公告核验", "上海证券交易所", "https://bond.sse.com.cn/disclosure/announ/convertible/"],
  ["深交所上市公司公告核验", "深圳证券交易所", "https://www.szse.cn/disclosure/listed/notice/"],
];
methods.getRange("A20:C20").format = { fill: "#70AD47", font: { name: "Microsoft YaHei", bold: true, color: "#FFFFFF" } };
methods.getRange("A20:C25").format.borders = { preset: "inside", style: "thin", color: "#E2F0D9" };
methods.getRange("C21:C25").format.font = { name: "Consolas", size: 9, color: "#0563C1" };
methods.getRange("C21:C25").format.wrapText = true;

methods.getRange("A28:F28").merge();
methods.getRange("A28").values = [["限制"]];
methods.getRange("A28:F28").format = { fill: "#7F6000", font: { name: "Microsoft YaHei", bold: true, color: "#FFFFFF" } };
methods.getRange("A29:F31").merge();
methods.getRange("A29").values = [["仅覆盖一个年度和69只发行样本；无法完全剥离行业、公司新闻、盈利预期、规模与市场风格等共同因素。分组后每组约17–23只，检验功效有限。报告不计交易成本、配债取整、实际认购比例和转债上市价格，故不能直接等同于可执行套利收益。"]];
methods.getRange("A29:F31").format = { fill: "#FFF2CC", font: { name: "Microsoft YaHei", size: 10, color: "#7F6000" }, wrapText: true, verticalAlignment: "center" };
methods.getRange("A1:A31").format.columnWidth = 22;
methods.getRange("B1:B31").format.columnWidth = 62;
methods.getRange("C1:C31").format.columnWidth = 50;
methods.getRange("D1:E31").format.columnWidth = 13;
methods.getRange("F1:F31").format.columnWidth = 38;
methods.freezePanes.freezeRows(1);

// Format imported statistical sheets consistently.
const imported = [
  ["统计检验", headersFromCsv(statsCsv).length, statsCsv.trim().split(/\r?\n/).length, "StatsTable"],
  ["相关性", headersFromCsv(corrCsv).length, corrCsv.trim().split(/\r?\n/).length, "CorrelationTable"],
  ["分组结果", headersFromCsv(groupsCsv).length, groupsCsv.trim().split(/\r?\n/).length, "GroupTable"],
  ["回归稳健性", headersFromCsv(regCsv).length, regCsv.trim().split(/\r?\n/).length, "RegressionTable"],
];
for (const [sheetName, headerCount, rowCount, tableName] of imported) {
  const sheet = workbook.worksheets.getItem(sheetName);
  formatDataSheet(sheet, headerCount, rowCount, tableName);
  const lastCol = colLetter(headerCount - 1);
  sheet.getRange(`A1:${lastCol}${rowCount}`).format.columnWidth = 15;
  sheet.getRange(`A1:A${rowCount}`).format.columnWidth = sheetName === "相关性" ? 34 : 26;
  if (sheetName === "回归稳健性") {
    sheet.getRange(`A1:C${rowCount}`).format.columnWidth = 30;
  }
}

const statsSheet = workbook.worksheets.getItem("统计检验");
statsSheet.getRange("C2:J19").format.numberFormat = "0.00%;[Red](0.00%);-";
statsSheet.getRange("K2:L19").format.numberFormat = "0.000";
const corrSheet = workbook.worksheets.getItem("相关性");
corrSheet.getRange("E2:G13").format.numberFormat = "0.000";
corrSheet.getRange("B1:C13").format.columnWidth = 30;
const groupSheet = workbook.worksheets.getItem("分组结果");
groupSheet.getRange("D2:E17").format.numberFormat = "0.00%;[Red](0.00%);-";
groupSheet.getRange("D18:E29").format.numberFormat = "0.0";
groupSheet.getRange("F2:F29").format.numberFormat = "0";
groupSheet.getRange("G2:N29").format.numberFormat = "0.00%;[Red](0.00%);-";
groupSheet.getRange("O2:P29").format.numberFormat = "0.000";
const regSheet = workbook.worksheets.getItem("回归稳健性");
regSheet.getRange("D2:K29").format.numberFormat = "0.000";

// Verification before export.
const summaryCheck = await workbook.inspect({
  kind: "table",
  sheetId: "摘要",
  range: "A1:H35",
  include: "values,formulas",
  tableMaxRows: 35,
  tableMaxCols: 8,
  maxChars: 7000,
});
console.log("SUMMARY_INSPECT", summaryCheck.ndjson);
const methodCheck = await workbook.inspect({
  kind: "table",
  sheetId: "方法与来源",
  range: "A1:F31",
  include: "values,formulas",
  tableMaxRows: 31,
  tableMaxCols: 6,
  maxChars: 5000,
});
console.log("METHOD_INSPECT", methodCheck.ndjson);
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log("FORMULA_ERRORS", errors.ndjson);
const drawings = await workbook.inspect({ kind: "drawing", sheetId: "摘要", maxChars: 3000 });
console.log("DRAWINGS", drawings.ndjson);

for (const [sheetName, range] of [
  ["摘要", "A1:Q35"],
  ["方法与来源", "A1:F31"],
  ["样本底表", "A1:AS18"],
  ["统计检验", "A1:L19"],
  ["相关性", "A1:G13"],
  ["分组结果", "A1:P20"],
  ["回归稳健性", "A1:K20"],
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  const filename = `${sheetName}.png`;
  await fs.writeFile(path.join(previewDir, filename), new Uint8Array(await preview.arrayBuffer()));
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, previewDir }, null, 2));
