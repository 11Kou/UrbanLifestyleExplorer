r"""Run offline tests, optionally followed by a small real-service evaluation.

Usage: .venv\Scripts\python.exe test.py [--live]
The --live option calls Amap and the LLM using .env and may incur fees.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parent


def load_cases() -> list[dict]:
    return json.loads((ROOT / "tests" / "evaluation_cases.json").read_text(encoding="utf-8"))


def check_response(case: dict, status: int, body: dict) -> list[str]:
    from urban_explorer.models import ChatResponse

    expected = case["expect"]
    expected_status = expected.get("status", 200)
    if status != expected_status:
        return [f"期望 HTTP {expected_status}，实际 {status}"]
    if status != 200:
        return []
    # Invalid/missing result fields must not silently pass the evaluator.
    ChatResponse.model_validate(body)
    failures = []
    filters = body["session"]["filters"]
    budgets = body["session"]["budgets"]
    places = [p for result in body["results"] for p in result["places"]]
    if body["mode"] != "live":
        failures.append("错误地使用了演示模式")
    if filters["city"] != expected["city"]:
        failures.append(f"期望城市 {expected['city']}")
    agents = set(expected["agents"])
    if set(body["agents"]) != agents or {r["agent"] for r in body["results"]} != agents:
        failures.append(f"期望分流及专家结果 {sorted(agents)}")
    for key, value in expected.get("filters", {}).items():
        if filters.get(key) != value:
            failures.append(f"条件 {key} 应为 {value!r}")
    for kind, value in expected.get("budgets", {}).items():
        if budgets.get(kind) != value:
            failures.append(f"{kind} 预算应为 {value!r}")
    for preference in expected.get("preferences_contains", []):
        if preference not in filters["preferences"]:
            failures.append(f"偏好应包含 {preference}")
    for preference in expected.get("preferences_excludes", []):
        if preference in filters["preferences"]:
            failures.append(f"偏好应移除 {preference}")
    if any(p["mode"] != "live" or not p["id"].startswith("amap-") or p["city"] != expected["city"] for p in places):
        failures.append("地点模式、城市或 ID 来源不符")
    for result in body["results"]:
        if any(p["kind"] != result["agent"] for p in result["places"]):
            failures.append("专家返回了其他类别的地点")
    for kind, terms in expected.get("place_terms", {}).items():
        if any(p["kind"] == kind and not any(term in (p["name"] + p["category"] + " ".join(p["tags"])) for term in terms) for p in places):
            failures.append(f"{kind} 地点类型不符合 {terms}")
    for kind in ("food", "fun"):
        cap = budgets.get(kind)
        if cap is not None and any(p["kind"] == kind and (p["cost"] is None or p["cost"] > cap) for p in places):
            failures.append(f"{kind} 地点缺少价格或超出预算")
    area = expected.get("filters", {}).get("area")
    if area and any(area not in (p["area"] + p["address"]) for p in places):
        failures.append("返回地点不符合指定片区")
    policy = expected.get("results", "allow_empty")
    if policy == "empty" and places:
        failures.append("应返回空结果，不应擅自放宽条件")
    if policy == "required":
        for agent in agents - {"city"}:
            if not any(r["agent"] == agent and r["places"] for r in body["results"]):
                failures.append(f"{agent} 未返回可用地点")
    if any(term in notice for notice in body["notices"] for term in ("模型未返回", "生成失败", "未配置大模型", "外部服务", "查询失败")):
        failures.append("服务降级，需检查 notices")
    return failures


def evaluate_live(database: Path) -> list[dict]:
    from fastapi.testclient import TestClient

    from urban_explorer.api import create_app
    from urban_explorer.config import Settings

    settings = Settings(app_mode="live", database_url="sqlite:///" + database.as_posix(), _env_file=ROOT / ".env")
    if not settings.amap_api_key.get_secret_value() or not settings.has_llm:
        raise ValueError("真实评测需要在 .env 配置高德和模型密钥。")
    run_id = uuid.uuid4().hex
    records = []
    failed_sessions = set()
    with TestClient(create_app(settings)) as client:
        for case in load_cases():
            started = time.perf_counter()
            failures = []
            actual = {}
            session = case["session"]
            blocked = session in failed_sessions
            if blocked:
                failures.append("同一会话的前置用例失败，本条未调用接口")
            else:
                try:
                    response = client.post("/chat", json={
                        "session_id": f"eval_{run_id}_{session}", "message": case["question"],
                        **case.get("payload", {}),
                    })
                    body = response.json()
                    failures = check_response(case, response.status_code, body)
                    actual = {"status": response.status_code}
                    if response.status_code == 200:
                        actual.update(filters=body["session"]["filters"], budgets=body["session"]["budgets"],
                                      agents=body["agents"], notices=body["notices"], answer=body["answer"],
                                      places=[{"id": p["id"], "name": p["name"], "kind": p["kind"],
                                               "category": p["category"], "area": p["area"], "address": p["address"],
                                               "cost": p["cost"], "reason": p["reason"], "sources": p["sources"]}
                                              for r in body["results"] for p in r["places"]])
                except Exception as error:
                    # Never log raw upstream exceptions containing URLs or keys.
                    failures.append(f"执行异常：{type(error).__name__}")
            if failures:
                failed_sessions.add(session)
            record = {"id": case["id"], "category": case["category"], "session": session,
                      "question": case["question"], "passed": not failures, "blocked": blocked,
                      "elapsed_seconds": round(time.perf_counter() - started, 3),
                      "expected": case["expect"], "actual": actual, "failures": failures}
            if case["expect"].get("status", 200) == 200:
                record["manual_review"] = {"relevance": None, "factuality": None, "helpfulness": None}
            records.append(record)
            label = "BLOCKED" if blocked else "PASS" if record["passed"] else "FAIL"
            print(f"[{label}] {case['id']} {case['question']} ({record['elapsed_seconds']}s)", flush=True)
            for failure in failures:
                print("  " + failure, flush=True)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="城市初见：离线测试与真实接口评测")
    parser.add_argument("--live", action="store_true", help="额外运行40条评测，包含付费真实调用及输入校验")
    args = parser.parse_args()
    os.chdir(ROOT)
    work = ROOT / "work"
    temporary = work / "test-temp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary)}
    started = time.perf_counter()
    print("运行离线测试（不调用外部服务）…", flush=True)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT, env=environment)
    report = {"offline_passed": result.returncode == 0,
              "offline_elapsed_seconds": round(time.perf_counter() - started, 3),
              "live_requested": args.live, "live_results": []}
    status = result.returncode
    if args.live and status == 0:
        print("运行真实接口评测；使用独立数据库，不修改应用收藏和会话。", flush=True)
        try:
            records = evaluate_live(work / f"evaluation-{uuid.uuid4().hex}.db")
            report["live_results"] = records
            passed = sum(record["passed"] for record in records)
            report["live_pass_rate"] = passed / len(records)
            report["live_blocked"] = sum(record["blocked"] for record in records)
            report["live_executed"] = len(records) - report["live_blocked"]
            report["live_failed"] = report["live_executed"] - passed
            report["by_category"] = {}
            for record in records:
                counts = report["by_category"].setdefault(record["category"], {"total": 0, "passed": 0, "blocked": 0})
                counts["total"] += 1
                counts["passed"] += int(record["passed"])
                counts["blocked"] += int(record["blocked"])
            print(f"真实接口通过：{passed}/{len(records)}", flush=True)
            print(f"实际执行 {report['live_executed']} 条，失败 {report['live_failed']} 条，前置阻塞 {report['live_blocked']} 条。", flush=True)
            status = 0 if passed == len(records) else 1
        except Exception as error:
            report["live_error"] = type(error).__name__
            print(f"无法启动真实评测（{type(error).__name__}），请检查依赖及 .env 配置。", flush=True)
            status = 1
    report_path = work / ("test-report.json" if args.live else "test-report-offline.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"测试报告：{report_path}", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
