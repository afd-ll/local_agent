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

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:4b"
MAX_TURNS = 5        # 单轮对话中工具调用的最大轮数，防死循环
MAX_HISTORY = 16     # 滑动窗口：16K 上下文下保留最近 16 条消息（24 条含工具结果易超窗）
AGENT_DIR = os.path.join(os.path.expanduser("~"), ".agent")   # 数据目录：默认 ~/.agent/（可用命令行参数覆盖）
MAX_MEMORY_ENTRIES = 20   # 记忆条目上限，超过触发压缩
MEMORY_FILE = os.path.join(AGENT_DIR, "agent_memory.json")   # 记忆文件
SOUL_FILE = os.path.join(AGENT_DIR, "soul.md")   # 灵魂文件（身份设定，启动时注入 system）

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
    default = {"compressed": "", "entries": [], "last_seen": None}
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, encoding="utf-8") as f:
                mem = json.load(f)
            if isinstance(mem, dict):
                mem.setdefault("last_seen", None)
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

def save_memory(mem):
    try:
        os.makedirs(AGENT_DIR, exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"{RED}⚠️ 记忆保存失败: {e}{RESET}")

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
        return f"✅ 已记住：{content[:60]}"
    except Exception as e:
        return f"❌ 记忆保存失败: {e}"

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
        resp = requests.post(OLLAMA_URL, json={
            "model": MODEL, "messages": [
                {"role": "user", "content": prompt}
            ], "stream": False, "think": False,
            "options": {"num_ctx": 4096}   # 压缩任务小上下文，不与主对话抢显存
        }, timeout=120)
        summary = resp.json()["message"]["content"].strip()
        if summary:
            old_sum = mem.get("compressed", "")
            mem["compressed"] = (old_sum + "\n" + summary).strip() if old_sum else summary
            mem["entries"] = old[-3:]
            save_memory(mem)   # 落盘！否则压缩结果只活在内存，重启丢失
            print(f"\n{GRAY}[记忆压缩] {len(old)} 条 → 长期记忆（已保存）{RESET}")
    except Exception as e:
        print(f"{RED}⚠️ 记忆压缩失败: {e}{RESET}")

# ---------- 工具定义 ----------
tools = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出指定目录下的文件和文件夹（Windows dir 命令）。directory 参数为要列出的目录路径，省略则默认当前目录。",
            "parameters": {"type": "object", "properties": {"directory": {"type": "string", "default": "."}}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "echo_message",
            "description": "在命令行中打印一条消息，用于测试和确认。",
            "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "在 Windows 上执行一条 cmd 命令并返回输出。command 参数必须是完整的 cmd 命令字符串。危险操作（删除、格式化、关机）会被拦截。",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把文本内容写入指定文件（UTF-8 编码，自动创建目录）。path 为完整路径，content 为要写入的内容。系统目录禁止写入。",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "联网搜索（百度/必应）。query 为搜索关键词，返回前 5 条结果标题和摘要。",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "把一条重要信息存入长期记忆（跨会话保留）。用户明确要求记住、或者对话中出现需要长期保存的事实/偏好时调用。content 为要记住的内容。",
            "parameters": {"type": "object", "properties": {"content": {"type": "string"}}}
        }
    }
]

# ---------- 工具函数实现 ----------
def list_files(directory="."):
    try:
        result = subprocess.run(["cmd", "/c", "dir", directory], capture_output=True, text=True, timeout=10)
        return result.stdout
    except Exception as e:
        return f"执行出错: {e}"

def echo_message(message):
    return f"已执行: echo {message}"

def execute_shell(command):
    dangerous = ["rm -rf", "del /f", "del /q", "format", "shutdown", "rd /s", "rmdir /s",
                 "diskpart", "reg delete", "taskkill /f /im", "cipher /w", "vssadmin delete"]
    for kw in dangerous:
        if kw in command.lower():
            return f"❌ 禁止执行含有 '{kw}' 的命令。"
    try:
        result = subprocess.run(["cmd", "/c", command], capture_output=True, text=True, encoding='gbk', errors='replace', timeout=30)
        if result.returncode == 0:
            return f"✅ 执行成功:\n{result.stdout}"
        else:
            return f"❌ 执行失败:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "⏰ 超时"
    except Exception as e:
        return f"💥 {e}"

def write_file(path, content):
    """文件写入（UTF-8），系统目录保护（动态识别用户目录）"""
    low = path.lower()
    user_dir = os.environ.get("USERPROFILE", "").lower() or os.environ.get("HOME", "").lower()
    blocked = ["c:\\windows", "c:\\program files", "c:\\$recycle", "system32",
               "c:\\boot", "c:\\programdata"]
    if user_dir:
        blocked.append(os.path.join(user_dir, "appdata"))
        blocked.append(os.path.join(user_dir, "desktop"))   # 桌面也保护，防乱写
    for b in blocked:
        if b and low.startswith(b):
            return f"❌ 禁止写入系统目录: {b}"
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"💥 {e}"

def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

