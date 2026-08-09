# -*- coding: utf-8 -*-
"""
tools/__init__.py —— 工具注册表（插件化核心）

机制：
1. 每个工具是一个独立 .py 文件，放在本目录下（_ 开头除外）
2. 工具文件里用 @register_tool 装饰器声明（name / description / parameters）
3. 主程序调用 load_all_tools() 自动扫描加载，无需改主文件

添加新工具（三步）：
1. 在 tools/ 下新建 my_tool.py
2. 用 @register_tool 注册（handler 签名固定为 (args, ctx)）
3. 重启 agent，工具自动出现在列表里

handler 签名：def my_tool(args: dict, ctx: dict) -> str
  - args:  模型传的参数（JSON 对象）
  - ctx:   主程序注入的上下文（{"mem": 记忆对象, "add_memory": 函数, "soul_file": 路径, "set_pref": 函数}）
  - 返回:  字符串（会作为工具结果回传给模型）
"""
import importlib
import os

TOOL_REGISTRY = {}


def register_tool(name, description, parameters):
    """装饰器：把一个函数注册为可被 agent 调用的工具"""
    def decorator(func):
        TOOL_REGISTRY[name] = {
            "description": description,
            "parameters": parameters,
            "handler": func,
        }
        return func
    return decorator


def load_all_tools():
    """扫描 tools/ 目录下所有工具模块（_ 开头除外），自动加载注册"""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(os.listdir(pkg_dir)):
        if f.endswith(".py") and not f.startswith("_"):
            module_name = f[:-3]
            if module_name not in __import__("sys").modules or True:
                importlib.import_module(f"{__name__}.{module_name}")
    return TOOL_REGISTRY


def build_tools_schema():
    """把注册表转成 Ollama 的 tools 参数格式"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["parameters"],
            },
        }
        for name, info in TOOL_REGISTRY.items()
    ]
