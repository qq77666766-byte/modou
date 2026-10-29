#!/usr/bin/env python3
"""墨斗管理后台 (Modou Admin Console)

零依赖的本地 Web 后台，两大功能：
  1. 消耗检查 —— 解析 ~/.claude/projects/**/*.jsonl 的用量记录，
     按 昨天 / 本周 / 本月 统计各模型 token 消耗、费用与调配（委派）情况。
  2. 模型设置 —— 主模型（自动选最贵 / 手动）写入 ~/.claude/settings.json，
     执行官模型（墨斗）写入 executor.md 的 frontmatter。

用法:  python3 modou_admin.py [--port 8848] [--claude-dir ~/.claude]
仅监听 127.0.0.1，不向外发送任何数据，不涉及任何密钥。
"""

import argparse
import json
import os
import re
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 价格表（美元 / 百万 token），缓存写默认按 1.25x 输入价，缓存读按 0.1x。
# 若 usage 里带 ephemeral_1h_input_tokens 明细，则 1h 缓存写按 2x 计。
# 数据截至 2026-10（官方价格表 2026-10-06 版）。新一代模型缓存读不再是 0.1x，
# 用 "cr" 字段写绝对价；没写 "cr" 的模型仍按 0.1x 输入价。
# ---------------------------------------------------------------------------
PRICES = {
    "claude-fable-5-1":  {"in": 10.0, "out": 50.0, "cr": 0.25, "name": "Fable 5.1"},
    "claude-mythos-5-1": {"in": 10.0, "out": 50.0, "cr": 0.25, "name": "Mythos 5.1"},
    "claude-fable-5":    {"in": 10.0, "out": 50.0, "name": "Fable 5"},
    "claude-mythos-5":   {"in": 10.0, "out": 50.0, "name": "Mythos 5"},
    "claude-opus-5-5":   {"in": 4.0,  "out": 20.0, "cr": 0.20, "name": "Opus 5.5"},
    "claude-opus-5":     {"in": 5.0,  "out": 25.0, "name": "Opus 5"},
    "claude-opus-4-8":   {"in": 5.0,  "out": 25.0, "name": "Opus 4.8"},
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0, "name": "Opus 4.7"},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0, "name": "Opus 4.6"},
    "claude-sonnet-5-5": {"in": 2.0,  "out": 10.0, "cr": 0.20, "name": "Sonnet 5.5"},
    "claude-sonnet-5":   {"in": 2.0,  "out": 10.0, "name": "Sonnet 5"},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0, "name": "Sonnet 4.6"},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50, "name": "Haiku 5.5"},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0,  "name": "Haiku 4.5"},
}
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0
CACHE_READ = 0.1

# 可在下拉框中选择的执行官候选模型
EXECUTOR_CHOICES = [
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-haiku-5-5",
]
MAIN_CHOICES = ["claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"]

# 执行官速度档位（frontmatter 的 effort 字段）。子代理不支持 /fast 极速模式，
# effort 是官方支持的提速手段：档位越低思考越少 → 越快、越省 token。
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]


def norm_model(model_id):
    """把带日期后缀的 id (claude-haiku-4-5-20251001) 归一到价格表键。"""
    if not model_id:
        return None
    for key in sorted(PRICES, key=len, reverse=True):
        if model_id == key or model_id.startswith(key + "-"):
            return key
    return model_id


def price_of(model_id):
    return PRICES.get(norm_model(model_id))


def most_expensive_model():
    """自动模式：主模型 = 价格表里最贵的（按输入价，其次输出价）。
    Mythos 仅对特定组织开放，自动模式跳过它。同价时取表中靠前的（新版在前）。"""
    candidates = {k: v for k, v in PRICES.items() if "mythos" not in k}
    return max(candidates, key=lambda k: (candidates[k]["in"], candidates[k]["out"]))


