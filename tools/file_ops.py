# -*- coding: utf-8 -*-
"""
tools/file_ops.py —— 文件与命令工具
list_files: 列目录
execute_shell: 执行 cmd 命令（危险命令拦截）
write_file: 写文件（系统目录保护）
"""
import os
import re
import subprocess

from . import register_tool

# 危险命令黑名单（可自行增删）
DANGEROUS = ["rm -rf", "del /f", "del /q", "format", "shutdown", "rd /s", "rmdir /s",
             "diskpart", "reg delete", "taskkill /f /im", "cipher /w", "vssadmin delete"]


@register_tool(
    "list_files",
    "列出指定目录下的所有文件和文件夹。directory 必须是完整路径（如 D:\\\\ 或 D:\\\\work）。重要：根目录要写 D:\\\\（带反斜杠），只写 D: 会被 Windows 当成当前目录列错地方。返回完整列表。",
    {"type": "object", "properties": {"directory": {"type": "string", "default": "."}}},
)
def list_files(args, ctx):
    directory = args.get("directory", ".")
    # 防御：模型常传 "D:"（漏反斜杠）→ cmd 会把它当"当前目录"而非根目录，这里自动补全
    if re.match(r"^[a-zA-Z]:$", directory):
        directory += "\\"
    try:
        # /b 只输出名称（避免卷标/统计信息干扰），/a 含隐藏文件；显式 gbk 编码 + 容错
        result = subprocess.run(["cmd", "/c", "dir", "/b", "/a", directory],
                                capture_output=True, encoding="gbk", errors="replace", timeout=15)
        if result.returncode == 0:
            items = [ln for ln in result.stdout.splitlines() if ln.strip()]
            return f"目录 [{directory}] 共 {len(items)} 项:\n" + "\n".join(items)
        else:
            return f"查询失败 [{directory}]: {result.stderr.strip() or '目录不存在'}"
    except subprocess.TimeoutExpired:
        return "查询超时"
    except Exception as e:
        return f"执行出错: {e}"


@register_tool(
    "execute_shell",
    "在 Windows 上执行一条 cmd 命令并返回输出。command 必须是完整的 cmd 命令字符串。注意：命令里的目录路径要写全（如 dir D:\\\\ 而不是 dir D:，dir D: 会列当前目录）。危险操作（删除、格式化、关机）会被拦截。",
    {"type": "object", "properties": {"command": {"type": "string"}}},
)
def execute_shell(args, ctx):
    command = args.get("command", "")
    for kw in DANGEROUS:
        if kw in command.lower():
            return f"❌ 禁止执行含有 '{kw}' 的命令。"
    try:
        result = subprocess.run(["cmd", "/c", command], capture_output=True, text=True,
                                encoding="gbk", errors="replace", timeout=30)
        if result.returncode == 0:
            return f"✅ 执行成功:\n{result.stdout}"
        else:
            return f"❌ 执行失败:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "⏰ 超时"
    except Exception as e:
        return f"💥 {e}"


@register_tool(
    "write_file",
    "把文本内容写入指定文件（UTF-8 编码，自动创建目录）。path 为完整路径，content 为要写入的内容。系统目录禁止写入。",
    {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
)
def write_file(args, ctx):
    path = args.get("path", "")
    content = args.get("content", "")
    low = path.lower()
    user_dir = os.environ.get("USERPROFILE", "").lower() or os.environ.get("HOME", "").lower()
    blocked = ["c:\\windows", "c:\\program files", "c:\\$recycle", "system32",
               "c:\\boot", "c:\\programdata"]
    if user_dir:
        blocked.append(os.path.join(user_dir, "appdata"))
        blocked.append(os.path.join(user_dir, "desktop"))
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
