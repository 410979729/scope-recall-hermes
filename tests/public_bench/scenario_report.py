"""Render two frozen scenario runs into a machine-readable comparison and review table."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

# Isolated CLI execution omits this directory; only test helpers are added.
sys.path.insert(0, str(Path(__file__).resolve().parent))

LABELS = {"pass": "通过", "fail": "不通过", "m6": "留给 M6 真机", "error": "测法错误"}


def read(path):
    """Read one UTF-8 result without modifying its original evidence."""
    return json.loads(path.read_text(encoding="utf-8"))


def brief(row):
    """Expose decisive numbers or output snippets beside the status label."""
    a, key = row["actual"], row["id"]
    if isinstance(a, dict) and (a.get("real_host_result") or row["status"] == "m6"):
        return (
            a["real_host_result"]["summary"] + "; " + a["real_host_result"]["evidence"]
            if a.get("real_host_result")
            else a["missing"]
        )
    if key == "S9.latency" and "rows" in a:
        return "; ".join(
            f"{x['scale']}/{x['state']}/{x['metric']}={x['ms']}ms (上限{x['limit_ms']})" for x in a["rows"]
        )
    if key in {"S9.capture", "S9.fresh"}:
        return f"捕获 p95={a.get('capture_p95_ms')}ms; 开始 p95={a.get('start_p95_ms', '另测')}ms"
    if key == "S9.backlog":
        return f"积压 {a['before']['depth']}→{a['after']['depth']}; 恢复观测上界={a.get('recovery_seconds')}s"
    if key.startswith("S2."):
        return json.dumps({k: v for k, v in a.items() if k not in {"path", "failed"}}, ensure_ascii=False)
    if key == "S6.unrelated":
        return f"问题误注入 {a['false_injections']}/{a['count']}"
    if key == "S6.compare":
        return f"问题误注入率 {a['rate']:.4f}"
    if key == "S6.latency":
        summary = f"提问 p95={a['prompt_p95_ms']:.2f}ms; 开始 p95={a['start_p95_ms']:.2f}ms"
    elif key == "S3.promotion":
        summary = f"受控业务钟 {a.get('controlled_observation_seconds')}s；流程通过不代表真实模型质量或运行 p95"
    else:
        summary = json.dumps(a, ensure_ascii=False, default=str).replace("|", "\\|")[:240]
    return summary


def behavior_summary(key, actual):
    """Keep failure summaries focused on the observed acceptance condition."""
    if "missing" in actual:
        return actual["missing"]
    if key in {"S5.next", "S7.edit"}:
        old = [host for host, packet in actual["reads"].items() if "琥珀色" in json.dumps(packet, ensure_ascii=False)]
        return f"当前读取仍含旧值琥珀色的入口：{old}"
    if key == "S6.session":
        return "; ".join(
            f"{row['host']}:累计估计{row['delivered_tokens']},上限{row['diagnostics'].get('session_token_cap')}"
            for row in actual
        )
    if key in {"S5.sessions", "S5.budget", "S5.once"}:
        replies = actual.get("after", actual)
        flags = {host: "银色" in json.dumps(row.get("output", {}), ensure_ascii=False) for host, row in replies.items()}
        return f"下一轮含更正值银色：{flags}"
    if key == "S8.inline":
        return f"落库私密文本条数：{sum('TEST-hidden-' in row['content'] for row in actual['stored'])}"
    return json.dumps(actual, ensure_ascii=False, default=str)[:320]


def pair(baseline, current):
    """Join by stable subcheck ID and compute the paired false-injection criterion."""
    if baseline["fixture_sha256"] != current["fixture_sha256"]:
        raise ValueError("comparison requires byte-identical fixtures")
    old = {row["id"]: row for row in baseline["checks"]}
    new = {row["id"]: row for row in current["checks"]}
    if old.keys() != new.keys():
        raise ValueError("both versions must execute every subcheck")
    rates = []
    for rows in (old, new):
        actual = rows["S6.unrelated"]["actual"]
        rates.append(actual["false_injections"] / actual["count"])
    for index, rows in enumerate((old, new)):
        rows["S6.compare"]["status"] = "pass" if index == 0 or rates[1] <= rates[0] else "fail"
        rows["S6.compare"]["classification"] = None if rows["S6.compare"]["status"] == "pass" else "product_defect"
        rows["S6.compare"]["actual"].update(rate=rates[index], baseline_rate=rates[0], current_rate=rates[1])
    return [
        dict(
            id=key,
            definition=value["definition"],
            method=value["method"],
            criterion=value["criterion"],
            baseline=old[key],
            current=value,
        )
        for key, value in new.items()
    ]


def command(args, row, version):
    """Produce a fresh-root command scoped to the smallest independent scenario."""
    scenario = row["id"].split(".")[0]
    environment = "baseline-3.16.1" if version == "baseline" else "venv"
    output = args.scratch / f"reproduce-{scenario}-{version}"
    return (
        f"& '{args.scratch / environment / 'Scripts/python.exe'}' -I -B -X utf8 "
        f"'{Path(__file__).with_name('scenarios.py')}' --only {scenario} "
        f"--output '{output}' --resources '{args.resources}' --scratch '{args.scratch}'"
    )


def render(args, baseline, current, rows):
    """Emit definition-by-definition results, reproduction inputs and explicit limits."""
    lines = [
        "# ScopeRecall S1–S11 场景验收",
        "",
        f"生成于 {datetime.now(UTC).isoformat()}。",
        "",
        f"被测版本：{current['version']}；源码基线：{args.source_ref}。生产代码未改。",
        "3.16.1 从独立发行包环境加载；原 M0 保留 venv 现场已升级，未用作旧版对照。",
        "两版本使用同一份夹具。真实模型调用、真实嵌入服务调用、真实宿主进程均为 0。",
        "确定性模型结果只证明流程；真实安装、真实模型质量、Linux 和真实服务恢复留给 M6。",
        "管理页面通过其真实 UIApi 的 preview/confirm/read 路由验收，未启动桌面或浏览器。",
        "",
        "## 逐条验收",
        "",
        "| 子检查 | 定义原句 | 测法与判据 | 3.16.1 | 4.0 | 4.0 证据摘要 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        old, new = row["baseline"], row["current"]
        summary = brief(new).replace("|", chr(92) + "|")
        lines.append(
            f"| {row['id']} | {row['definition']} | {row['method']}；{row['criterion']} | "
            f"{LABELS[old['status']]} | {LABELS[new['status']]} | {summary} |"
        )
    lines += [
        "",
        "## 不通过项的复现与分类",
        "",
        "每条命令使用尚不存在的输出目录；重跑时更换该目录名。完整输入与输出在合并 JSON 对应子检查的 actual 中。",
        "S5/S7 的旧值判断涵盖当前结果中的原文，历史查询允许保留旧值。不是仅检查事实头已更新。",
    ]
    for row in rows:
        for version in ("baseline", "current"):
            value = row[version]
            if value["status"] not in {"fail", "error"}:
                continue
            category = "测法问题" if value["status"] == "error" else "产品缺陷（旧版缺失能力单独标明）"
            lines += [
                "",
                f"### {row['id']} / {'3.16.1' if version == 'baseline' else '4.0'}：{category}",
                "",
                f"输入：{row['method']}。",
                f"期望：{row['criterion']}。",
                f"实际：{brief(value)}。",
                "",
                "```powershell",
                command(args, row, version),
                "```",
            ]
    lines += [
        "",
        "## 证据边界",
        "",
        "- S2：58 份样例均实跑。旧版 51 个 mismatch 来自缺少新版自动快照，原证据保留。",
        "- S3：五个候选经历不足证据、补充证据、15 分钟静默窗口和评估；业务钟显式推进，不能当真实调度延迟 p95。",
        "- S4：接收方先运行 SessionStart，再在同会话提问；开场简报和问题钩子的原始输出分别保留。",
        "- S9 两档召回只引用 latency2-report.md。事实和范围分布与 M0 不同，全负载仍需验收。",
        "- 本包仅测 35 万档捕获和新消息时效。无向量及事实全负载，恢复指标只覆盖提炼队列。",
        "- 捕获门槛取 M0 35 万档 43.9241ms；会话开始 200ms，自动召回 1000ms、首次 2000ms、检索 200ms 均不放宽。",
        "- 机器上的其他活动和 OS 页缓存未隔离；计时是本地合成观察，不是线上服务保证。",
        "",
        "## 测试与改动",
        "",
        f"机器可读结果：`{args.output / 'results.json'}`。",
        "",
        f"指定文件检查：{args.validation['passed']} passed / {args.validation['failures']} failed / "
        f"{args.validation['skipped']} skipped；JUnit：`{args.junit}`。",
        f"质量检查通过：`{args.quality_log}`；未跑全量 T1 或整层。",
        "",
        "改动文件：scenarios.py、scenario_store.py、scenario_model.py、scenario_checks.py、scenario_mutations.py、scena"
        "rio_scale.py、scenario_report.py、scenarios_fixture.json、test_public_bench.py，均位于 tests/public_bench/。",
        "",
        "合成库完成取证后清理；保留结果、兼容矩阵、测试 JUnit、质量日志及可复现的隔离环境。未提交、未推送。",
    ]
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validation(args):
    """Read actual test counts and require the quality gate's successful receipt."""
    suites = ElementTree.parse(args.junit).getroot().findall("testsuite")
    counts = {
        key: sum(int(suite.get(key, "0")) for suite in suites) for key in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] == 0 or counts["failures"] or counts["errors"]:
        raise ValueError("selected scenario test file must pass before publication")
    quality = args.quality_log.read_text(encoding="utf-8-sig")
    if "quality: formatted, imports along the layers" not in quality:
        raise ValueError("successful quality receipt required")
    return dict(counts, passed=counts["tests"] - counts["skipped"])