def cost_usd(model_id, usage):
    """按单条 usage 计费；未知模型返回 None。"""
    p = price_of(model_id)
    if not p:
        return None
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    # 有 5m/1h 明细时按明细计缓存写费用
    detail = usage.get("cache_creation") or {}
    w5 = detail.get("ephemeral_5m_input_tokens")
    w1 = detail.get("ephemeral_1h_input_tokens")
    if w5 is not None or w1 is not None:
        write_cost = ((w5 or 0) * CACHE_WRITE_5M + (w1 or 0) * CACHE_WRITE_1H) * p["in"]
    else:
        write_cost = cw * CACHE_WRITE_5M * p["in"]
    return (inp * p["in"] + out * p["out"] + write_cost + cr * p.get("cr", CACHE_READ * p["in"])) / 1e6


# ---------------------------------------------------------------------------
# 用量扫描：Claude Code 把每条 assistant 消息（含 usage）写进
# ~/.claude/projects/<project>/<session>.jsonl。同一条消息可能因流式写入出现
# 多行，用 (message.id, requestId) 去重。
# ---------------------------------------------------------------------------
class UsageScanner:
    def __init__(self, claude_dir):
        self.projects_dir = Path(claude_dir) / "projects"
        self._file_cache = {}  # path -> (mtime, size, [records])

    def records(self):
        out = []
        if not self.projects_dir.is_dir():
            return out
        for path in self.projects_dir.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            cached = self._file_cache.get(str(path))
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                out.extend(cached[2])
                continue
            recs = self._parse_file(path)
            self._file_cache[str(path)] = (st.st_mtime, st.st_size, recs)
            out.extend(recs)
        # 跨文件也去重一次（同一请求可能被多个视图文件记录）
        seen, dedup = set(), []
        for r in out:
            key = r["key"]
            if key in seen:
                continue
            seen.add(key)
            dedup.append(r)
        return dedup

    def _parse_file(self, path):
        recs, seen = [], set()
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = obj.get("message") or {}
                    usage = msg.get("usage")
                    model = msg.get("model")
                    ts = obj.get("timestamp")
                    if not usage or not model or not ts or model == "<synthetic>":
                        continue
                    key = (msg.get("id") or "", obj.get("requestId") or "", ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        # 时间戳是 UTC ISO，转本地时区
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                    except ValueError:
                        continue
                    recs.append({
                        "key": key,
                        "dt": dt,
                        "model": norm_model(model),
                        "raw_model": model,
                        "sidechain": bool(obj.get("isSidechain")),
                        "usage": {
                            "input_tokens": usage.get("input_tokens", 0) or 0,
                            "output_tokens": usage.get("output_tokens", 0) or 0,
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                            "cache_creation": usage.get("cache_creation"),
                        },
                    })
        except OSError:
            pass
        return recs


def range_bounds(name):
    now = datetime.now().astimezone()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "today":
        return today0, now
    if name == "yesterday":
        return today0 - timedelta(days=1), today0
    if name == "week":  # 本周（周一起）
        return today0 - timedelta(days=today0.weekday()), now
    if name == "month":
        return today0.replace(day=1), now
    return None, now  # all


def aggregate(records, start, end, main_model, executor_model):
    by_model, daily = {}, {}
    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
              "cost": 0.0, "requests": 0}
    exec_stat = {"cost": 0.0, "output": 0, "requests": 0, "cost_if_main": 0.0}
    main_stat = {"cost": 0.0, "output": 0, "requests": 0}
    main_price = PRICES.get(main_model)

    for r in records:
        if start and r["dt"] < start:
            continue
        if r["dt"] >= end:
            continue
        u, m = r["usage"], r["model"]
        c = cost_usd(m, u) or 0.0
        slot = by_model.setdefault(m, {"model": m, "name": PRICES.get(m, {}).get("name", m),
                                       "input": 0, "output": 0, "cache_write": 0,
                                       "cache_read": 0, "cost": 0.0, "requests": 0})
        slot["input"] += u["input_tokens"]
        slot["output"] += u["output_tokens"]
        slot["cache_write"] += u["cache_creation_input_tokens"]
        slot["cache_read"] += u["cache_read_input_tokens"]
        slot["cost"] += c
        slot["requests"] += 1
        totals["input"] += u["input_tokens"]
        totals["output"] += u["output_tokens"]
        totals["cache_write"] += u["cache_creation_input_tokens"]
        totals["cache_read"] += u["cache_read_input_tokens"]
        totals["cost"] += c
        totals["requests"] += 1
        day = r["dt"].strftime("%m-%d")
        d = daily.setdefault(day, {"day": day, "cost": 0.0, "output": 0})
        d["cost"] += c
        d["output"] += u["output_tokens"]

        if m == executor_model:
            exec_stat["cost"] += c
            exec_stat["output"] += u["output_tokens"]
            exec_stat["requests"] += 1
            if main_price:
                exec_stat["cost_if_main"] += (cost_usd(main_model, u) or 0.0)
        elif m == main_model:
            main_stat["cost"] += c
            main_stat["output"] += u["output_tokens"]
            main_stat["requests"] += 1

    savings = max(0.0, exec_stat["cost_if_main"] - exec_stat["cost"])
    total_out = totals["output"]
    delegation_ratio = (exec_stat["output"] / total_out) if total_out else 0.0
    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat(),
        "totals": totals,
        "by_model": sorted(by_model.values(), key=lambda x: -x["cost"]),
        "daily": sorted(daily.values(), key=lambda x: x["day"]),
        "executor": exec_stat,
        "main": main_stat,
        "savings": savings,
        "delegation_ratio": delegation_ratio,
    }


