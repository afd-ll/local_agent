# -*- coding: utf-8 -*-
"""
local_agent.py —— 本地 Ollama Agent（记忆 + 搜索 + 文件写入 + 彩色 CLI）
V11 = V10 + 按Q打断输出 + 不确定命令先查帮助再执行
V10 = V9 + 数据目录统一归置（AGENT_DIR）
V9 = V8 + 修复 compress_memory 压缩结果未落盘（重启丢失）的 bug
V8 = V7 + Tavily 搜索 API + 多步工具链自主规划
V7 = V6 + 时间感知（启动注入当前时间 + 上次会话间隔）
V6 = V5 + 灵魂文件 soul.md（身份设定启动注入，不占多少上下文）
V5 = V4 + 上下文裁剪前先沉淀进记忆（压缩不丢失早期内容）
V4 = V3 + 启动显示记忆文件 + 命令行指定记忆文件路径
基于 v2：保留思考 tag、16K 上下文、滑动窗口
新增：
  1. 记忆系统（Hermes 式）：跨会话记忆存储 + 超量自动压缩
  2. 联网搜索（百度移动优先，DuckDuckGo 备用，无需 API key）
  3. 文件写入工具（write_file，带系统目录保护）
  4. CLI 美化：思考过程灰色，工具调用绿色，错误红色
"""
import subprocess
import json
import requests
import re
import html
import os
import sys
import time
from urllib.parse import quote

try:
    import msvcrt   # Windows 专用：非阻塞键盘监听（按 Q 打断）
except ImportError:
    msvcrt = None

from tools import load_all_tools, build_tools_schema, TOOL_REGISTRY

# 模块加载时自动扫描 tools/ 目录，注册所有工具
load_all_tools()
TOOLS_SCHEMA = build_tools_schema()


