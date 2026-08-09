# -*- coding: utf-8 -*-
"""
tools/memory_tools.py —— 记忆与偏好工具
remember: 存一条长期记忆（跨会话）
update_pref: 更新灵魂文件里的用户偏好（称呼/名字/相处方式）
依赖主程序 ctx：{"mem", "add_memory", "soul_file", "set_pref"}
"""
from . import register_tool


MODES = ["闲聊", "工作", "写代码", "查资料"]


@register_tool(
    "set_mode",
    "切换工作模式。可选模式：闲聊 / 工作 / 写代码 / 查资料。当用户明确说'进入xx模式'、'切换到xx模式'时调用。模式会跨会话保持。",
    {"type": "object", "properties": {"mode": {"type": "string"}}},
)
def set_mode(args, ctx):
    mode = (args.get("mode", "") or "").strip()
    if mode not in MODES:
        return f"未知模式 '{mode}'，可选：{' / '.join(MODES)}"
    ctx["mem"]["mode"] = mode
    if ctx.get("save_memory"):
        ctx["save_memory"](ctx["mem"])
    return f"已切换到【{mode}】模式（跨会话保持）"


@register_tool(
    "remember",
    "把一条重要信息存入长期记忆（跨会话保留）。用户明确要求记住、或者对话中出现需要长期保存的事实/偏好时调用。content 为要记住的内容。",
    {"type": "object", "properties": {"content": {"type": "string"}}},
)
def remember(args, ctx):
    content = args.get("content", "")
    return ctx["add_memory"](ctx["mem"], content)


@register_tool(
    "update_pref",
    "更新灵魂文件里的用户偏好（称呼/名字/相处方式）。当用户明确要求改变对你的称呼、给你起新名字或调整相处方式时调用。key 取 '称呼'/'名字'/'相处方式'，value 为新内容。",
    {"type": "object", "properties": {
        "key": {"type": "string", "enum": ["称呼", "名字", "相处方式"]},
        "value": {"type": "string"}}},
)
def update_pref(args, ctx):
    key = args.get("key", "")
    value = args.get("value", "")
    if key not in ("称呼", "名字", "相处方式"):
        return f"❌ 不支持的偏好字段: {key}（只能改 称呼/名字/相处方式）"
    if ctx["set_pref"](ctx["soul_file"], key, value):
        return f"✅ 已更新灵魂偏好 {key} = {value}，下次启动生效"
    return "❌ 写入灵魂文件失败"