# ---------------------------------------------------------------------------
# 模型设置：主模型 -> ~/.claude/settings.json；执行官 -> executor.md frontmatter
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, claude_dir, repo_dir):
        self.claude_dir = Path(claude_dir)
        self.settings_path = self.claude_dir / "settings.json"
        # 优先全局安装的 executor，其次仓库内的
        self.executor_paths = [
            self.claude_dir / "agents" / "executor.md",
            repo_dir / "agents" / "executor.md",
        ]

    # -- 主模型 --------------------------------------------------------------
    def read_settings(self):
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get_main_model(self):
        return self.read_settings().get("model")

    def set_main_model(self, model):
        settings = self.read_settings()
        settings["model"] = model
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # -- 执行官模型 ----------------------------------------------------------
    def executor_file(self):
        for p in self.executor_paths:
            if p.is_file():
                return p
        return None

    def get_executor_model(self):
        p = self.executor_file()
        if not p:
            return None, None
        m = re.search(r"^model:\s*(\S+)\s*$", p.read_text(encoding="utf-8"), re.M)
        return (m.group(1) if m else None), str(p)

    def set_executor_model(self, model):
        p = self.executor_file()
        if not p:
            raise FileNotFoundError("找不到 executor.md（未安装墨斗？）")
        text = p.read_text(encoding="utf-8")
        new_text, n = re.subn(r"^model:\s*\S+\s*$", f"model: {model}", text, count=1, flags=re.M)
        if n == 0:
            raise ValueError("executor.md frontmatter 里没有 model: 字段")
        p.write_text(new_text, encoding="utf-8")
        return str(p)

    # -- 执行官速度（effort）--------------------------------------------------
    def get_executor_effort(self):
        p = self.executor_file()
        if not p:
            return None
        m = re.search(r"^effort:\s*(\S+)\s*$", p.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None

    def set_executor_effort(self, effort):
        """effort 为 EFFORT_CHOICES 之一；传 "" 表示删除该字段（跟随会话默认）。"""
        p = self.executor_file()
        if not p:
            raise FileNotFoundError("找不到 executor.md（未安装墨斗？）")
        text = p.read_text(encoding="utf-8")
        if effort:
            new_text, n = re.subn(r"^effort:\s*\S+\s*$", f"effort: {effort}",
                                  text, count=1, flags=re.M)
            if n == 0:  # 没有 effort 行，插到 model 行后面
                new_text, n = re.subn(r"^(model:\s*\S+\s*)$", rf"\1\neffort: {effort}",
                                      text, count=1, flags=re.M)
                if n == 0:
                    raise ValueError("executor.md frontmatter 里没有 model: 字段")
        else:
            new_text = re.sub(r"^effort:\s*\S+\s*\n?", "", text, count=1, flags=re.M)
        p.write_text(new_text, encoding="utf-8")
        return str(p)


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
def make_handler(scanner, config):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静一点
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/usage":
                q = parse_qs(url.query)
                rng = (q.get("range") or ["yesterday"])[0]
                start, end = range_bounds(rng)
                exec_model, _ = config.get_executor_model()
                main_model = norm_model(config.get_main_model()) or most_expensive_model()
                data = aggregate(scanner.records(), start, end,
                                 main_model, norm_model(exec_model))
                data["range"] = rng
                self._json(data)
            elif url.path == "/api/settings":
                exec_model, exec_path = config.get_executor_model()
                self._json({
                    "main_model": config.get_main_model(),
                    "main_auto_pick": most_expensive_model(),
                    "main_choices": MAIN_CHOICES,
                    "executor_model": exec_model,
                    "executor_path": exec_path,
                    "executor_choices": EXECUTOR_CHOICES,
                    "executor_effort": config.get_executor_effort(),
                    "effort_choices": EFFORT_CHOICES,
                    "prices": PRICES,
                    "settings_path": str(config.settings_path),
                    "projects_dir": str(scanner.projects_dir),
                    "projects_found": scanner.projects_dir.is_dir(),
                })
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)

            if url.path == "/api/settings/main":
                if payload.get("auto"):
                    model = most_expensive_model()
                else:
                    model = payload.get("model")
                    if model not in PRICES:
                        return self._json({"error": f"未知模型: {model}"}, 400)
                config.set_main_model(model)
                self._json({"ok": True, "model": model})
            elif url.path == "/api/settings/executor":
                model = payload.get("model")
                effort = payload.get("effort", None)  # None=不动; ""=删除; 其他需合法
                if model not in PRICES:
                    return self._json({"error": f"未知模型: {model}"}, 400)
                if effort not in (None, "") and effort not in EFFORT_CHOICES:
                    return self._json({"error": f"未知 effort: {effort}"}, 400)
                try:
                    path = config.set_executor_model(model)
                    if effort is not None:
                        config.set_executor_effort(effort)
                except (FileNotFoundError, ValueError) as e:
                    return self._json({"error": str(e)}, 400)
                self._json({"ok": True, "model": model, "effort": effort,
                            "path": path, "note": "新开会话后生效"})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