def main():
    """Render a complete comparison only when both selected versions finished."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--quality-log", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args()
    args.validation = validation(args)
    baseline, current = read(args.baseline), read(args.current)
    rows = pair(baseline, current)
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(
        schema="scenarios-comparison/1",
        validation=args.validation,
        source_ref=args.source_ref,
        baseline=baseline,
        current=current,
        checks=rows,
        baseline_counts=dict(Counter(row["baseline"]["status"] for row in rows)),
        current_counts=dict(Counter(row["current"]["status"] for row in rows)),
    )
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(args, baseline, current, rows)
    print(json.dumps({key: report[key] for key in ("baseline_counts", "current_counts")}))


def merge_real(current, results):
    """Overlay real evidence by the existing IDs, preserving dry/small runs as unverified.

    Definitions remain owned by the frozen scenario fixture. A result from a
    different fixture or product version is rejected before any row is changed.
    """
    from copy import deepcopy

    from real_hosts import IDS

    merged = deepcopy(current)
    checks = {row["id"]: row for row in merged["checks"]}
    fixture = Path(__file__).with_name("scenarios_fixture.json").read_bytes().replace(b"\r\n", b"\n")
    # Windows text writers may preserve the same frozen JSON with CRLF. Only
    # these two byte representations are interchangeable, not edited fixtures.
    fixture_hashes = {hashlib.sha256(fixture).hexdigest(), hashlib.sha256(fixture.replace(b"\n", b"\r\n")).hexdigest()}
    for result in results:
        same_fixture = (
            result["fixture_sha256"] == current["fixture_sha256"]
            or {result["fixture_sha256"], current["fixture_sha256"]} <= fixture_hashes
        )
        if not same_fixture or result["version"] != current["version"]:
            raise ValueError("real evidence requires the identical fixture and product version")
        for row in result["checks"]:
            key = row["id"]
            if key not in IDS or key not in checks or row["definition"] != checks[key]["definition"]:
                raise ValueError("real evidence changed a scenario definition")
            accepted = result["mode"] == "real" and row["status"] in {"pass", "fail", "error"}
            actual = dict(checks[key]["actual"])
            observation = dict(
                mode=result["mode"],
                status=row["status"],
                actual=row["actual"],
                summary=("真机" if accepted else "流程/小规模，真机未验收")
                + ": "
                + LABELS[row["status"]]
                + "; "
                + real_numbers(row["actual"]),
                evidence=row["actual"].get("evidence", result["root"]),
            )
            prior = actual.get("real_host_result")
            if accepted or not prior or prior["mode"] != "real" or prior["status"] == "m6":
                actual["real_host_result"] = observation
            else:
                actual.setdefault("real_host_flow_results", []).append(observation)
            checks[key]["actual"] = actual
            if accepted:
                checks[key].update(status=row["status"], classification=row["classification"])
    return merged


def real_numbers(actual):
    """Keep decisive observed numbers visible in the existing table's evidence column."""
    text = "详见证据"
    if "scales" in actual:
        text = "; ".join(
            f"{row['scale']}条: 捕获p95={row['warm']['capture']['p95_ms']:.2f}/"
            f"{row['concurrent']['capture']['p95_ms']:.2f}ms, "
            f"积压{row['backlog_before']['depth']}→{row['backlog_after']['depth']}→{row['backlog_recovered']['depth']}"
            for row in actual["scales"]
        )
    elif "non_download_seconds" in actual:
        text = f"非下载{actual['non_download_seconds']:.2f}s; 下载{actual.get('download_seconds', 0):.2f}s"
    elif "promotion_p95_seconds" in actual:
        text = f"晋升{actual['completed']}/{actual['sample_count']}; p95={actual['promotion_p95_seconds']}s"
    elif "matrix_count" in actual:
        text = f"矩阵{actual['matrix_count']}份"
    elif "hosts" in actual:
        text = f"宿主{sum(bool(row['passed']) for row in actual['hosts'].values())}/{len(actual['hosts'])}"
    elif "faults" in actual:
        text = f"故障{sum(bool(row['passed']) for row in actual['faults'])}/{len(actual['faults'])}"
    return text


