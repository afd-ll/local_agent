# -*- coding: utf-8 -*-
"""
tools/skills_tools.py —— 技能系统（Skills）

机制（与 Hermes 同构）：
- 技能文件存在 <数据目录>/skills/ 下，每个技能一个 .md（frontmatter: name/description + 正文步骤）
- 技能索引（name+description）常驻 system prompt（很短的几行）
- 技能全文按需加载：模型判断任务需要时调 load_skill 读入
- 学习：用户教了新方法/完成可复用流程 → learn_skill 总结保存

skill 文件格式：
---
name: 查磁盘结构
description: 用户问磁盘/文件夹里有什么时
---
1. 用 list_files 列根目录
2. 看有没有特殊目录
...
"""
import os

from . import register_tool


def parse_skill(content):
    """解析技能文件：frontmatter 取 name/description，其余为正文"""
    name = ""
    desc = ""
    body = content.strip()
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            meta, body = parts[1], parts[2].strip()
            for line in meta.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
    return name, desc, body


def _skills_dir(ctx):
    return ctx.get("skills_dir", "skills")


@register_tool(
    "list_skills",
    "列出所有已保存的技能（技能名 + 一句话说明）。用户问你会什么技能、或你不知道该用什么方法时调用。",
    {"type": "object", "properties": {}},
)
def list_skills(args, ctx):
    sdir = _skills_dir(ctx)
    if not os.path.isdir(sdir):
        return "还没有任何技能（可以让我用 learn_skill 学习）"
    lines = []
    for f in sorted(os.listdir(sdir)):
        if f.endswith(".md"):
            try:
                with open(os.path.join(sdir, f), encoding="utf-8") as fh:
                    name, desc, _ = parse_skill(fh.read())
                lines.append(f"- {name or f[:-3]}：{desc or '无描述'}")
            except Exception:
                continue
    return "技能列表：\n" + "\n".join(lines) if lines else "还没有任何技能"


@register_tool(
    "load_skill",
    "加载一个技能的全部内容（步骤说明）。当任务匹配某个技能时调用，把技能内容读入上下文按步骤执行。name 为技能名。",
    {"type": "object", "properties": {"name": {"type": "string"}}},
)
def load_skill(args, ctx):
    name = args.get("name", "")
    sdir = _skills_dir(ctx)
    path = os.path.join(sdir, name + ".md")
    if not os.path.isfile(path):
        return f"未找到技能 '{name}'（用 list_skills 查看可用技能）"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        _, _, body = parse_skill(content)
        return f"技能 [{name}] 内容：\n{body}"
    except Exception as e:
        return f"读取技能失败: {e}"


@register_tool(
    "learn_skill",
    "学习并保存一个新技能（或更新同名技能）。当用户教了你一个新方法、你完成了一个可复用的多步流程、或用户明确要求'记住这个方法'时调用。name 为技能名（简短），description 为一句话说明何时用，content 为具体步骤（分步、可执行）。",
    {"type": "object", "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "content": {"type": "string"}}},
)
def learn_skill(args, ctx):
    name = (args.get("name", "") or "").strip()
    desc = (args.get("description", "") or "").strip()
    content = (args.get("content", "") or "").strip()
    if not name or not content:
        return "❌ 技能名和内容不能为空"
    sdir = _skills_dir(ctx)
    try:
        os.makedirs(sdir, exist_ok=True)
        # 文件名安全化（去非法字符）
        safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
        if not safe:
            return "❌ 技能名含非法字符"
        doc = f"---\nname: {safe}\ndescription: {desc}\n---\n\n{content}\n"
        with open(os.path.join(sdir, safe + ".md"), "w", encoding="utf-8") as f:
            f.write(doc)
        return f"✅ 技能已保存：{safe}（{desc}）——下次遇到同类任务我会调用它"
    except Exception as e:
        return f"❌ 保存技能失败: {e}"