# ---------------------------------------------------------------------------
# 前端页面（内嵌，无外部依赖，自适应亮/暗色）
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>墨斗管理后台</title>
<style>
:root {
  --bg:#f6f5f2; --card:#ffffff; --fg:#1e1e1c; --muted:#6f6d66;
  --line:#e4e2db; --accent:#0f6f5c; --accent-weak:#dcefe9; --warn:#a04d0c;
  --bar:#0f6f5c; --bar2:#8aa39b;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#161614; --card:#1f1f1c; --fg:#eceae4; --muted:#98958c;
    --line:#33322d; --accent:#4fbf9f; --accent-weak:#1d3a33; --warn:#e0954f;
    --bar:#4fbf9f; --bar2:#3d5a52; }
}
* { box-sizing:border-box; }
body { margin:0; font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--fg); }
.wrap { max-width:960px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:8px 0 2px; }
h1 small { color:var(--muted); font-weight:400; font-size:13px; margin-left:8px; }
h2 { font-size:16px; margin:0 0 12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px 18px; margin-top:16px; }
.tabs { display:flex; gap:8px; margin:16px 0 0; flex-wrap:wrap; }
.tab { border:1px solid var(--line); background:var(--card); color:var(--fg);
  padding:6px 16px; border-radius:999px; cursor:pointer; font-size:14px; }
.tab.on { background:var(--accent); border-color:var(--accent); color:#fff; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.kpi { border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.kpi .v { font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; }
.kpi .l { color:var(--muted); font-size:12px; }
.kpi.hl { background:var(--accent-weak); border-color:transparent; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th,td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--line); font-size:13px; }
th:first-child,td:first-child { text-align:left; }
th { color:var(--muted); font-weight:500; }
.scroll { overflow-x:auto; }
.bars { display:flex; align-items:flex-end; gap:4px; height:110px; margin-top:8px; }
.bcol { flex:1; display:flex; flex-direction:column; justify-content:flex-end;
  align-items:center; gap:4px; min-width:22px; }
