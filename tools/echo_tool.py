# -*- coding: utf-8 -*-
"""
tools/echo_tool.py —— 测试工具（演示最小工具写法）
echo_message: 打印一条消息
"""
from . import register_tool


@register_tool(
    "echo_message",
    "在命令行中打印一条消息，用于测试和确认。",
    {"type": "object", "properties": {"message": {"type": "string"}}},
)
def echo_message(args, ctx):
    return f"已执行: echo {args.get('message', '')}"