def merge_main():
    """Amend the original 53-row comparison/table from independently produced host evidence."""
    parser = argparse.ArgumentParser(description=merge_main.__doc__)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--real-results", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(sys.argv[2:])
    comparison = read(args.comparison)
    comparison["current"] = merge_real(comparison["current"], [read(path) for path in args.real_results])
    rows = pair(comparison["baseline"], comparison["current"])
    comparison.update(checks=rows, current_counts=dict(Counter(row["current"]["status"] for row in rows)))
    text = args.report.read_text(encoding="utf-8")
    lines = []
    by_id = {row["id"]: row for row in rows}
    for line in text.splitlines():
        if line.startswith("4.0：") and "共 53 条" in line:
            counts = comparison["current_counts"]
            line = (
                f"4.0：{counts.get('pass', 0)} 条通过、{counts.get('fail', 0)} 条不通过、"
                f"{counts.get('m6', 0)} 条留给 M6，{counts.get('error', 0)} 条测法错误。共 53 条。"
            )
        key = line.split(" | ")[0].removeprefix("| ")
        if key in by_id:
            row = by_id[key]
            line = (
                f"| {key} | {row['definition']} | {row['method']}；{row['criterion']} | "
                f"{LABELS[row['baseline']['status']]} | {LABELS[row['current']['status']]} | "
                + brief(row["current"]).replace("|", "\\|")
                + " |"
            )
        lines.append(line)
    lines += [
        "",
        "M6 附加证据按原子检查 ID 合入；dry-run 和小规模试跑不计为真机通过。",
        "当前逐条计数：" + json.dumps(comparison["current_counts"], ensure_ascii=False) + "。",
    ]
    args.output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    merge_main() if len(sys.argv) > 1 and sys.argv[1] == "merge-real" else main()