def search_tavily(query):
    """Tavily 搜索（外部 API，结果质量高）：https://api.tavily.com/search"""
    if not TAVILY_API_KEY:
        return None   # 无 key，交给 fallback
    try:
        resp = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "include_answer": True      # 让 Tavily 生成短答案，直接可用
        }, timeout=20)
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        out = []
        answer = data.get("answer")
        if answer:
            out.append(f"💡 摘要：{answer[:300]}")
        for i, r in enumerate(results[:5]):
            out.append(f"{i+1}. {r.get('title', '')}\n   {r.get('url', '')}\n   {(r.get('content', '') or '')[:150]}")
        return "搜索结果：\n" + "\n".join(out)
    except Exception as e:
        return None

def search_web(query):
    """联网搜索：Tavily（外部API）优先 → 百度移动 → DuckDuckGo"""
    r = search_tavily(query)
    if r:
        return r
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"}
    results = []
    # 1) 百度移动版（国内可用，玄枢爬虫验证过）
    try:
        url = "https://m.baidu.com/s?word=" + quote(query)
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "utf-8"
        titles = re.findall(r"<h3[^>]*>(.*?)</h3>", r.text, re.S)
        abstracts = re.findall(r'class="c-abstract[^"]*"[^>]*>(.*?)</div>', r.text, re.S)
        for i, t in enumerate(titles[:5]):
            results.append(f"{i+1}. {_strip_html(t)}")
            if i < len(abstracts):
                results.append(f"   {_strip_html(abstracts[i])[:120]}")
    except Exception:
        pass
    # 2) DuckDuckGo Lite 备用（海外网络可用）
    if not results:
        try:
            url = "https://lite.duckduckgo.com/lite/?q=" + quote(query)
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.encoding = "utf-8"
            links = re.findall(r'<a rel="nofollow"[^>]*>(.*?)</a>', r.text, re.S)
            snippets = re.findall(r'class="result-snippet">(.*?)</td>', r.text, re.S)
            for i, t in enumerate(links[:5]):
                results.append(f"{i+1}. {_strip_html(t)}")
                if i < len(snippets):
                    results.append(f"   {_strip_html(snippets[i])[:120]}")
        except Exception:
            pass
    if results:
        return "搜索结果：\n" + "\n".join(results)
    return "❌ 搜索无结果或网络失败"

def remember(mem, content):
    """记住一条重要信息（跨会话保存到记忆文件）"""
    return add_memory(mem, content)

# ---------- 工具分发 ----------
def run_tool(name, args, mem):
    if name == "list_files":
        return list_files(args.get("directory", "."))
    elif name == "echo_message":
        return echo_message(args.get("message", ""))
    elif name == "execute_shell":
        return execute_shell(args.get("command", ""))
    elif name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
    elif name == "search_web":
        return search_web(args.get("query", ""))
    elif name == "remember":
        return remember(mem, args.get("content", ""))
    else:
        return f"未知工具: {name}"

# ---------- 上下文裁剪（先沉淀，后裁剪） ----------
def condense_dialog(text):
    """把将被裁剪掉的对话压缩成要点（沉淀进记忆，不丢失早期内容）"""
    prompt = (
        "下面是一段对话记录。请提取其中值得长期记住的要点："
        "用户的偏好、身份信息、重要事实、任务进展、明确要求。"
        "最多 5 条，每条一句话，忽略寒暄。只输出要点列表：\n" + text
    )
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "think": False,
            "options": {"num_ctx": 8192}
        }, timeout=120)
        summary = resp.json()["message"]["content"].strip()
        return summary if summary else None
    except Exception as e:
        print(f"{RED}⚠️ 对话沉淀失败: {e}{RESET}")
        return None

def trim_history(messages, mem, max_len=MAX_HISTORY):
    """裁剪前先把要丢的对话沉淀进记忆（压缩而非丢失）"""
    system = [m for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    if len(rest) > max_len:
        drop = rest[:len(rest) - max_len]   # 即将被裁剪的早期消息
        # 只沉淀有意义的对话（跳过纯工具调用/结果）
        dialog = []
        for m in drop:
            role = m["role"]
            content = str(m.get("content", ""))[:300]
            if role in ("user", "assistant") and content and not m.get("tool_calls"):
                dialog.append(f"{'用户' if role == 'user' else '助手'}: {content}")
        if dialog:
            summary = condense_dialog("\n".join(dialog))
            if summary:
                add_memory(mem, f"[对话沉淀] {summary}")
                print(f"{GRAY}[记忆沉淀] 早期对话已压缩存入记忆{RESET}")
        rest = rest[-max_len:]
    return system + rest

def check_interrupt():
    """检测是否按了 Q 键（打断当前输出）。非阻塞，Windows msvcrt 实现"""
    if msvcrt is None:
        return False
    try:
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'q', b'Q'):
                return True
    except Exception:
        pass
    return False