.bar { width:100%; background:var(--bar); border-radius:3px 3px 0 0; min-height:2px; }
.blab { font-size:10px; color:var(--muted); white-space:nowrap; }
.row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:8px 0; }
select,button { font:inherit; padding:6px 12px; border-radius:6px;
  border:1px solid var(--line); background:var(--card); color:var(--fg); }
button.primary { background:var(--accent); color:#fff; border-color:var(--accent); cursor:pointer; }
label { font-size:14px; }
.muted { color:var(--muted); font-size:13px; }
.ok { color:var(--accent); } .warn { color:var(--warn); }
#toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
  background:var(--fg); color:var(--bg); padding:8px 20px; border-radius:8px;
  opacity:0; transition:opacity .3s; pointer-events:none; font-size:14px; }
code { background:var(--accent-weak); padding:1px 5px; border-radius:4px; font-size:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>墨斗管理后台 <small>Modou Admin Console</small></h1>
  <div class="muted">主线弹线，工匠照线施工 —— 消耗检查 · 模型设置</div>

  <div class="tabs" id="tabs">
    <button class="tab" data-r="yesterday">昨天</button>
    <button class="tab" data-r="week">本周</button>
    <button class="tab" data-r="month">本月</button>
    <button class="tab" data-r="today">今天</button>
    <button class="tab" data-r="all">全部</button>
  </div>

  <div class="card">
    <h2>消耗总览 <span class="muted" id="rangeLabel"></span></h2>
    <div class="kpis" id="kpis"></div>
  </div>

  <div class="card">
    <h2>调配情况（主线 vs 墨斗）</h2>
    <div class="kpis" id="delegation"></div>
    <div class="muted" id="delegationNote" style="margin-top:8px"></div>
  </div>

  <div class="card">
    <h2>按模型明细</h2>
    <div class="scroll"><table id="modelTable"></table></div>
  </div>

  <div class="card">
    <h2>每日费用</h2>
    <div class="bars" id="bars"></div>
  </div>

  <div class="card">
    <h2>模型设置</h2>
    <div class="row">
      <label>主模型：</label>
      <label><input type="checkbox" id="autoMain"> 自动选择最贵</label>
      <select id="mainSelect"></select>
      <button class="primary" onclick="saveMain()">保存主模型</button>
    </div>
    <div class="muted" id="mainInfo"></div>
    <div class="row" style="margin-top:14px">
      <label>执行官（墨斗）模型：</label>
      <select id="execSelect"></select>
      <label>速度档位：</label>
      <select id="effortSelect"></select>
      <button class="primary" onclick="saveExec()">保存执行官</button>
    </div>
    <div class="muted">速度档位即 effort：档位越低施工越快、token 越省（思考更少）。
      子代理不支持 /fast 极速模式，effort 是官方支持的提速手段；机械性实施推荐 low / medium。</div>
    <div class="muted" id="execInfo"></div>
    <div class="muted" id="pairWarn" style="margin-top:6px"></div>
  </div>

  <div class="card">
    <h2>健康检查 &amp; 价格表</h2>
    <div id="health"></div>
    <div class="scroll" style="margin-top:10px"><table id="priceTable"></table></div>
  </div>
</div>
<div id="toast"></div>

<script>
let S = null, currentRange = "yesterday";
const $ = id => document.getElementById(id);
const fmtT = n => n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(2)+"M"
              : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : String(n);
const fmtC = n => "$" + n.toFixed(n >= 100 ? 0 : n >= 1 ? 2 : 4);
const rangeNames = {yesterday:"昨天", week:"本周", month:"本月", today:"今天", all:"全部"};

function toast(msg, isErr) {
  const t = $("toast"); t.textContent = msg;
  t.style.background = isErr ? "var(--warn)" : "var(--fg)";
  t.style.opacity = 1; setTimeout(() => t.style.opacity = 0, 2500);
}

async function loadUsage(range) {
  currentRange = range;
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.r === range));
  const d = await (await fetch("/api/usage?range=" + range)).json();
  $("rangeLabel").textContent = "· " + (rangeNames[range] || range);
  const t = d.totals;
  $("kpis").innerHTML = [
    kpi(fmtC(t.cost), "总费用", true),
    kpi(fmtT(t.output), "输出 token"),
    kpi(fmtT(t.input), "输入 token"),
    kpi(fmtT(t.cache_read), "缓存读"),
    kpi(fmtT(t.cache_write), "缓存写"),
    kpi(String(t.requests), "请求数"),
  ].join("");

  const ex = d.executor, mn = d.main;
  $("delegation").innerHTML = [
    kpi((d.delegation_ratio*100).toFixed(1) + "%", "委派率（按输出 token）", true),
    kpi(fmtC(d.savings), "墨斗为你省下", true),
    kpi(fmtC(mn.cost), "主线花费"),
    kpi(fmtC(ex.cost), "墨斗花费"),
    kpi(String(mn.requests), "主线请求"),
    kpi(String(ex.requests), "墨斗请求"),
  ].join("");
  $("delegationNote").textContent = ex.requests === 0
    ? "该时段没有墨斗（执行官）的调用记录。实施类任务多让主线委派给墨斗，省钱又省上下文。"
    : `若这些实施工作全由主模型完成需 ${fmtC(ex.cost_if_main)}，实际仅 ${fmtC(ex.cost)}。`;

  const rows = d.by_model.map(m =>
    `<tr><td>${m.name}<div class="muted">${m.model}</div></td><td>${m.requests}</td>` +
    `<td>${fmtT(m.input)}</td><td>${fmtT(m.output)}</td><td>${fmtT(m.cache_read)}</td>` +
    `<td>${fmtT(m.cache_write)}</td><td><b>${fmtC(m.cost)}</b></td></tr>`).join("");
  $("modelTable").innerHTML =
    "<tr><th>模型</th><th>请求</th><th>输入</th><th>输出</th><th>缓存读</th><th>缓存写</th><th>费用</th></tr>" +
    (rows || "<tr><td colspan=7 class='muted'>该时段无记录</td></tr>");

  const days = d.daily.slice(-31);
  const maxC = Math.max(...days.map(x => x.cost), 1e-9);
  $("bars").innerHTML = days.length
    ? days.map(x => `<div class="bcol" title="${x.day}  ${fmtC(x.cost)}">` +
        `<div class="bar" style="height:${Math.max(2, x.cost/maxC*90)}px"></div>` +
        `<div class="blab">${x.day.slice(3)}</div></div>`).join("")
    : "<div class='muted'>该时段无记录</div>";
}

