# -*- coding: utf-8 -*-
"""
tools/web_search.py —— 联网搜索工具
search_web: Tavily API 优先（环境变量 TAVILY_API_KEY），降级链：百度移动 → DuckDuckGo
"""
import html
import os
import re
import requests
from urllib.parse import quote

from . import register_tool

# Tavily key：优先读环境变量；也可在下方写死（个人版）
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _search_tavily(query):
    """Tavily 搜索（外部 API，结果质量高）"""
    if not TAVILY_API_KEY:
        return None
    try:
        resp = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "include_answer": True,
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
    except Exception:
        return None


def _search_baidu_mobile(query):
    """百度移动版（国内可用）"""
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"}
    try:
        url = "https://m.baidu.com/s?word=" + quote(query)
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "utf-8"
        titles = re.findall(r"<h3[^>]*>(.*?)</h3>", r.text, re.S)
        abstracts = re.findall(r'class="c-abstract[^"]*"[^>]*>(.*?)</div>', r.text, re.S)
        out = []
        for i, t in enumerate(titles[:5]):
            out.append(f"{i+1}. {_strip_html(t)}")
            if i < len(abstracts):
                out.append(f"   {_strip_html(abstracts[i])[:120]}")
        return "\n".join(out) if out else None
    except Exception:
        return None


def _search_ddg(query):
    """DuckDuckGo Lite 备用（海外网络）"""
    try:
        url = "https://lite.duckduckgo.com/lite/?q=" + quote(query)
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.encoding = "utf-8"
        links = re.findall(r'<a rel="nofollow"[^>]*>(.*?)</a>', r.text, re.S)
        snippets = re.findall(r'class="result-snippet">(.*?)</td>', r.text, re.S)
        out = []
        for i, t in enumerate(links[:5]):
            out.append(f"{i+1}. {_strip_html(t)}")
            if i < len(snippets):
                out.append(f"   {_strip_html(snippets[i])[:120]}")
        return "\n".join(out) if out else None
    except Exception:
        return None


@register_tool(
    "search_web",
    "联网搜索（Tavily → 百度 → DuckDuckGo）。query 为搜索关键词，返回前 5 条结果标题和摘要。",
    {"type": "object", "properties": {"query": {"type": "string"}}},
)
def search_web(args, ctx):
    query = args.get("query", "")
    r = _search_tavily(query)
    if r:
        return r
    r = _search_baidu_mobile(query)
    if r:
        return "搜索结果：\n" + r
    r = _search_ddg(query)
    if r:
        return "搜索结果：\n" + r
    return "❌ 搜索无结果或网络失败"