# ---------- 调用 Ollama（流式 + 灰色思考 + Q 打断） ----------
def call_ollama_stream(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "stream": True,
        "think": True,           # qwen3 保留思考模式
        "options": {"num_ctx": 16384}
    }
    resp = requests.post(OLLAMA_URL, json=payload, stream=True, timeout=120)
    resp.raise_for_status()

    content_parts = []
    tool_calls = None
    think_started = False
    content_started = False
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = data.get("message", {})
        # 按 Q 打断当前输出
        if check_interrupt():
            print(f"\n{RED}⏹ 已打断{RESET}", flush=True)
            resp.close()
            return {"role": "assistant", "content": "".join(content_parts)}, False
        if msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]
            break
        think_piece = msg.get("thinking")
        if think_piece:
            if not think_started:
                print(f"\n{GRAY}", end="", flush=True)
                think_started = True
            print(f"{think_piece}", end="", flush=True)
        piece = msg.get("content")
        if piece:
            if think_started and not content_started:
                print(f"{RESET}\n", flush=True)   # 思考结束，恢复颜色换行
                content_started = True
            content_parts.append(piece)
            print(piece, end="", flush=True)

    if tool_calls is not None:
        print(f"{RESET}", flush=True)
        return {"role": "assistant", "content": "", "tool_calls": tool_calls}, True
    print(f"{RESET}", flush=True)
    return {"role": "assistant", "content": "".join(content_parts)}, False

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
        "你是一个运行在本地的小型助手，能调用 6 个工具："
        "list_files（列目录）、echo_message（打印）、execute_shell（执行cmd命令）、"
        "write_file（写文件）、search_web（联网搜索）、remember（记住重要信息）。\n"
        "使用原则：\n"
        "1. 日常闲聊、问答、寒暄直接回答，不要调用任何工具，也不要考虑用户是否想用其他工具。\n"
        "2. 只有当任务确实需要时才调用对应工具：查文件用 list_files、执行命令用 execute_shell、"
        "写文件用 write_file、查最新资料用 search_web、用户要求记住时用 remember。\n"
        "3. 复杂任务可以自动规划多步工具链：一次调一个工具，等结果返回后自己判断下一步。\n"
        "   例如用户说'帮我记住我的设备信息'：先用 execute_shell 查询系统信息，再用 remember 记住结果。\n"
        "4. 遇到不常见或复杂的命令/工具用法不明确时，不要凭猜测执行：先用 execute_shell 执行 '<命令> -help'、'<命令> /?' 或 'help <命令>' 查看帮助，或用 search_web 搜索用法。日常简单命令（dir、ipconfig、type 等）直接用。\n"
        "5. 不要猜测用户想要用哪个工具——任务需要什么就用什么，不需要就不调用。\n"
        "6. 工具结果能直接回答用户时，用简洁中文总结，不重复输出原始内容。\n"
        f"\n【时间】当前：{time_str}（星期{weekday}）。距离上次会话：{gap_text}。\n"
        f"【历史记忆】（跨会话保留，供参考）\n{mem_text}"
    )
    if soul:
        system_prompt = soul + "\n\n" + system_prompt   # 灵魂（身份）放最前，优先级最高
    messages = [{"role": "system", "content": system_prompt}]
    print(f"{BOLD}Local Agent 已启动（{MODEL}）{RESET}")
    print(f"{GRAY}📖 已读取记忆文件: {MEMORY_FILE}{RESET}")
    print(f"{GRAY}💫 灵魂文件: {SOUL_FILE}（{'已注入' if soul else '未找到，跳过'}）{RESET}")
    print(f"{GRAY}   近期记忆 {len(mem.get('entries', []))} 条 | 长期压缩 {'有' if mem.get('compressed') else '无'}{RESET}")
    print(f"{GRAY}输入 'exit' 退出 | 输入 '记住：xxx' 直接存记忆{RESET}")

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
        while turns < MAX_TURNS:
            turns += 1
            t_round = time.time()   # 本轮耗时计时
            try:
                msg, is_tool = call_ollama_stream(messages)
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
                    try:
                        result = run_tool(func_name, args, mem)
                    except Exception as e:
                        # 工具崩溃不退出程序：错误截断后作为结果汇报给 AI，让它决策换方法或告知用户
                        err_msg = f"{type(e).__name__}: {e}"[:200]
                        result = (f"❌ 工具 {func_name} 执行出错: {err_msg}。"
                                  "请根据这个错误决定：尝试其他方法，或告知用户无法完成。")
                    elapsed = time.time() - t_tool
                    print(f"{GREEN}[调用工具] {func_name}({json.dumps(args, ensure_ascii=False)}) ({elapsed:.1f}s){RESET}")
                    messages.append({"role": "tool", "content": result})
                continue
            else:
                messages.append(msg)
                # 显示本轮生成耗时
                gen_elapsed = time.time() - t_round
                print(f"{GRAY}⏱ 本轮耗时 {gen_elapsed:.1f}s{RESET}")
                break
        else:
            print(f"\n{RED}⚠️ 工具调用轮数超限，已终止本轮。{RESET}")

        messages = trim_history(messages, mem)

if __name__ == "__main__":
    main()
