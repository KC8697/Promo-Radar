"""促销雷达扫描器（免费版）

流程：Tavily 全网搜索 + Google News RSS + 固定促销网页 → Gemini 免费层整理 → docs/deals.json
- Tavily：每月 1,000 次免费搜索，程序每天有上限，不会超额
- Gemini：只负责整理（不用它的搜索功能，免费层不提供）

用法：
    GEMINI_API_KEY=xxx python scanner/scan.py
    python scanner/scan.py --dry-run      # 只打印结果，不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DEALS_PATH = ROOT / "docs" / "deals.json"
MYT = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-MY,en;q=0.9,zh;q=0.8",
}

DEAL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "source_index": {"type": "integer"},
            "merchant": {"type": "string"},
            "title": {"type": "string"},
            "offer": {"type": "string"},
            "conditions": {"type": "string"},
            "valid_until": {"type": "string", "nullable": True},
            "travel_period": {"type": "string"},
        },
        "required": ["source_index", "merchant", "title", "offer"],
    },
}

FIELDS_HELP = """每个促销输出一个对象：
- source_index: 这个促销来自哪一条资料（填资料前面的编号）
- merchant: 商家/航空公司/银行名称
- title: 一句话中文标题（不超过 25 字）
- offer: 优惠内容，中文，写清楚折扣、价格或回扣（资料没写就简单描述）
- conditions: 主要条件，中文，没有就空字符串
- valid_until: 截止日期 YYYY-MM-DD，资料没写就 null
- travel_period: 机票/酒店的出行期间，没有就空字符串
规则：只收录真正的促销或优惠（不要一般新闻、财报、评论）；马来西亚居民能用的才收；
同一个促销只写一次；不要编造资料里没有的信息。"""


class FatalError(Exception):
    """不能重试的错误（API key 错等）"""


def today_myt() -> date:
    return datetime.now(MYT).date()


# ───────────────────────── Gemini（不含搜索功能）─────────────────────────

class Gemini:
    def __init__(self, models: list[str], pause: float):
        from google import genai
        from google.genai import errors, types

        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            sys.exit("缺少环境变量 GEMINI_API_KEY")
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=90_000))
        self.types = types
        self.errors = errors
        self.models = list(models)
        self.pause = pause

    def _try_model(self, model, contents, config, attempts=2):
        for attempt in range(attempts):
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
                time.sleep(self.pause)
                return resp
            except self.errors.APIError as e:
                code = getattr(e, "code", None)
                if code == 404:
                    print(f"  ! 模型 {model} 不存在，换下一个")
                    return "missing"
                if code and 400 <= code < 500 and code != 429:
                    raise FatalError(f"Gemini 返回 {code}：{str(e)[:300]}") from e
                wait = 15 * (attempt + 1)
                print(f"  ! {model} 暂时出错（{code}），{wait}s 后重试")
                time.sleep(wait)
            except Exception as e:
                print(f"  ! {model} 请求失败（{e.__class__.__name__}）")
                time.sleep(10)
        return None

    def _call(self, contents, config):
        for model in list(self.models):
            result = self._try_model(model, contents, config)
            if result == "missing":
                self.models.remove(model)
                continue
            if result is not None:
                if self.models[0] != model:
                    self.models.remove(model)
                    self.models.insert(0, model)
                return result
            print(f"  ! {model} 一直失败，换下一个模型")
        print("  ! 所有模型都失败，跳过这一批")
        return None

    def check(self):
        print(f"测试 Gemini 连接（模型顺序：{', '.join(self.models)}）…")
        if self._call("回复 OK 两个字母", self.types.GenerateContentConfig(temperature=0)) is None:
            raise FatalError("所有模型都连不上，可能是 Google 服务暂时有问题，请稍后再跑")
        print(f"  连接正常，使用 {self.models[0]}")

    def extract(self, material: str, note: str = "") -> list[dict]:
        extra = f"\n这个类别的额外规则：{note}" if note else ""
        prompt = (
            f"今天是 {today_myt().isoformat()}（马来西亚时间）。以下是编了号的资料，请从中找出促销。\n\n"
            f"{FIELDS_HELP}{extra}\n\n资料：\n{material}"
        )
        cfg = self.types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=DEAL_SCHEMA, temperature=0.1
        )
        resp = self._call(prompt, cfg)
        if resp is None:
            return []
        try:
            data = json.loads(resp.text or "[]")
        except json.JSONDecodeError:
            return []
        return [d for d in data if isinstance(d, dict)]


# ───────────────────────── 资料来源 ─────────────────────────

def google_news(query: str, days: int) -> list[dict]:
    """Google News RSS 搜索马来西亚新闻，返回最近 days 天的标题和链接。"""
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(f"{query} when:{days}d")
        + "&hl=en-MY&gl=MY&ceid=MY:en"
    )
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (httpx.HTTPError, ET.ParseError) as e:
        print(f"  ! 新闻搜索失败（{query}）：{e.__class__.__name__}")
        return []

    cutoff = datetime.now(MYT) - timedelta(days=days)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            pub = None
        if pub and pub < cutoff:
            continue
        desc = BeautifulSoup(it.findtext("description") or "", "html.parser").get_text(" ").strip()
        if title and link:
            items.append({
                "title": title,
                "link": link,
                "source": source,
                "date": pub.astimezone(MYT).date().isoformat() if pub else "",
                "snippet": desc[:200] if desc and desc[:20] not in title else "",
            })
    return items


class Tavily:
    """Tavily 全网搜索（每次基本搜索用 1 个额度）。"""

    def __init__(self, daily_limit: int):
        self.key = os.environ.get("TAVILY_API_KEY", "").strip()
        self.left = daily_limit
        self.disabled = not self.key
        if self.disabled:
            print("（没有设置 TAVILY_API_KEY，跳过全网搜索，只用新闻）")

    def search(self, query: str, time_range: str = "week", max_results: int = 5) -> list[dict]:
        if self.disabled:
            return []
        if self.left <= 0:
            print(f"  ! 这次的 Tavily 搜索上限已用完，跳过「{query}」")
            return []
        self.left -= 1
        try:
            r = httpx.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self.key}"},
                json={
                    "query": query,
                    "search_depth": "basic",   # 1 个额度
                    "max_results": max_results,
                    "time_range": time_range,
                    "country": "malaysia",     # 优先马来西亚的结果
                },
                timeout=40,
            )
        except httpx.HTTPError as e:
            print(f"  ! Tavily 请求失败（{e.__class__.__name__}）")
            return []
        if r.status_code in (401, 403):
            print("  ! Tavily key 无效，之后的全网搜索全部跳过（请检查 GitHub Secret）")
            self.disabled = True
            return []
        if r.status_code >= 400:
            print(f"  ! Tavily 返回 {r.status_code}：{r.text[:150]}")
            if r.status_code in (429, 432, 433):  # 额度用完或太频繁
                self.disabled = True
            return []
        out = []
        for it in r.json().get("results", []):
            url = (it.get("url") or "").strip()
            title = (it.get("title") or "").strip()
            if url and title:
                out.append({
                    "title": title,
                    "url": url,
                    "content": re.sub(r"\s+", " ", it.get("content") or "")[:500],
                    "date": (it.get("published_date") or "")[:10],
                })
        return out


def fetch_page_text(url: str, limit: int = 12000) -> str:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  ! 抓取失败 {url}：{e.__class__.__name__}")
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:limit]


def url_ok(url: str) -> bool:
    if not url.startswith("http"):
        return False
    if "news.google.com" in url:
        return True  # Google News 转址链接，浏览器打开会跳到原文
    try:
        r = httpx.get(url, headers=HEADERS, timeout=8, follow_redirects=True)
        return r.status_code < 400
    except httpx.HTTPError:
        return False


# ───────────────────────── 整理与合并 ─────────────────────────

def normalize(raw: dict, category: str, url: str, cfg: dict) -> dict | None:
    title = str(raw.get("title") or "").strip()
    merchant = str(raw.get("merchant") or "").strip()
    if not title or not merchant:
        return None
    blob = " ".join(str(raw.get(k) or "") for k in ("merchant", "title", "offer", "conditions")).lower()
    if any(kw.lower() in blob for kw in cfg.get("exclude_keywords", [])):
        return None

    valid_until = raw.get("valid_until")
    if valid_until:
        try:
            valid_until = date.fromisoformat(str(valid_until)[:10]).isoformat()
        except ValueError:
            valid_until = None
    if valid_until and valid_until < today_myt().isoformat():
        return None

    key = re.sub(r"\W+", "", f"{merchant}{title}".lower())
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:12],
        "category": category,
        "merchant": merchant,
        "title": title,
        "offer": str(raw.get("offer") or "").strip(),
        "conditions": str(raw.get("conditions") or "").strip(),
        "valid_until": valid_until,
        "travel_period": str(raw.get("travel_period") or "").strip(),
        "url": url,
        "link_checked": False,
    }


def check_links(deals: list[dict]) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        for d, ok in zip(deals, pool.map(lambda d: url_ok(d["url"]), deals)):
            d["link_checked"] = ok


def merge(existing: list[dict], fresh: list[dict], keep_days: int) -> list[dict]:
    today = today_myt().isoformat()
    by_id = {d["id"]: d for d in existing}
    for d in fresh:
        old = by_id.get(d["id"])
        d["first_seen"] = old["first_seen"] if old else today
        d["last_seen"] = today
        by_id[d["id"]] = d
    cutoff = (today_myt() - timedelta(days=keep_days)).isoformat()
    kept = [
        d for d in by_id.values()
        if (d["valid_until"] >= today if d.get("valid_until") else d.get("last_seen", today) >= cutoff)
    ]
    kept.sort(key=lambda d: (d.get("valid_until") or "9999", d["category"]))
    return kept


def scan_category(gem: Gemini, tav: Tavily, cat: dict, cfg: dict) -> list[dict]:
    """收集一个类别的全网搜索结果、新闻和网页，分批交给 Gemini 整理。"""
    sources: list[dict] = []  # 每个元素：{"text": 给 Gemini 看的内容, "url": 链接}
    seen = set()

    def add(key: str, text: str, url: str) -> None:
        k = key.lower().strip()
        if k and k not in seen:
            seen.add(k)
            sources.append({"text": text, "url": url})

    # 1) Tavily 全网搜索
    for q in cat.get("web_queries", []) or []:
        if isinstance(q, dict):
            query, tr = q["q"], q.get("time_range", "week")
        else:
            query, tr = q, "week"
        before = tav.left
        items = tav.search(query, time_range=tr)
        if tav.left < before:  # 真的有搜索才显示
            print(f"  全网「{query}」：{len(items)} 条")
        for it in items:
            line = f"{it['title']}（{it['url'].split('/')[2]}"
            line += f"，{it['date']}）" if it["date"] else "）"
            if it["content"]:
                line += f" — {it['content']}"
            add(it["url"], line, it["url"])

    # 2) Google News
    days = cfg.get("news_days", 10)
    for q in cat.get("news_queries", []) or []:
        items = google_news(q, days)
        print(f"  新闻「{q}」：{len(items)} 条")
        for it in items:
            line = f"[{it['date']}] {it['title']}（{it['source']}）"
            if it["snippet"]:
                line += f" — {it['snippet']}"
            add(it["title"], line, it["link"])

    # 3) 固定网页
    for page in cat.get("pages", []) or []:
        text = fetch_page_text(page["url"])
        print(f"  网页「{page['name']}」：{'读取成功' if text else '读取失败'}")
        if text:
            add(page["url"], f"网页《{page['name']}》全文：{text}", page["url"])

    if not sources:
        return []

    # 分批交给 Gemini，每批最多 batch_size 条，全部资料都会被看到
    size = cfg.get("batch_size", 40)
    deals = []
    for start in range(0, len(sources), size):
        batch = sources[start : start + size]
        material = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(batch))
        for raw in gem.extract(material, cat.get("note", "")):
            idx = raw.get("source_index")
            if not isinstance(idx, int) or not 0 <= idx < len(batch):
                continue  # 编号不对，可能是模型乱写，丢掉
            d = normalize(raw, cat["category"], batch[idx]["url"], cfg)
            if d:
                deals.append(d)
    # 同一个促销可能在不同批次出现，按 id 去重
    return list({d["id"]: d for d in deals}.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    gem = Gemini(cfg.get("models") or ["gemini-flash-lite-latest"], cfg.get("pause_seconds", 5))
    try:
        gem.check()
    except FatalError as e:
        sys.exit(f"停止：{e}")

    limit = cfg.get("tavily_per_run_limit", cfg.get("tavily_daily_limit", 30))
    tav = Tavily(limit)
    fresh: list[dict] = []
    cats = cfg.get("categories", [])
    for i, cat in enumerate(cats, 1):
        print(f"[{i}/{len(cats)}] {cat['category']}")
        try:
            found = scan_category(gem, tav, cat, cfg)
        except FatalError as e:
            sys.exit(f"停止：{e}")
        print(f"  → 整理出 {len(found)} 个促销")
        fresh.extend(found)

    if not tav.disabled or tav.key:
        print(f"这次用了 {limit - tav.left} 次 Tavily 搜索（上限 {limit}）")
    print(f"检查 {len(fresh)} 个链接…")
    check_links(fresh)
    if args.dry_run:
        print(json.dumps(fresh, ensure_ascii=False, indent=2))
        return

    existing = []
    if DEALS_PATH.exists():
        existing = json.loads(DEALS_PATH.read_text(encoding="utf-8")).get("deals", [])
    deals = merge(existing, fresh, cfg.get("keep_undated_days", 21))
    out = {"updated_at": datetime.now(MYT).isoformat(timespec="minutes"), "deals": deals}
    DEALS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成：网页现在有 {len(deals)} 个有效促销")


if __name__ == "__main__":
    main()