def load_env():
    """读取 .env 配置文件（~/.agent/.env 优先；不提交 Git，key 不进仓库）。
    .env 覆盖环境变量——避免残留环境变量（如旧 provider 的 API_KEY）错配"""
    candidates = [
        os.path.join(os.path.expanduser("~"), ".agent", ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                for line in open(p, encoding="utf-8"):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip()
        except Exception:
            pass


load_env()

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.environ.get("MODEL", "qwen3:4b")
API_KEY = os.environ.get("API_KEY", "")                     # 硅基流动 key：存在时走 OpenAI 兼容模式
API_BASE = os.environ.get("API_BASE", "https://api.siliconflow.cn/v1")


def is_openai_mode():
    """API_KEY 存在 → OpenAI 兼容（云端）；否则 Ollama（本地）"""
    return bool(API_KEY)
MAX_TURNS = 20       # 单轮对话中工具调用的最大轮数（写代码/分段写文件场景需要多次调用）
# 上下文按 provider 适配：云端（OpenAI 兼容）上下文大，本地 Ollama 小。
# .env 里 NUM_CTX / MAX_HISTORY 可覆盖。
NUM_CTX = int(os.environ.get("NUM_CTX", "128000" if is_openai_mode() else "16384"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "48" if is_openai_mode() else "16"))
CTX_TRIM_RATIO = 0.50   # 上下文用到 50% 时触发记忆沉淀
CTX_KEEP_RATIO = 0.20   # 沉淀后裁剪到约 20%（保留最近 40% 条数近似）
AGENT_DIR = os.path.join(os.path.expanduser("~"), ".agent")   # 数据目录：默认 ~/.agent/（可用命令行参数覆盖）
MAX_MEMORY_ENTRIES = 20   # 记忆条目上限，超过触发压缩
MEMORY_FILE = os.path.join(AGENT_DIR, "agent_memory.json")   # 记忆文件
SOUL_FILE = os.path.join(AGENT_DIR, "soul.md")   # 灵魂文件（身份设定，启动时注入 system）
WORK_FILE = os.path.join(AGENT_DIR, "work.md")    # 工作要求文件（用户的工作期望，启动时注入 system）
SKILLS_DIR = os.path.join(AGENT_DIR, "skills")    # 技能目录（用户学习的技能，跨会话保留）
BUILTIN_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")  # 内置技能（仓库自带范本，只读）

# Tavily 搜索 API key：优先读环境变量 TAVILY_API_KEY，没有就用这里填的
# 注册获取（免费额度 1000 次/月）：https://tavily.com
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")   # 注册 https://tavily.com 免费 key，设环境变量

# ---------- ANSI 颜色（CLI 美化） ----------
GRAY  = "\033[90m"   # 思考过程：灰色
GREEN = "\033[32m"   # 工具调用：绿色
RED   = "\033[31m"   # 错误：红色
BOLD  = "\033[1m"
RESET = "\033[0m"

def enable_vt():
    """Windows 终端启用 ANSI 转义支持（VT 处理）"""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(h, ctypes.byref(mode))
            kernel32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass

# ---------- 记忆系统（Hermes 式：存储 + 压缩） ----------
def load_memory():
    """读取记忆文件；不存在则返回空结构"""
    default = {"compressed": "", "entries": [], "last_seen": None, "mode": "闲聊"}
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, encoding="utf-8") as f:
                mem = json.load(f)
            if isinstance(mem, dict):
                mem.setdefault("last_seen", None)
                mem.setdefault("mode", "闲聊")   # 工作模式跨会话保持
                return mem
    except Exception:
        pass
    return default

def format_time_gap(prev_ts, now_ts):
    """把时间差格式化成友好中文"""
    gap = max(0, now_ts - prev_ts)
    if gap < 60:
        return "刚刚"
    if gap < 3600:
        return f"{int(gap // 60)} 分钟前"
    if gap < 86400:
        return f"{int(gap // 3600)} 小时 {int((gap % 3600) // 60)} 分钟前"
    days = int(gap // 86400)
    hours = int((gap % 86400) // 3600)
    return f"{days} 天 {hours} 小时前"

def build_soul_section(soul_text):
    """把灵魂文件转成清晰的 system 注入文本：
    【用户偏好】区块解析后语义明确化——'称呼'是对用户的称呼，'名字'是 AI 自己的名字，
    显式区分，避免模型把'名字'误当用户名字。"""
    if not soul_text:
        return None
    if "【用户偏好】" not in soul_text:
        return soul_text
    main_part, _ = soul_text.split("【用户偏好】", 1)
    prefs = parse_soul_prefs(soul_text)
    lines = []
    if prefs.get("称呼"):
        lines.append(f"- 称呼用户为：{prefs['称呼']}")
    if prefs.get("名字"):
        lines.append(f"- 你的名字是：{prefs['名字']}（这是用户给你起的名字，是【你 AI 的名字】，不是用户的名字）")
    if prefs.get("相处方式"):
        lines.append(f"- 相处方式：{prefs['相处方式']}")
    block = "【用户偏好】（硬规定）\n" + "\n".join(lines)
    return (main_part.rstrip() + "\n\n" + block).strip()

def build_skills_index(builtin_dir, user_dir):
    """合并内置+用户技能索引（用户同名覆盖内置）。索引常驻 system，内容按需加载"""
    from tools.skills_tools import parse_skill
    seen = set()
    lines = []
    for sdir in (user_dir, builtin_dir):   # 用户优先：同名用户技能覆盖内置
        if not os.path.isdir(sdir):
            continue
        for f in sorted(os.listdir(sdir)):
            if f.endswith(".md"):
                try:
                    with open(os.path.join(sdir, f), encoding="utf-8") as fh:
                        name, desc, _ = parse_skill(fh.read())
                    key = name or f[:-3]
                    if key in seen:
                        continue   # 用户同名技能覆盖内置，索引只出现一次
                    seen.add(key)
                    lines.append(f"- {key}：{desc or '无描述'}")
                except Exception:
                    continue
    return "\n".join(lines) if lines else "（无）"

def load_work(path=None):
    """读取工作要求文件（工作期望与职责）；不存在返回 None"""
    path = path or WORK_FILE
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return None

def load_soul(path=None):
    """读取灵魂文件（身份设定）；不存在返回 None"""
    path = path or SOUL_FILE
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return None

# ---------- 灵魂初始化：用户偏好收集（硬规定） ----------
def parse_soul_prefs(soul_text):
    """解析灵魂文件里的【用户偏好】区块；宽松匹配中英文冒号/空格。返回 {'称呼','名字','相处方式'}"""
    prefs = {"称呼": "", "名字": "", "相处方式": ""}
    if "【用户偏好】" in soul_text:
        block = soul_text.split("【用户偏好】", 1)[1]
        for line in block.splitlines():
            m = re.match(r"^\s*(称呼|名字|相处方式)\s*[:：]\s*(.+)$", line)
            if m and m.group(1) in prefs:
                prefs[m.group(1)] = m.group(2).strip()
    return prefs

def set_pref(soul_file, key, value):
    """把一条偏好写入灵魂文件的【用户偏好】区块（无则追加），立即落盘"""
    try:
        lines = open(soul_file, encoding="utf-8").read().splitlines()
    except Exception:
        lines = []
    in_block = False
    found = False
    new_lines = []
    for line in lines:
        if line.strip() == "【用户偏好】":
            in_block = True
            new_lines.append(line)
            continue
        if in_block and line.strip() == "":
            in_block = False
        if in_block and line.startswith(key + "："):
            new_lines.append(f"{key}：{value}")
            found = True
            continue
        new_lines.append(line)
    if not found:
        if not any(l.strip() == "【用户偏好】" for l in lines):
            new_lines.append("")
            new_lines.append("【用户偏好】")
        new_lines.append(f"{key}：{value}")
    try:
        with open(soul_file, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
        return True
    except Exception:
        return False

def collect_prefs(soul_file):
    """首次运行：逐项询问缺失的用户偏好（称呼/名字/相处方式），答完立即写入灵魂文件"""
    soul_text = load_soul(soul_file) or ""
    prefs = parse_soul_prefs(soul_text)
    questions = {
        "称呼": "你想让我怎么称呼你？",
        "名字": "你给我起的名字是什么？",
        "相处方式": "你希望我怎么跟你相处？",
    }
    for key, q in questions.items():
        if not prefs.get(key):
            print(f"\n{BOLD} {q}{RESET}")
            print(f"{GRAY}（直接回车可跳过此项，之后可在灵魂文件里修改）{RESET}")
            ans = input(f"{GREEN}你: {RESET}").strip()
            if not ans:
                ans = "（未指定）"   # 占位：避免每次启动都问
            set_pref(soul_file, key, ans)
            prefs[key] = ans
            print(f"{GRAY} 已写入灵魂文件{RESET}")
    # 重新读取完整灵魂（含新写入的偏好）
    return load_soul(soul_file) or ""

def save_memory(mem):
    try:
        os.makedirs(AGENT_DIR, exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"{RED} 记忆保存失败: {e}{RESET}")

def memory_to_text(mem):
    """把记忆转成注入 system 的文本"""
    parts = []
    if mem.get("compressed"):
        parts.append(f"【长期记忆】{mem['compressed']}")
    if mem.get("entries"):
        recent = [f"- {e['content']}" for e in mem["entries"][-8:]]
        if recent:
            parts.append("【近期记忆】\n" + "\n".join(recent))
    return "\n".join(parts) if parts else "（暂无）"

def add_memory(mem, content):
    """新增一条记忆。压缩延迟到主循环空闲时做——避免工具调用链中嵌套模型调用导致卡死/超时"""
    try:
        mem.setdefault("entries", []).append({
            "content": content.strip(),
            "time": time.strftime("%Y-%m-%d %H:%M")
        })
        save_memory(mem)
        return f" 已记住：{content[:60]}"
    except Exception as e:
        return f" 记忆保存失败: {e}"

def compress_memory(mem):
    """记忆压缩：调本地模型把全部旧条目总结成一条长期记忆"""
    old = mem["entries"]
    if not old:
        return
    text = "\n".join(f"- {e['content']}" for e in old[:-3])  # 保留最近 3 条不动
    prompt = (
        "下面是一个 AI 助手的记忆条目。请把它们压缩成 3-5 条简洁的长期记忆，"
        "保留用户偏好、重要事实、常用配置，删除过时细节。只输出压缩结果：\n" + text
    )
    try:
        summary = llm_once([{"role": "user", "content": prompt}], num_ctx=4096, think=False)
        if summary:
            old_sum = mem.get("compressed", "")
            mem["compressed"] = (old_sum + "\n" + summary).strip() if old_sum else summary
            mem["entries"] = old[-3:]
            save_memory(mem)   # 落盘！否则压缩结果只活在内存，重启丢失
            print(f"\n{GRAY}[记忆压缩] {len(old)} 条 → 长期记忆（已保存）{RESET}")
    except Exception as e:
        print(f"{RED} 记忆压缩失败: {e}{RESET}")

# ---------- 工具定义 ----------
# ---------- 工具定义：由 tools/ 目录自动加载（TOOLS_SCHEMA） ----------
# ---------- 工具分发（注册表） ----------
def run_tool(name, args, ctx):
    """从注册表取工具执行；崩溃不退出，错误汇报给 AI 决策"""
    info = TOOL_REGISTRY.get(name)
    if not info:
        return f"未知工具: {name}"
    try:
        return info["handler"](args, ctx)
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"[:200]
        return (f" 工具 {name} 执行出错: {err_msg}。"
                "请根据这个错误决定：尝试其他方法，或告知用户无法完成。")

# ---------- 上下文裁剪（先沉淀，后裁剪） ----------
def condense_dialog(text):
    """把将被裁剪掉的对话压缩成要点（沉淀进记忆，不丢失早期内容）"""
    prompt = (
        "下面是一段对话记录。请提取其中值得长期记住的要点："
        "用户的偏好、身份信息、重要事实、任务进展、明确要求。"
        "最多 5 条，每条一句话，忽略寒暄。只输出要点列表：\n" + text
    )
    try:
        summary = llm_once([{"role": "user", "content": prompt}], num_ctx=4096, think=False)
        return summary if summary else None
    except Exception as e:
        print(f"{RED} 对话沉淀失败: {e}{RESET}")
        return None

def trim_history(messages, mem, max_len=MAX_HISTORY):
    """裁剪前先把要丢的对话沉淀进记忆（压缩而非丢失）。
    留余量 + 小裁剪不沉淀：避免触发一次后每轮都触发"""
    system = [m for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    keep = max_len - 4   # 留余量：一次裁到 max_len-4，2-3 轮才触发一次裁剪
    if len(rest) > max_len:
        drop = rest[:len(rest) - keep]
        # 只取被裁对话的后 8 条（限制压缩输入，加快速度）
        dialog = []
        for m in drop[-8:]:
            role = m["role"]
            content = str(m.get("content", ""))[:300]
            if role in ("user", "assistant") and content and not m.get("tool_calls"):
                dialog.append(f"{'用户' if role == 'user' else '助手'}: {content}")
        # 少于 3 条有意义的对话不值得调模型压缩，直接丢
        if len(dialog) >= 3:
            # 先提示再干活——沉淀要调本地模型压缩，几秒到十几秒，不提示会以为卡了
            print(f"{GRAY} 记忆沉淀：压缩早期对话中...{RESET}", end="", flush=True)
            summary = condense_dialog("\n".join(dialog))
            if summary:
                add_memory(mem, f"[对话沉淀] {summary}")
                print(f"  已存入记忆（{len(summary)} 字）{RESET}")
            else:
                print(f"（无值得沉淀的内容）{RESET}")
        rest = rest[-keep:]
    return system + rest

def trim_to_ratio(messages, mem, keep_ratio):
    """按 token 百分比沉淀裁剪：上下文用到阈值时调用。
    沉淀早期对话进记忆，然后保留最近 keep_ratio 比例的消息
    （token 比例用条数比例近似——无分词器，平均消息长度相近时足够准）"""
    system = [m for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    keep = max(int(len(rest) * keep_ratio), 8)   # 至少保留 8 条
    if len(rest) <= keep:
        return messages
    drop = rest[:len(rest) - keep]
    dialog = []
    for m in drop[-8:]:
        role = m["role"]
        content = str(m.get("content", ""))[:300]
        if role in ("user", "assistant") and content and not m.get("tool_calls"):
            dialog.append(f"{'用户' if role == 'user' else '助手'}: {content}")
    if len(dialog) >= 3:
        print(f"{GRAY} 记忆沉淀：压缩早期对话中...{RESET}", end="", flush=True)
        summary = condense_dialog("\n".join(dialog))
        if summary:
            add_memory(mem, f"[对话沉淀] {summary}")
            print(f" ✅ 已存入记忆（{len(summary)} 字）{RESET}")
        else:
            print(f"（无值得沉淀的内容）{RESET}")
    rest = rest[-keep:]
    return system + rest

# ---------- CLI Markdown 轻量渲染（流式状态机：粗体/代码块/表格行） ----------
MD_BOLD = "\033[1m"
MD_CODE = "\033[36m"    # 代码块：青色
MD_TABLE = "\033[35m"   # 表格：品红
md_state = {"code": False}

def render_md(piece, st):
    """流式 markdown 轻量渲染：```代码块``` / **粗体** / 表格行（| 开头）。
    跨 chunk 的 ** 或表格边界可能漏处理——CLI 级够用即可"""
    # 1. 代码块开关
    if "```" in piece:
        if not st["code"]:
            st["code"] = True
            piece = piece.replace("```", "", 1)
            return MD_CODE + piece
        else:
            st["code"] = False
            piece = piece.replace("```", "", 1)
            return RESET + piece
    if st["code"]:
        return piece   # 代码块内原样（已上色）
    # 2. 粗体 **：成对切换
    if "**" in piece:
        parts = piece.split("**")
        out = []
        for i, p in enumerate(parts):
            if p:
                out.append(p)
            if i < len(parts) - 1:
                out.append(MD_BOLD if i % 2 == 0 else RESET)
        piece = "".join(out)
    # 3. 表格行：行首或换行后 | 开头 → 表格色（代码块外）
    import re
    if re.search(r"(^|\n)\|", piece):
        piece = MD_TABLE + piece + RESET
    return piece

def read_keys():
    """非阻塞读取按键：'q'=打断输出 / 't'=展开收起思考 / [] = 无按键"""
    if msvcrt is None:
        return []
    keys = []
    try:
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'q', b'Q'):
                keys.append('q')
            elif ch in (b't', b'T'):
                keys.append('t')
    except Exception:
        pass
    return keys

# ---------- 调用 Ollama（流式 + 灰色思考 + Q 打断） ----------
def llm_once(messages, num_ctx=4096, think=False):
    """非流式单次请求（记忆压缩/沉淀用）。OpenAI 模式走云端，否则走 Ollama"""
    if is_openai_mode():
        payload = {"model": MODEL, "messages": messages, "stream": False,
                   "temperature": 0.3, "max_tokens": 1024}
        if "qwen3" in MODEL.lower() or "deepseek" in MODEL.lower():
            payload["thinking"] = {"type": "enabled"} if think else {"type": "disabled"}
        headers = {"Authorization": "Bearer " + API_KEY}
        resp = requests.post(API_BASE + "/chat/completions", json=payload,
                             headers=headers, timeout=(10, 300))
        return resp.json()["choices"][0]["message"]["content"].strip()
    req = {"model": MODEL, "messages": messages, "stream": False,
           "options": {"num_ctx": num_ctx}}
    if "qwen3" in MODEL.lower():
        req["think"] = think
    resp = requests.post(OLLAMA_URL, json=req, timeout=(10, 300))
    return resp.json()["message"]["content"].strip()


def call_openai_stream(messages):
    """流式对话（OpenAI 兼容，如硅基流动）。UI 逻辑与 Ollama 版一致：
    思考折叠/中转词/按键。返回 (msg, is_tool, prompt_tokens)"""
    payload = {"model": MODEL, "messages": messages, "stream": True, "temperature": 0.7}
    if "qwen3" in MODEL.lower() or "deepseek" in MODEL.lower():
        payload["thinking"] = {"type": "enabled"}
    headers = {"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"}
    resp = requests.post(API_BASE + "/chat/completions", json=payload, headers=headers,
                         stream=True, timeout=(10, 600))
    resp.raise_for_status()

    import queue as _queue
    import threading
    q = _queue.Queue()

    def _reader():
        try:
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                if line.startswith("data: "):
                    d = line[6:].strip()
                    if d == "[DONE]":
                        break
                    try:
                        q.put(json.loads(d))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            q.put({"__error__": str(e)})
        finally:
            q.put({"__done__": True})

    threading.Thread(target=_reader, daemon=True).start()

    content_parts, think_parts = [], []
    tool_calls_acc = {}
    prompt_tokens = 0
    finish_seen = False
    think_expanded = False
    think_ended = False
    think_start_ts = time.time()
    last_activity = time.time()
    tool_calls = None

    while True:
        keys = read_keys()
        if 'q' in keys:
            print(f"\r{RED} 已打断{RESET}   ", flush=True)
            resp.close()
            return {"role": "assistant", "content": "".join(content_parts)}, False, 0
        if 't' in keys:
            think_expanded = not think_expanded
            if think_expanded:
                total = len("".join(think_parts))
                if total > 500:
                    print(f"\r{RESET}\n{GRAY} [展开思考 · 共 {total} 字 · 显示末尾 300 字]{RESET}\n", end="", flush=True)
                    print(f"{GRAY}{''.join(think_parts)[-300:]}{RESET}", end="", flush=True)
                else:
                    print(f"\r{RESET}\n{GRAY} [展开思考]{RESET}\n", end="", flush=True)
                    print(f"{GRAY}{''.join(think_parts)}{RESET}", end="", flush=True)
            else:
                print(f"{RESET}\n{GRAY} [收起思考]{RESET}", end="", flush=True)

        try:
            data = q.get(timeout=0.2)
            last_activity = time.time()
        except _queue.Empty:
            if finish_seen:
                break
            idle = time.time() - last_activity
            if think_parts and not tool_calls and not content_parts and idle > 2:
                print(f"\r{GRAY} 正在生成工具调用...（已 {idle:.0f}s / Q 打断）{RESET}", end="", flush=True)
            continue

        if data.get("__done__"):
            break
        if data.get("__error__"):
            print(f"\n{RED} 流错误: {data['__error__']}{RESET}")
            break
        if data.get("usage"):
            prompt_tokens = data["usage"].get("prompt_tokens", 0) or prompt_tokens

        choices = data.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                idx = tc.get("index", 0)
                acc = tool_calls_acc.setdefault(idx, {"name": "", "args": ""})
                fn = tc.get("function") or {}
                acc["name"] += fn.get("name") or ""
                acc["args"] += fn.get("arguments") or ""
        reasoning = delta.get("reasoning_content")
        if reasoning:
            think_parts.append(reasoning)
            if think_expanded:
                print(f"{GRAY}{reasoning}{RESET}", end="", flush=True)
            else:
                tl = len("".join(think_parts))
                print(f"\r{GRAY} 思考中... {tl} 字（T 展开 / Q 打断）{RESET}", end="", flush=True)
        piece = delta.get("content")
        if piece:
            if not think_ended:
                think_ended = True
                if think_parts and not think_expanded:
                    tl = len("".join(think_parts))
                    print(f"\r{GRAY} 思考完成：{tl} 字 · {time.time()-think_start_ts:.1f}s（T 展开）{RESET}", end="", flush=True)
                print(f"{RESET}\n", end="", flush=True)
            content_parts.append(piece)
            print(render_md(piece, md_state), end="", flush=True)
        if choices[0].get("finish_reason") in ("stop", "tool_calls"):
            finish_seen = True

    if tool_calls_acc:
        tcs = []
        for idx in sorted(tool_calls_acc):
            acc = tool_calls_acc[idx]
            tcs.append({"function": {"name": acc["name"], "arguments": acc["args"]}})
        tool_calls = tcs
        if think_parts and not think_ended and not think_expanded:
            tl = len("".join(think_parts))
            print(f"\r{GRAY} 思考完成：{tl} 字 · {time.time()-think_start_ts:.1f}s（T 展开）{RESET}", end="", flush=True)
        print(f"{RESET}", flush=True)
        return {"role": "assistant", "content": "".join(content_parts), "tool_calls": tool_calls}, True, prompt_tokens
    print(f"{RESET}", flush=True)
    return {"role": "assistant", "content": "".join(content_parts)}, False, prompt_tokens


def call_ollama_stream(messages):
    """流式调用 Ollama。后台线程读流 + 队列轮询——
    队列空档（模型静默）时显示"中转词"判定阶段：思考中/生成工具调用中，不用靠猜超时"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "stream": True,
        "options": {"num_ctx": NUM_CTX}
    }
    if "qwen3" in MODEL.lower():
        payload["think"] = True   # qwen3 专属：思考模式；qwen2.5 等模型传了会 400
    # 读超时 600s 纯兜底（中转词已保证阶段可见，超时只防真死锁）
    resp = requests.post(OLLAMA_URL, json=payload, stream=True, timeout=(10, 600))
    resp.raise_for_status()

    import queue as _queue
    import threading

    q = _queue.Queue()

    def _reader():
        """后台线程：逐行读流，块放入队列"""
        try:
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                q.put(data)
        except Exception as e:
            q.put({"__error__": str(e)})
        finally:
            q.put({"__done__": True})

    threading.Thread(target=_reader, daemon=True).start()

    content_parts = []
    think_parts = []
    tool_calls = None
    prompt_tokens = 0
    think_expanded = False      # 思考默认收起
    think_ended = False
    think_start_ts = time.time()
    last_activity = time.time()   # 最后一次收到数据的时间

    while True:
        # 按键：Q 打断 / T 展开收起思考（每轮循环都检查，静默期也能响应）
        keys = read_keys()
        if 'q' in keys:
            print(f"\r{RED} 已打断{RESET}   ", flush=True)
            resp.close()
            return {"role": "assistant", "content": "".join(content_parts)}, False, 0
        if 't' in keys:
            think_expanded = not think_expanded
            if think_expanded:
                total = len("".join(think_parts))
                if total > 500:
                    # 防刷屏：只显示末尾 300 字，后续增量实时追加
                    print(f"\r{RESET}\n{GRAY} [展开思考 · 共 {total} 字 · 显示末尾]{RESET}\n", end="", flush=True)
                    print(f"{GRAY}{''.join(think_parts)[-300:]}{RESET}", end="", flush=True)
                else:
                    print(f"\r{RESET}\n{GRAY} [展开思考]{RESET}\n", end="", flush=True)
                    print(f"{GRAY}{''.join(think_parts)}{RESET}", end="", flush=True)
            else:
                print(f"{RESET}\n{GRAY} [收起思考]{RESET}", end="", flush=True)

        # 取块：0.2s 超时——空档期显示中转词
        try:
            data = q.get(timeout=0.2)
            last_activity = time.time()
        except _queue.Empty:
            idle = time.time() - last_activity
            # 中转词：有思考但没出正文/工具调用 + 静默 2s → 正在生成工具调用（参数很长，静默正常）
            if think_parts and not tool_calls and not content_parts and idle > 2:
                print(f"\r{GRAY} 正在生成工具调用...（已 {idle:.0f}s / Q 打断）{RESET}", end="", flush=True)
            continue

        if data.get("__done__"):
            break
        if data.get("__error__"):
            print(f"\n{RED} 流错误: {data['__error__']}{RESET}")
            break
        if data.get("prompt_eval_count"):
            prompt_tokens = data["prompt_eval_count"]   # 输入 token 量（百分比沉淀用）

        msg = data.get("message", {})
        if msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]
            break
        think_piece = msg.get("thinking")
        if think_piece:
            think_parts.append(think_piece)
            if think_expanded:
                print(f"{GRAY}{think_piece}{RESET}", end="", flush=True)
            else:
                # 收起模式：同一行动态刷新字数——长思考时证明它在干活，不是卡死
                think_len_now = len("".join(think_parts))
                print(f"\r{GRAY} 思考中... {think_len_now} 字（T 展开 / Q 打断）{RESET}", end="", flush=True)
        piece = msg.get("content")
        if piece:
            if not think_ended:
                think_ended = True
                # 思考结束：\r 覆盖"思考中...N字"状态行，显示摘要
                if think_parts and not think_expanded:
                    think_len = len("".join(think_parts))
                    think_secs = time.time() - think_start_ts
                    print(f"\r{GRAY} 思考完成：{think_len} 字 · {think_secs:.1f}s（T 展开）{RESET}", end="", flush=True)
                print(f"{RESET}\n", end="", flush=True)
            content_parts.append(piece)
            print(render_md(piece, md_state), end="", flush=True)

    if tool_calls is not None:
        # 思考完直接调工具（无 content）：\r 覆盖状态行后显示摘要
        if think_parts and not think_ended and not think_expanded:
            think_len = len("".join(think_parts))
            print(f"\r{GRAY} 思考完成：{think_len} 字 · {time.time()-think_start_ts:.1f}s（T 展开）{RESET}", end="", flush=True)
        print(f"{RESET}", flush=True)
        return {"role": "assistant", "content": "", "tool_calls": tool_calls}, True, prompt_tokens
    print(f"{RESET}", flush=True)
    return {"role": "assistant", "content": "".join(content_parts)}, False, prompt_tokens

# ---------- 主循环 ----------
def main():
    global MEMORY_FILE, SOUL_FILE
    # 命令行参数：python my_agent_v6.py [记忆文件路径] [灵魂文件路径]
    if len(sys.argv) > 1:
        MEMORY_FILE = sys.argv[1]
    if len(sys.argv) > 2:
        SOUL_FILE = sys.argv[2]

    enable_vt()
    mem = load_memory()
    soul = load_soul()
    work_req = load_work()

    # 灵魂初始化（硬规定）：
    #  - 无 soul 文件 → 引导用户创建并收集偏好
    #  - 有 soul 但偏好不全 → 逐项补齐，答完写入 soul 文件
    if not soul:
        print(f"\n{BOLD} 未找到灵魂文件（{SOUL_FILE}）{RESET}")
        print(f"{GRAY}灵魂文件里可以定义我的身份、你的偏好，让我更懂你{RESET}")
        create = input(f"{GREEN}要不要先认识一下？输入 y 创建，其他跳过: {RESET}").strip().lower()
        if create == "y":
            soul = collect_prefs(SOUL_FILE)
            print(f"{GRAY} 灵魂文件已创建，开始干活{RESET}")
    elif any(not v for v in parse_soul_prefs(soul).values()):
        print(f"\n{BOLD} 首次使用：先认识你一下{RESET}")
        soul = collect_prefs(SOUL_FILE)
        print(f"{GRAY} 灵魂文件已完善，开始干活{RESET}")

    # 工具上下文：注入给所有工具的共享状态
    ctx = {
        "mem": mem,
        "add_memory": add_memory,
        "save_memory": save_memory,
        "soul_file": SOUL_FILE,
        "set_pref": set_pref,
        "skills_dir": SKILLS_DIR,
        "builtin_skills_dir": BUILTIN_SKILLS_DIR,
    }
    os.makedirs(SKILLS_DIR, exist_ok=True)
    skills_index = build_skills_index(BUILTIN_SKILLS_DIR, SKILLS_DIR)

    mem_text = memory_to_text(mem)

    # ---- 时间感知：当前时间 + 上次会话间隔 ----
    now_ts = time.time()
    now_local = time.localtime()
    time_str = time.strftime("%Y年%m月%d日 %H:%M", now_local)
    weekday = "一二三四五六日"[now_local.tm_wday]
    last_seen = mem.get("last_seen")
    gap_text = format_time_gap(last_seen, now_ts) if last_seen else "首次运行"
    mem["last_seen"] = now_ts
    save_memory(mem)   # 记录本次运行时间，供下次对比

    system_prompt = (
        "你是一个运行在本地的小型助手，能调用 12 个工具："
        "list_files（列目录）、read_file（读文件）、echo_message（打印）、execute_shell（执行cmd命令）、"
        "write_file（写文件）、search_web（联网搜索）、update_pref（更新用户偏好）、remember（记住重要信息）、"
        "list_skills（列技能）、load_skill（加载技能步骤）、learn_skill（学习保存技能）、set_mode（切换工作模式）。\n"
        "使用原则：\n"
        "1. 日常闲聊、问答、寒暄直接回答，不要调用任何工具，也不要考虑用户是否想用其他工具。\n"
        "2. 只有当任务确实需要时才调用对应工具：查文件用 list_files、执行命令用 execute_shell、"
        "写文件用 write_file、查最新资料用 search_web、用户要求记住时用 remember、"
        "用户明确要求改变称呼/名字/相处方式时用 update_pref。\n"
        "3. 技能机制：遇到任务先看【可用技能】索引，匹配就用 load_skill 加载步骤执行；"
        "用户教了新方法/完成可复用流程/用户说'记住这个方法'时，用 learn_skill 总结保存。\n"
        "4. 复杂任务可以自动规划多步工具链：一次调一个工具，等结果返回后自己判断下一步。\n"
        "   例如用户说'帮我记住我的设备信息'：先用 execute_shell 查询系统信息，再用 remember 记住结果。\n"
        "5. 遇到不常见或复杂的命令/工具用法不明确时，不要凭猜测执行：先用 execute_shell 执行 '<命令> -help'、'<命令> /?' 或 'help <命令>' 查看帮助，或用 search_web 搜索用法。日常简单命令（dir、ipconfig、type 等）直接用。\n"
        "6. 不要猜测用户想要用哪个工具——任务需要什么就用什么，不需要就不调用。\n"
        "7. 工具结果能直接回答用户时，用简洁中文总结，不重复输出原始内容。\n"
        f"\n【当前工作模式】{mem.get('mode', '闲聊')}——回答风格：闲聊=轻松简短；工作=严谨高效；写代码=直接给完整代码；查资料=先搜索再回答。用户说'进入xx模式'时用 set_mode 切换。\n"
        f"\n【可用技能】（索引，需要时用 load_skill 加载全文）\n{skills_index}\n"
        f"\n【时间】当前：{time_str}（星期{weekday}）。距离上次会话：{gap_text}。\n"
        f"【历史记忆】（跨会话保留，供参考）\n{mem_text}"
    )
    if soul:
        soul_section = build_soul_section(soul)
        if soul_section:
            system_prompt = soul_section + "\n\n" + system_prompt   # 灵魂（身份）放最前，优先级最高
    if work_req:
        system_prompt = "【工作要求与职责】（硬性规定，必须遵守）\n" + work_req + "\n\n" + system_prompt
    messages = [{"role": "system", "content": system_prompt}]
    print(f"{BOLD}Local Agent 已启动（{MODEL}）{RESET}")
    print(f"{GRAY} 已读取记忆文件: {MEMORY_FILE}{RESET}")
    print(f"{GRAY} 灵魂文件: {SOUL_FILE}（{'已注入' if soul else '未找到，跳过'}）{RESET}")
    print(f"{GRAY} 工作要求: {WORK_FILE}（{'已注入' if work_req else '未配置，可复制 work.example.md 创建'}）{RESET}")
    print(f"{GRAY}   近期记忆 {len(mem.get('entries', []))} 条 | 长期压缩 {'有' if mem.get('compressed') else '无'} | 模式: {mem.get('mode', '闲聊')}{RESET}")
    print(f"{GRAY}输入 'exit' 退出 | '记住：xxx' 直接存记忆 | 生成时 T=展开/收起思考 Q=打断{RESET}")

    while True:
        # 记忆超量时在空闲时压缩（不在工具调用链里做，防嵌套模型调用卡死）
        if len(mem.get("entries", [])) > MAX_MEMORY_ENTRIES:
            compress_memory(mem)
        try:
            user = input(f"\n{GREEN}你: {RESET}")
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in ["exit", "quit", "退出"]:
            break

        # 手动记忆："记住：xxx" 直接存，不走模型
        if user.startswith("记住：") or user.startswith("记住:"):
            content = user.split("：", 1)[-1] if "：" in user else user.split(":", 1)[-1]
            print(add_memory(mem, content))
            continue

        messages.append({"role": "user", "content": user})
        turns = 0
        last_prompt_tokens = 0
        while turns < MAX_TURNS:
            turns += 1
            t_round = time.time()   # 本轮耗时计时
            try:
                if is_openai_mode():
                    msg, is_tool, prompt_tokens = call_openai_stream(messages)
                else:
                    msg, is_tool, prompt_tokens = call_ollama_stream(messages)
                if prompt_tokens:
                    last_prompt_tokens = prompt_tokens
            except requests.exceptions.ReadTimeout:
                print(f"\n{RED} 模型 5 分钟没有新输出（可能在生成很长的工具参数，也可能卡住了）。按 Q 打断，或再发一条消息继续。{RESET}")
                break
            except Exception as e:
                print(f"\n{RED}错误: {e}{RESET}")
                break

            if is_tool:
                messages.append(msg)
                for tc in msg["tool_calls"]:
                    func_name = tc["function"]["name"]
                    args = tc["function"]["arguments"]
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    t_tool = time.time()
                    result = run_tool(func_name, args, ctx)
                    elapsed = time.time() - t_tool
                    print(f"{GREEN}[调用工具] {func_name}({json.dumps(args, ensure_ascii=False)}) ({elapsed:.1f}s){RESET}")
                    messages.append({"role": "tool", "content": result})
                continue
            else:
                messages.append(msg)
                # 显示本轮生成耗时
                gen_elapsed = time.time() - t_round
                print(f"{GRAY} 本轮耗时 {gen_elapsed:.1f}s{RESET}")
                break
        else:
            print(f"\n{RED} 工具调用已达 {MAX_TURNS} 次上限，本轮终止。任务没完成的话，再发一条消息我接着干。{RESET}")

        # 上下文管理：token 百分比触发沉淀（50% → 20%），条数机制兜底
        if last_prompt_tokens and last_prompt_tokens > NUM_CTX * CTX_TRIM_RATIO:
            pct = int(last_prompt_tokens / NUM_CTX * 100)
            print(f"{GRAY} 上下文 {pct}%/{NUM_CTX}，触发沉淀缩减...{RESET}")
            messages = trim_to_ratio(messages, mem, CTX_KEEP_RATIO)
        else:
            messages = trim_history(messages, mem)

if __name__ == "__main__":
    main()
