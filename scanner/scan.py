"""促销雷达扫描器：用 Gemini（含 Google 搜索）找促销，整理后写入 docs/deals.json。

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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DEALS_PATH = ROOT / "docs" / "deals.json"
MYT = timezone(timedelta(hours=8))

FIELDS_HELP = """每个促销输出一个 JSON 对象，字段：
- merchant: 商家/航空公司/银行名称
- title: 一句话中文标题（不超过 30 字）
- offer: 优惠内容，中文，写清楚折扣、价格或回扣（例如"吉隆坡→东京来回 RM899 起"）
- conditions: 主要条件，中文，没有就写空字符串（例如"需用 UOB 信用卡，最低消费 RM500"）
- valid_until: 促销截止日期 YYYY-MM-DD，不知道就写 null
- travel_period: 机票/酒店的出行期间，中文，没有就写空字符串
- url: 促销页面网址，必须是真实来源网址
只收录目前仍有效、且马来西亚居民可以使用的促销。不要编造任何信息，不确定的字段留空。"""

SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "merchant": {"type": "string"},
            "title": {"type": "string"},
            "offer": {"type": "string"},
            "conditions": {"type": "string"},
            "valid_until": {"type": "string", "nullable": True},
            "travel_period": {"type": "string"},
            "url": {"type": "string"},
        },
        "required": ["merchant", "title", "offer", "url"],
    },
}


class FatalError(Exception):
    """不能重试的错误（API key 错、模型名称错等）"""


def today_myt() -> date:
    return datetime.now(MYT).date()


# ───────────────────────── Gemini 调用 ─────────────────────────

class Gemini:
    def __init__(self, model: str, pause: float):
        from google import genai
        from google.genai import errors, types

        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            sys.exit("缺少环境变量 GEMINI_API_KEY")
        # 每次请求最多等 90 秒，避免卡死
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=90_000))
        self.types = types
        self.errors = errors
        self.model = model
        self.pause = pause

    def _call(self, contents: str, config, retries: int = 3):
        for attempt in range(retries):
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                time.sleep(self.pause)
                return resp
            except self.errors.APIError as e:
                code = getattr(e, "code", None)
                # 4xx（除了 429 额度限制）重试也没用，直接报错停止
                if code and 400 <= code < 500 and code != 429:
                    raise FatalError(f"Gemini 返回 {code}：{str(e)[:300]}") from e
                wait = 20 * (attempt + 1)
                print(f"  ! Gemini 暂时出错（{code}），{wait}s 后重试")
                time.sleep(wait)
            except Exception as e:  # 超时、网络问题
                wait = 10 * (attempt + 1)
                print(f"  ! 请求失败（{e.__class__.__name__}），{wait}s 后重试")
                time.sleep(wait)
        print("  ! 重试 3 次仍失败，跳过这一条")
        return None

    def check(self) -> None:
        """开始前先用一个很小的请求测试 key 和模型名称是否正确。"""
        print(f"测试 Gemini 连接（模型：{self.model}）…")
        resp = self._call("回复 OK 两个字母", self.types.GenerateContentConfig(temperature=0))
        if resp is None:
            raise FatalError("连接 Gemini 失败，请稍后再试")
        print("  连接正常")

    def search(self, query: str) -> list[dict]:
        """用 Google 搜索 grounding 查找促销。grounding 模式不支持强制 JSON，所以从文字里解析。"""
        t = self.types
        prompt = (
            f"今天是 {today_myt().isoformat()}（马来西亚时间）。请上网搜索：{query}\n\n"
            f"{FIELDS_HELP}\n\n只输出一个 JSON 数组，不要任何其他文字。找不到就输出 []。"
        )
        cfg = t.GenerateContentConfig(
            tools=[t.Tool(google_search=t.GoogleSearch())],
            temperature=0.2,
        )
        resp = self._call(prompt, cfg)
        if resp is None:
            return []
        deals = parse_json_array(resp.text or "")
        sources = grounding_urls(resp)
        for d in deals:
            d["_sources"] = sources
        return deals

    def extract(self, page_text: str, page_url: str) -> list[dict]:
        """从固定网页的文字中提取促销（强制 JSON 输出）。"""
        t = self.types
        prompt = (
            f"今天是 {today_myt().isoformat()}。以下是网页 {page_url} 的文字内容，"
            f"请提取其中的促销。网页里没有具体网址的，url 就填 {page_url}。\n\n"
            f"{FIELDS_HELP}\n\n网页内容：\n{page_text}"
        )
        cfg = t.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SCHEMA,
            temperature=0.1,
        )
        resp = self._call(prompt, cfg)
        if resp is None:
            return []
        return parse_json_array(resp.text or "")


def parse_json_array(text: str) -> list[dict]:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]


def grounding_urls(resp) -> list[str]:
    urls = []
    try:
        meta = resp.candidates[0].grounding_metadata
        for chunk in meta.grounding_chunks or []:
            if chunk.web and chunk.web.uri:
                urls.append(chunk.web.uri)
    except (AttributeError, IndexError, TypeError):
        pass
    return urls[:5]


# ───────────────────────── 抓网页 ─────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-MY,en;q=0.9,zh;q=0.8",
}


def fetch_text(url: str, limit: int = 15000) -> str:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  ! 抓取失败 {url}: {e}")
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:limit]


def url_ok(url: str) -> bool:
    if not url.startswith("http"):
        return False
    try:
        r = httpx.get(url, headers=HEADERS, timeout=8, follow_redirects=True)
        return r.status_code < 400
    except httpx.HTTPError:
        return False


# ───────────────────────── 整理与合并 ─────────────────────────

def normalize(raw: dict, category: str, cfg: dict) -> dict | None:
    title = str(raw.get("title") or "").strip()
    merchant = str(raw.get("merchant") or "").strip()
    if not title or not merchant:
        return None

    blob = " ".join(str(raw.get(k) or "") for k in ("merchant", "title", "offer", "conditions"))
    for kw in cfg.get("exclude_keywords", []):
        if kw.lower() in blob.lower():
            return None

    valid_until = raw.get("valid_until")
    if valid_until:
        try:
            valid_until = date.fromisoformat(str(valid_until)[:10]).isoformat()
        except ValueError:
            valid_until = None
    if valid_until and date.fromisoformat(valid_until) < today_myt():
        return None  # 已过期

    url = str(raw.get("url") or "").strip()

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
        "_sources": raw.get("_sources") or [],
    }


def check_links(deals: list[dict]) -> None:
    """同时检查所有链接（最多 8 个一起），打不开就换成搜索来源。"""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda d: url_ok(d["url"]), deals))
    for d, ok in zip(deals, results):
        sources = d.pop("_sources", [])
        if ok:
            d["link_checked"] = True
        elif sources:
            d["url"] = sources[0]  # 模型给的网址打不开，就用搜索来源
            d["link_checked"] = True


def merge(existing: list[dict], fresh: list[dict], keep_days: int) -> list[dict]:
    today = today_myt().isoformat()
    by_id = {d["id"]: d for d in existing}
    for d in fresh:
        old = by_id.get(d["id"])
        d["first_seen"] = old["first_seen"] if old else today
        d["last_seen"] = today
        by_id[d["id"]] = d

    cutoff = (today_myt() - timedelta(days=keep_days)).isoformat()
    kept = []
    for d in by_id.values():
        if d.get("valid_until"):
            if d["valid_until"] >= today:
                kept.append(d)
        elif d.get("last_seen", today) >= cutoff:
            kept.append(d)
    kept.sort(key=lambda d: (d.get("valid_until") or "9999", d["category"]))
    return kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    gem = Gemini(cfg.get("model", "gemini-flash-latest"), cfg.get("pause_seconds", 6))
    try:
        gem.check()
    except FatalError as e:
        sys.exit(f"停止：{e}")

    fresh: list[dict] = []
    queries = cfg.get("search_queries", [])
    for i, item in enumerate(queries, 1):
        print(f"[搜索 {i}/{len(queries)}] {item['category']}：{item['query'][:50]}")
        try:
            found = gem.search(item["query"])
        except FatalError as e:
            sys.exit(f"停止：{e}")
        print(f"  得到 {len(found)} 条")
        for raw in found:
            d = normalize(raw, item["category"], cfg)
            if d:
                fresh.append(d)

    for page in cfg.get("fixed_pages") or []:
        print(f"[网页] {page['name']}")
        text = fetch_text(page["url"])
        if text:
            try:
                found = gem.extract(text, page["url"])
            except FatalError as e:
                sys.exit(f"停止：{e}")
            for raw in found:
                d = normalize(raw, page["category"], cfg)
                if d:
                    fresh.append(d)

    print(f"检查 {len(fresh)} 条促销的链接…")
    check_links(fresh)
    print(f"本次找到 {len(fresh)} 条")
    if args.dry_run:
        print(json.dumps(fresh, ensure_ascii=False, indent=2))
        return

    existing = []
    if DEALS_PATH.exists():
        existing = json.loads(DEALS_PATH.read_text(encoding="utf-8")).get("deals", [])
    deals = merge(existing, fresh, cfg.get("keep_undated_days", 21))
    out = {"updated_at": datetime.now(MYT).isoformat(timespec="minutes"), "deals": deals}
    DEALS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {len(deals)} 条到 {DEALS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