function kpi(v, l, hl) {
  return `<div class="kpi${hl ? " hl" : ""}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
}

async function loadSettings() {
  S = await (await fetch("/api/settings")).json();
  const mainSel = $("mainSelect");
  mainSel.innerHTML = S.main_choices.map(m =>
    `<option value="${m}">${S.prices[m].name} ($${S.prices[m].in}/$${S.prices[m].out})</option>`).join("");
  const curMain = S.main_model && Object.keys(S.prices).find(k =>
    S.main_model === k || S.main_model.startsWith(k + "-"));
  const auto = !S.main_model || curMain === S.main_auto_pick;
  $("autoMain").checked = auto;
  mainSel.value = curMain || S.main_auto_pick;
  mainSel.disabled = auto;
  $("mainInfo").innerHTML = S.main_model
    ? `当前 settings.json 中：<code>${S.main_model}</code>（${S.settings_path}）`
    : `settings.json 未设置 model，Claude Code 使用其默认模型。自动模式将写入最贵的 <code>${S.main_auto_pick}</code>。`;

  const execSel = $("execSelect");
  execSel.innerHTML = S.executor_choices.map(m =>
    `<option value="${m}">${S.prices[m].name} ($${S.prices[m].in}/$${S.prices[m].out})</option>`).join("");
  if (S.executor_model && S.prices[S.executor_model]) execSel.value = S.executor_model;

  const effortNames = {low:"low · 最快", medium:"medium · 快（推荐）",
    high:"high · 标准", xhigh:"xhigh · 深思", max:"max · 极限"};
  $("effortSelect").innerHTML = '<option value="">跟随会话默认</option>' +
    S.effort_choices.map(e => `<option value="${e}">${effortNames[e] || e}</option>`).join("");
  $("effortSelect").value = S.executor_effort || "";

  $("execInfo").innerHTML = S.executor_model
    ? `当前 executor.md 中：<code>${S.executor_model}</code>` +
      (S.executor_effort ? `，effort <code>${S.executor_effort}</code>` : "") +
      `（${S.executor_path}）— 修改后新开会话生效`
    : `<span class="warn">未找到已安装的 executor.md，请先安装墨斗（bash install.sh）。</span>`;
  updatePairWarn();

  $("health").innerHTML = [
    line(S.executor_model, `执行官已安装：${S.executor_path || "—"}`, "执行官未安装（找不到 executor.md）"),
    line(S.projects_found, `用量数据目录：${S.projects_dir}`, `找不到用量目录 ${S.projects_dir}（本机没跑过 Claude Code？）`),
    line(true, "价格表内置于本工具，模型价格变动时请更新 PRICES。"),
  ].join("");

  $("priceTable").innerHTML =
    "<tr><th>模型</th><th>模型 ID</th><th>输入 $/M</th><th>输出 $/M</th></tr>" +
    Object.entries(S.prices).map(([k, p]) =>
      `<tr><td>${p.name}</td><td style="text-align:left"><code>${k}</code></td>` +
      `<td>$${p.in}</td><td>$${p.out}</td></tr>`).join("");
}

function line(ok, okText, badText) {
  return `<div class="${ok ? "ok" : "warn"}">${ok ? "✓ " + okText : "✗ " + (badText || okText)}</div>`;
}

function updatePairWarn() {
  const main = $("autoMain").checked ? S.main_auto_pick : $("mainSelect").value;
  const ex = $("execSelect").value;
  const pm = S.prices[main], pe = S.prices[ex];
  $("pairWarn").innerHTML = (pm && pe && pe.in >= pm.in)
    ? `<span class="warn">⚠ 执行官（$${pe.in}/M）不便宜于主模型（$${pm.in}/M）——同级转包纯增开销，墨斗的前提是主强于执行官。</span>`
    : `执行官为主模型价格的 ${pm && pe ? Math.round(pe.in/pm.in*100) : "?"}%，委派实施可显著省钱。`;
}

async function saveMain() {
  const auto = $("autoMain").checked;
  const r = await fetch("/api/settings/main", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(auto ? {auto:true} : {model: $("mainSelect").value})});
  const d = await r.json();
  if (d.ok) { toast(`主模型已设为 ${d.model}`); loadSettings(); }
  else toast(d.error || "失败", true);
}

async function saveExec() {
  const r = await fetch("/api/settings/executor", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({model: $("execSelect").value, effort: $("effortSelect").value})});
  const d = await r.json();
  if (d.ok) { toast(`执行官已设为 ${d.model}${d.effort ? " / " + d.effort : ""}，新开会话生效`); loadSettings(); loadUsage(currentRange); }
  else toast(d.error || "失败", true);
}

$("autoMain").addEventListener("change", e => {
  $("mainSelect").disabled = e.target.checked; updatePairWarn();
});
$("mainSelect").addEventListener("change", updatePairWarn);
$("execSelect").addEventListener("change", updatePairWarn);
$("tabs").addEventListener("click", e => {
  if (e.target.dataset.r) loadUsage(e.target.dataset.r);
});

loadSettings().then(() => loadUsage("yesterday"));
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="墨斗管理后台")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"),
                    help="Claude Code 配置目录（默认 ~/.claude）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    repo_dir = Path(__file__).resolve().parent.parent
    scanner = UsageScanner(args.claude_dir)
    config = Config(args.claude_dir, repo_dir)

    addr = ("127.0.0.1", args.port)
    httpd = ThreadingHTTPServer(addr, make_handler(scanner, config))
    url = f"http://{addr[0]}:{args.port}"
    print(f"墨斗管理后台已启动：{url}  (Ctrl+C 退出)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
