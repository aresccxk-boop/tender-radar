#!/usr/bin/env python3
"""抓取公开政府采购公告，写成 data/tenders.json。

只访问公开页面，不登录、不碰付费平台。请求间隔由 config/rules.json 的「请求间隔秒」控制（默认不少于 1.5 秒）。
某个数据源失败时记入状态并继续，不让整次任务失败。

用法：
  python scripts/fetch.py
  python scripts/fetch.py --only gd,ggzy
  python scripts/fetch.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import urllib3.util.connection as urllib3_conn

# 政府站点对境外 IPv6 经常握手超时，固定走 IPv4。
urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "config" / "rules.json"
DATA_PATH = ROOT / "data" / "tenders.json"
CST = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 粤公平门户前端用来给公开接口签名的参数，写在官网 JS 里，不是账号密钥。
_GF = "zxcvbnmlkjhgfdsaqwertyuiop0987654321QWERTYUIOPLKJHGFDSAZXCVBNM"
_KRE = _GF + "-@#$%^&*+!"


def _an(nums: list[int]) -> str:
    return "".join(_KRE[i] for i in nums)


_SIGN_KEY = _an([8, 28, 20, 42, 21, 53, 65, 6])
_H_APP = _an([56, 62, 52, 11, 23, 62, 39, 18, 16, 62, 54, 25, 25])
_H_APP_V = _an([11, 11, 0, 21, 62, 25, 24, 19, 20, 15, 7])
_H_NONCE = _an([56, 62, 52, 11, 23, 62, 39, 18, 16, 62, 60, 24, 5, 2, 18])
_H_TS = _an([56, 62, 52, 11, 23, 62, 39, 18, 16, 62, 40, 23, 6, 18, 14, 20, 15, 6, 25])
_H_SIG = _an([56, 62, 52, 11, 23, 62, 39, 18, 16, 62, 53, 23, 11, 5, 15, 20, 22, 19, 18])

HENAN_CHANNELS = [
    ("9102", "采购意向"),
    ("0101", "招标公告"),
    ("0103", "更正"),
    ("0102", "结果中标"),
]
GGZY_TYPE = {
    "0201": "招标公告",
    "0202": "结果中标",
    "0203": "结果中标",
    "0204": "更正",
}


def now_cst() -> datetime:
    return datetime.now(CST)


def load_rules() -> dict:
    with RULES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def norm_title(title: str) -> str:
    return re.sub(r"[\s\u3000，。、“”\"'（）()【】\[\]·,.:：\-—]+", "", title or "").lower()


def sanitize_budget(amount) -> float | None:
    if amount is None or amount == "":
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    # 年份、被拆开的日期，不是金额
    if 1990 <= amount <= 2035:
        return None
    if amount == int(amount):
        digits = str(int(amount))
        if len(digits) >= 8 and digits.startswith("20"):
            return None
    return round(amount, 2)


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    patterns = [
        r"(?:中标（成交）金额|中标金额|成交金额|成交总价|中标价|预算金额|项目预算|采购预算|预算总价|最高限价)\s*[:：为]?\s*([\d,]+(?:\.\d+)?)\s*(万元|万|元)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            num = float(m.group(1).replace(",", ""))
            unit = m.group(2) or ""
            if unit in ("万", "万元"):
                num *= 10000
            cleaned = sanitize_budget(num)
            if cleaned:
                return cleaned
    return None


def money_from_number(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        num = float(str(raw).replace(",", "").strip())
    except ValueError:
        return None
    return sanitize_budget(num)


def parse_deadline(text: str | None) -> str:
    if not text:
        return ""
    m = re.search(
        r"(?:投标截止|开标时间|响应截止|递交截止|提交截止|截止时间|开标).{0,30}?"
        r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})",
        text,
    )
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


_ORG_BAD = ("通知", "质疑", "签订", "要求", "名单", "支付", "融资", "服务费", "如下", "详见", "查询", "交货", "核验", "提出", "收取", "评审", "网址", "公告")
_ORG_OK = ("学校", "学院", "大学", "中学", "小学", "局", "中心", "医院", "公司", "政府", "委员会", "幼儿园", "教育")


def plausible_org(name: str) -> str:
    name = re.split(r"[，。；;\s]", (name or "").strip(" ：:，,。"))[0]
    if not name or len(name) < 4 or len(name) > 30:
        return ""
    if name in {"信息", "名称", "代表", "要求", "地址", "电话", "联系人"}:
        return ""
    if any(w in name for w in _ORG_BAD):
        return ""
    if not any(w in name for w in _ORG_OK):
        return ""
    return name


def parse_party(text: str | None, labels: str) -> str:
    if not text:
        return ""
    m = re.search(rf"(?:{labels})[：:\s]{{0,6}}([\u4e00-\u9fa5A-Za-z0-9（）()]{{2,40}})", text)
    if not m:
        return ""
    return plausible_org(m.group(1))


def ymd_from_compact(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", raw or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def henan_city(text: str, rules: dict) -> str:
    raw = text or ""
    for name in sorted(rules.get("河南地市") or [], key=len, reverse=True):
        if name and name in raw:
            return name
    for county, city in (rules.get("区县归属") or {}).items():
        if county and county in raw:
            return city
    return ""


def compact(text: str) -> str:
    return re.sub(r"[\s\u3000、,，.。·/\\（）()【】\[\]\"'“”]+", "", text or "").lower()


def keyword_hits(title: str, rules: dict) -> list[str]:
    folded = compact(title)
    hits = []
    extra = rules.get("关键词还需包含") or {}
    excludes = rules.get("排除词") or []
    if any(w and compact(w) in folded for w in excludes):
        return []
    for kw in rules.get("搜索关键词") or []:
        if not kw or compact(kw) not in folded:
            continue
        need = extra.get(kw) or []
        if need and not any(compact(w) in folded for w in need):
            continue
        hits.append(kw)
    return hits


def item_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def make_item(**kw) -> dict:
    url = kw["url"]
    return {
        "id": item_id(url),
        "title": kw.get("title") or "",
        "buyer": kw.get("buyer") or "",
        "budget": kw.get("budget"),
        "winner": kw.get("winner") or "",
        "publish_date": kw.get("publish_date") or "",
        "deadline": kw.get("deadline") or "",
        "source": kw.get("source") or "",
        "source_id": kw.get("source_id") or "",
        "url": url,
        "region": kw.get("region") or "national",
        "city": kw.get("city") or "",
        "province": kw.get("province") or "",
        "type_hint": kw.get("type_hint") or "招标公告",
        "keywords": kw.get("keywords") or [],
    }


class Client:
    def __init__(self, delay: float):
        self.delay = max(1.5, float(delay))
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._last = 0.0

    def _wait(self) -> None:
        gap = self.delay - (time.monotonic() - self._last)
        if self._last and gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def send(self, method: str, url: str, *, tries: int = 2, timeout=(8, 22), **kwargs):
        last = None
        for attempt in range(tries):
            self._wait()
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
                return resp
            except requests.RequestException as exc:
                last = exc
                print(f"  retry {attempt + 1}/{tries} {url[:80]} {exc.__class__.__name__}", flush=True)
        raise last


def gd_sign(params: dict) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = "".join(secrets.choice(_GF) for _ in range(16))
    raw = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sig = hashlib.sha256(f"{nonce}{_SIGN_KEY}{requests.utils.unquote(raw)}{ts}".encode()).hexdigest()
    return {
        "User-Agent": UA,
        "Referer": "https://ygp.gdzwfw.gov.cn/",
        _H_APP: _H_APP_V,
        _H_NONCE: nonce,
        _H_TS: ts,
        _H_SIG: sig,
    }


def gd_link(entry: dict) -> str:
    q = "&".join(
        f"{k}={quote(str(v or ''))}"
        for k, v in [
            ("noticeId", entry.get("noticeId")),
            ("projectCode", entry.get("projectCode")),
            ("bizCode", entry.get("tradingProcess")),
            ("siteCode", entry.get("regionCode")),
            ("publishDate", entry.get("publishDate")),
            ("source", entry.get("pubServicePlat")),
            ("titleDetails", entry.get("noticeSecondTypeDesc")),
            ("classify", entry.get("projectType")),
        ]
    )
    edition = entry.get("edition") or "v3"
    second = entry.get("noticeSecondType") or "D"
    return f"https://ygp.gdzwfw.gov.cn/#/44/new/jygg/{edition}/{second}?{q}"


def gd_region(name: str) -> tuple[str, str, str]:
    city = name or ""
    if city.startswith("广州"):
        return "guangzhou", "广州", "广东"
    return "guangdong", city or "省级", "广东"


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def gd_detail(client: Client, entry: dict) -> dict:
    """补预算、截止日、采购人。失败时返回空字典。"""
    site = entry.get("regionCode") or entry.get("siteCode") or "44"
    biz = str(entry.get("tradingProcess") or "")
    project = entry.get("projectCode") or ""
    notice = entry.get("noticeId") or ""
    if not (biz and project and notice):
        return {}
    base = "https://ygp.gdzwfw.gov.cn/ggzy-portal/center/apis/"
    node_params = {
        "siteCode": site,
        "tradingType": "D",
        "bizCode": biz,
        "projectCode": project,
    }
    try:
        r = client.send("GET", base + "trading-notice/new/singleNode", params=node_params, headers=gd_sign(node_params), tries=2)
    except requests.RequestException:
        return {}
    node = (r.text or "").strip().strip('"')
    if not node.isdigit():
        try:
            node = str((r.json().get("data") or "")).strip()
        except Exception:
            return {}
    if not node.isdigit():
        return {}
    detail_params = {
        "nodeId": node,
        "version": entry.get("edition") or "v3",
        "tradingType": "D",
        "noticeId": notice,
        "bizCode": biz,
        "projectCode": project,
        "siteCode": site,
    }
    try:
        r = client.send("GET", base + "trading-notice/new/detail", params=detail_params, headers=gd_sign(detail_params), tries=2)
        data = r.json().get("data") or {}
    except Exception:
        return {}
    blobs = []
    budget = None
    buyer = ""
    for col in data.get("tradingNoticeColumnModelList") or []:
        rich = col.get("richtext") or ""
        if rich:
            blobs.append(strip_html(rich))
        for row in col.get("multiKeyValueTableList") or []:
            cells = row if isinstance(row, list) else [row]
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                key = str(cell.get("key") or cell.get("aliasName") or "")
                val = str(cell.get("value") or "")
                blobs.append(f"{key}：{val}")
                if budget is None and any(w in key for w in ("预算", "中标金额", "成交金额", "最高限价")):
                    budget = money_from_number(val) or parse_money(f"{key}：{val}元")
                if not buyer and any(w in key for w in ("采购人", "采购单位", "招标人")):
                    buyer = val.strip()[:40]
    text = "\n".join(blobs)[:20000]
    return {
        "budget": budget if budget is not None else parse_money(text),
        "deadline": parse_deadline(text),
        "buyer": buyer or parse_party(text, "采购人|采购单位|招标人"),
        "winner": parse_party(text, "中标供应商|成交供应商|中标人|成交人|供应商名称"),
    }


def fetch_gd(client: Client, rules: dict, pages: int, detail_budget: list[int]) -> tuple[list[dict], dict]:
    name = "广东省公共资源交易平台"
    url = "https://ygp.gdzwfw.gov.cn/ggzy-portal/search/v2/items"
    found: dict[str, dict] = {}
    listed = 0
    try:
        for kw in rules["搜索关键词"]:
            for page in range(1, pages + 1):
                body = {
                    "type": "trading-type",
                    "openConvert": False,
                    "keyword": kw,
                    "siteCode": "44",
                    "secondType": "D",
                    "tradingProcess": "",
                    "thirdType": "[]",
                    "projectType": "",
                    "publishStartTime": "",
                    "publishEndTime": "",
                    "pageNo": page,
                    "pageSize": 20,
                }
                r = client.send(
                    "POST",
                    url,
                    json=body,
                    headers={"Referer": "https://ygp.gdzwfw.gov.cn/", "Origin": "https://ygp.gdzwfw.gov.cn"},
                    tries=2,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = (r.json() or {}).get("data") or {}
                rows = data.get("pageData") or []
                listed += len(rows)
                for entry in rows:
                    title = entry.get("noticeTitle") or ""
                    hits = keyword_hits(title, rules)
                    if not hits:
                        continue
                    region, city, province = gd_region(entry.get("regionName") or "")
                    hint = entry.get("datasetName") or entry.get("noticeSecondTypeDesc") or "招标公告"
                    rec = make_item(
                        title=title,
                        buyer=entry.get("projectOwner") or "",
                        publish_date=ymd_from_compact(entry.get("publishDate") or ""),
                        source=name,
                        source_id="gd",
                        url=gd_link(entry),
                        region=region,
                        city=city,
                        province=province,
                        type_hint=hint,
                        keywords=hits,
                    )
                    rec["_entry"] = entry
                    prev = found.get(rec["id"])
                    if prev:
                        prev["keywords"] = sorted(set(prev["keywords"] + hits))
                    else:
                        found[rec["id"]] = rec
                total_pages = int(data.get("pageTotal") or 0)
                if page >= total_pages:
                    break
    except Exception as exc:
        return [], {"id": "gd", "name": name, "status": "fail", "listed": listed, "kept": 0, "note": str(exc)[:180]}

    detailed = 0
    ordered = focus_first(list(found.values()))
    for rec in ordered:
        if detail_budget[0] <= 0:
            break
        extra = gd_detail(client, rec.pop("_entry"))
        detail_budget[0] -= 1
        detailed += 1
        for key in ("budget", "deadline", "buyer", "winner"):
            if extra.get(key) and not rec.get(key):
                rec[key] = extra[key]
    for rec in found.values():
        rec.pop("_entry", None)
    note = f"列表 {listed} 条，标题命中 {len(found)} 条，补详情 {detailed} 条"
    return list(found.values()), {"id": "gd", "name": name, "status": "ok", "listed": listed, "kept": len(found), "note": note}


def fetch_henan(client: Client, rules: dict, pages: int, detail_budget: list[int]) -> tuple[list[dict], dict]:
    name = "河南省政府采购网"
    cities = rules.get("河南地市") or []
    found: dict[str, dict] = {}
    listed = 0
    skipped = []
    try:
        for code, hint in HENAN_CHANNELS:
            channel_fail = 0
            stop_channel = False
            for bz, level in ((1, "省级"), (2, "市县")):
                if stop_channel:
                    break
                for page in range(1, pages + 1):
                    u = (
                        "https://zfcg.henan.gov.cn/henan/list2"
                        f"?channelCode={code}&pageNo={page}&pageSize=16&bz={bz}&gglx=0"
                    )
                    try:
                        r = client.send("GET", u, headers={"Referer": "https://zfcg.henan.gov.cn/"}, tries=1, timeout=(6, 12))
                    except requests.RequestException as exc:
                        channel_fail += 1
                        if channel_fail >= 1:
                            skipped.append(hint)
                            stop_channel = True
                            break
                        continue
                    if r.status_code != 200 or "List2" not in r.text:
                        channel_fail += 1
                        if channel_fail >= 1:
                            skipped.append(f"{hint} HTTP {r.status_code}")
                            stop_channel = True
                            break
                        continue
                    channel_fail = 0
                    r.encoding = "utf-8"
                    soup = BeautifulSoup(r.text, "html.parser")
                    lis = soup.select("div.List2 li")
                    if not lis:
                        break
                    listed += len(lis)
                    page_hits = 0
                    for li in lis:
                        a = li.find("a")
                        if not a or not a.get("href"):
                            continue
                        title = a.get("title") or a.get_text(strip=True)
                        hits = keyword_hits(title, rules)
                        if not hits:
                            continue
                        page_hits += 1
                        blues = li.select("span.Blue")
                        buyer = blues[0].get_text(strip=True) if blues else ""
                        date_el = li.select_one("span.Gray")
                        href = a["href"]
                        full = href if href.startswith("http") else "https://zfcg.henan.gov.cn" + href
                        city = henan_city(title + buyer, rules) or ("" if level == "省级" else "")
                        rec = make_item(
                            title=title,
                            buyer=buyer,
                            publish_date=ymd_from_compact(date_el.get_text(strip=True) if date_el else ""),
                            source=name,
                            source_id="henan",
                            url=full,
                            region="henan",
                            city=city,
                            province="河南",
                            type_hint=hint,
                            keywords=hits,
                        )
                        prev = found.get(rec["id"])
                        if prev:
                            prev["keywords"] = sorted(set(prev["keywords"] + hits))
                        else:
                            found[rec["id"]] = rec
    except Exception as exc:
        # 已经抓到的保留，状态标成部分成功或失败
        status = "partial" if found else "fail"
        return list(found.values()), {
            "id": "henan",
            "name": name,
            "status": status,
            "listed": listed,
            "kept": len(found),
            "note": str(exc)[:180],
        }

    detailed = 0
    ordered = focus_first(list(found.values()))
    detail_fail = 0
    for rec in ordered:
        if detail_budget[0] <= 0 or detail_fail >= 2:
            break
        try:
            r = client.send("GET", rec["url"], headers={"Referer": "https://zfcg.henan.gov.cn/"}, tries=1, timeout=(8, 16))
            r.encoding = "utf-8"
            text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:20000]
        except Exception:
            detail_fail += 1
            continue
        detail_fail = 0
        detail_budget[0] -= 1
        detailed += 1
        rec["budget"] = rec["budget"] or parse_money(text)
        rec["deadline"] = rec["deadline"] or parse_deadline(text)
        rec["buyer"] = rec["buyer"] or parse_party(text, "采购人|采购单位|招标人")
        rec["winner"] = rec["winner"] or parse_party(text, "中标供应商|成交供应商|中标人|成交人")
        if not rec["city"]:
            rec["city"] = henan_city(text[:1500], rules)
    note = f"列表 {listed} 条，标题命中 {len(found)} 条，补详情 {detailed} 条"
    if detail_fail >= 2:
        note += "；详情页中途超时，其余只保留列表字段"
    if skipped:
        note += "；这些栏目超时已跳过：" + "、".join(skipped)
    status = "partial" if skipped and found else "ok" if found or not skipped else "fail"
    if not found and skipped:
        status = "fail"
        note = "栏目超时：" + "、".join(skipped)
    return list(found.values()), {"id": "henan", "name": name, "status": status, "listed": listed, "kept": len(found), "note": note}


def ggzy_body_url(url: str) -> str:
    return url.replace("/information/deal/html/a/", "/information/deal/html/b/")


def fetch_ggzy(client: Client, rules: dict, pages: int, time_range: str, detail_budget: list[int]) -> tuple[list[dict], dict]:
    name = "全国公共资源交易平台"
    api = "https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList"
    found: dict[str, dict] = {}
    listed = 0
    scopes = [""] + [str(code) for code in (rules.get("重点省份") or [])]
    try:
        for kw in rules["搜索关键词"]:
            for scope in scopes:
                page_limit = pages if not scope else 1
                for page in range(1, page_limit + 1):
                    form = {
                        "DEAL_CLASSIFY": "02",
                        "DEAL_TIME": time_range or "04",
                        "FINDTXT": kw,
                        "PAGENUMBER": str(page),
                    }
                    if scope:
                        form["DEAL_PROVINCE"] = scope
                    r = client.send(
                        "POST",
                        api,
                        data=form,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": "https://www.ggzy.gov.cn/deal/dealList.html",
                            "X-Pass-Token": "",
                        },
                        tries=2,
                    )
                    try:
                        payload = r.json()
                    except Exception as exc:
                        raise RuntimeError(f"不是 JSON：HTTP {r.status_code}") from exc
                    code = payload.get("code")
                    if code in (829, 800, 801, 804):
                        raise RuntimeError(payload.get("message") or f"接口返回 {code}（验证码或限流）")
                    if code != 200:
                        raise RuntimeError(payload.get("message") or f"接口返回 {code}")
                    records = ((payload.get("data") or {}).get("records")) or []
                    listed += len(records)
                    if not records:
                        break
                    for entry in records:
                        title = re.sub(r"\s+", " ", entry.get("title") or "").strip()
                        hits = keyword_hits(title, rules)
                        if not hits:
                            continue
                        rel = entry.get("url") or ""
                        full = rel if rel.startswith("http") else urljoin("https://www.ggzy.gov.cn", rel)
                        province = entry.get("provinceText") or ""
                        info = str(entry.get("informationType") or "")
                        hint = GGZY_TYPE.get(info) or entry.get("informationTypeText") or "招标公告"
                        region, city = "national", province
                        if "河南" in province:
                            region, city = "henan", henan_city(title, rules)
                        elif "广东" in province:
                            if "广州" in title:
                                region, city = "guangzhou", "广州"
                            else:
                                region, city = "guangdong", ""
                        rec = make_item(
                            title=title,
                            publish_date=ymd_from_compact(entry.get("publishTime") or ""),
                            deadline=ymd_from_compact(entry.get("deadline") or ""),
                            source=name,
                            source_id="ggzy",
                            url=full,
                            region=region,
                            city=city,
                            province=province.replace("省", "").replace("市", ""),
                            type_hint=hint,
                            keywords=hits,
                        )
                        rec["_platform"] = entry.get("transactionSourcesPlatformText") or ""
                        prev = found.get(rec["id"])
                        if prev:
                            prev["keywords"] = sorted(set(prev["keywords"] + hits))
                        else:
                            found[rec["id"]] = rec
    except Exception as exc:
        return list(found.values()), {
            "id": "ggzy",
            "name": name,
            "status": "partial" if found else "fail",
            "listed": listed,
            "kept": len(found),
            "note": str(exc)[:180],
        }

    detailed = 0
    ordered = focus_first(list(found.values()))
    for rec in ordered:
        if detail_budget[0] <= 0:
            break
        if "/information/deal/html/" not in rec["url"]:
            continue
        body_url = ggzy_body_url(rec["url"])
        try:
            r = client.send("GET", body_url, headers={"Referer": "https://www.ggzy.gov.cn/"}, tries=1, timeout=(8, 16))
            r.encoding = "utf-8"
        except Exception:
            continue
        detail_budget[0] -= 1
        detailed += 1
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)[:20000]
        link = soup.select_one("a[href]")
        for a in soup.select("a[href]"):
            label = a.get_text(strip=True)
            href = a.get("href") or ""
            if "原文" in label and href.startswith("http"):
                rec["url"] = href
                rec["id"] = item_id(href)
                break
        rec["budget"] = rec["budget"] or parse_money(text)
        rec["deadline"] = rec["deadline"] or parse_deadline(text)
        rec["buyer"] = rec["buyer"] or parse_party(text, "采购人|采购单位|招标人")
        rec["winner"] = rec["winner"] or parse_party(text, "中标供应商|成交供应商|中标人|成交人")
        if rec.get("_platform") and not rec["buyer"]:
            pass
    for rec in found.values():
        rec.pop("_platform", None)
    note = f"近一月检索 {listed} 条，标题命中 {len(found)} 条，补详情 {detailed} 条"
    return list(found.values()), {"id": "ggzy", "name": name, "status": "ok", "listed": listed, "kept": len(found), "note": note}


def probe(client: Client, sid: str, name: str, url: str, ok_if) -> dict:
    try:
        r = client.send("GET", url, tries=1, timeout=(8, 15), headers={"User-Agent": UA})
        r.encoding = "utf-8"
        text = r.text[:4000]
        good, note = ok_if(r.status_code, text)
        return {"id": sid, "name": name, "status": "ok" if good else "fail", "listed": 0, "kept": 0, "note": note}
    except Exception as exc:
        return {"id": sid, "name": name, "status": "fail", "listed": 0, "kept": 0, "note": str(exc)[:180]}


def probe_ccgp(client: Client) -> dict:
    url = "https://search.ccgp.gov.cn/bxsearch?searchtype=1&page_index=1&bidSort=0&pinMu=0&bidType=0&dbselect=bidx&kw=%E6%99%BA%E6%85%A7%E6%95%99%E5%AE%A4&timeType=2"

    def judge(code, text):
        if code == 200 and "频繁" not in text and "vT-srch-result" in text:
            return True, "搜索页可打开。全国公告已由全国公共资源交易平台抓取，这里不重复入库。"
        if "频繁" in text:
            return False, f"HTTP {code}，页面提示访问过于频繁"
        return False, f"HTTP {code}，未看到搜索结果"

    return probe(client, "ccgp", "中国政府采购网", url, judge)


def probe_gdgpo(client: Client) -> dict:
    def judge(code, text):
        if code == 200 and len(text) > 200:
            return True, "首页可打开。公告数据与广东省公共资源交易平台同源，未重复抓取。"
        return False, f"HTTP {code}"

    return probe(client, "gdgpo", "广东省政府采购网", "https://gdgpo.czt.gd.gov.cn/", judge)


def probe_gz(client: Client) -> dict:
    def judge(code, text):
        if code == 200 and "403" not in text[:200]:
            return True, "首页可打开。广州政府采购已包含在省公共资源交易平台中，未重复抓取。"
        return False, f"HTTP {code}，WAF 拒绝" if code == 403 else f"HTTP {code}"

    return probe(client, "gzggzy", "广州公共资源交易中心", "https://www.gzggzy.cn/", judge)


_TITLE_SUFFIXES = [
    "公开招标公告", "中标成交结果公告", "中标结果公告", "成交结果公告", "结果公告",
    "更正事项公告", "更正公告", "竞争性谈判公告", "竞争性磋商公告", "招标公告",
    "成交公告", "合同公示", "合同公告", "采购公告", "谈判公告", "磋商公告",
]


def focus_first(rows: list[dict]) -> list[dict]:
    rank = {"henan": 0, "guangzhou": 1, "guangdong": 2}
    rows = sorted(rows, key=lambda r: r.get("publish_date") or "", reverse=True)
    rows.sort(key=lambda r: rank.get(r.get("region"), 3))
    return rows


def loose_title(title: str) -> str:
    text = norm_title(title).replace("的", "")
    for suf in _TITLE_SUFFIXES:
        text = text.replace(norm_title(suf), "")
    return re.sub(r"[a-z0-9]+$", "", text)


def refine_region(rec: dict, rules: dict) -> None:
    blob = "".join([rec.get("title") or "", rec.get("buyer") or "", rec.get("city") or ""])
    province = rec.get("province") or ""
    if "河南" in province or rec.get("region") == "henan":
        rec["region"] = "henan"
        city = henan_city(blob, rules)
        if city:
            rec["city"] = city
        rec["province"] = rec.get("province") or "河南"
        return
    if "广州" in blob and ("广东" in province or rec.get("region") in ("guangdong", "guangzhou")):
        rec["region"] = "guangzhou"
        rec["city"] = "广州"
        rec["province"] = rec.get("province") or "广东"


def richness(rec: dict) -> tuple:
    url = rec.get("url") or ""
    direct = 0 if "ggzy.gov.cn/information" in url else 1
    provincial = rec.get("source_id") in ("gd", "henan")
    return (
        1 if rec.get("budget") else 0,
        1 if rec.get("deadline") else 0,
        1 if rec.get("buyer") else 0,
        1 if rec.get("winner") else 0,
        direct,
        1 if provincial else 0,
    )


def normalize_hint(title: str, hint: str, rules: dict) -> str:
    text = f"{title or ''} {hint or ''}"
    for rule in rules.get("公告类型") or []:
        if any(k and k in text for k in rule.get("关键词") or []):
            return rule.get("名称") or hint
    for rule in rules.get("公告类型") or []:
        if rule.get("名称") == hint:
            return hint
    return hint or "招标公告"


def merge_items(groups: list[list[dict]], keep_days: int, rules: dict | None = None) -> list[dict]:
    by_key: dict[str, dict] = {}
    for group in groups:
        for rec in group:
            if rules:
                rec["type_hint"] = normalize_hint(rec.get("title") or "", rec.get("type_hint") or "", rules)
                refine_region(rec, rules)
            hint = rec.get("type_hint") or ""
            rec["budget"] = sanitize_budget(rec.get("budget"))
            rec["buyer"] = plausible_org(rec.get("buyer") or "")
            rec["winner"] = plausible_org(rec.get("winner") or "")
            base = "|".join([
                loose_title(rec.get("title") or ""),
                hint,
                (rec.get("publish_date") or "")[:7],
            ])
            province = rec.get("province") or ""
            key = base + "|" + province
            if province and (base + "|") in by_key:
                key = base + "|"
            elif not province:
                for existing in by_key:
                    if existing.startswith(base + "|") and existing != base + "|":
                        key = existing
                        break
            if not key.strip("|"):
                continue
            prev = by_key.get(key)
            if prev is None or richness(rec) > richness(prev):
                keywords = sorted(set((prev or {}).get("keywords") or []) | set(rec.get("keywords") or []))
                rec = dict(rec)
                rec["keywords"] = keywords
                if prev and not rec.get("budget"):
                    rec["budget"] = prev.get("budget")
                if prev and not rec.get("deadline"):
                    rec["deadline"] = prev.get("deadline")
                if prev and not rec.get("buyer"):
                    rec["buyer"] = prev.get("buyer")
                if prev and not rec.get("winner"):
                    rec["winner"] = prev.get("winner")
                by_key[key] = rec
            else:
                prev["keywords"] = sorted(set(prev.get("keywords") or []) | set(rec.get("keywords") or []))
                for field in ("budget", "deadline", "buyer", "winner"):
                    if not prev.get(field) and rec.get(field):
                        prev[field] = rec[field]
    cutoff = (now_cst() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    items = [rec for rec in by_key.values() if not rec.get("publish_date") or rec["publish_date"] >= cutoff]
    items.sort(key=lambda r: (r.get("publish_date") or "", r.get("title") or ""), reverse=True)
    return items


def load_previous() -> tuple[list[dict], list[dict]]:
    if not DATA_PATH.exists():
        return [], []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    return data.get("items") or [], data.get("sources") or []


def write_output(items: list[dict], sources: list[dict]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": now_cst().strftime("%Y-%m-%d %H:%M"),
        "timezone": "Asia/Shanghai",
        "runner": os.environ.get("FETCH_RUNNER") or "local",
        "sources": sources,
        "count": len(items),
        "items": items,
    }
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def self_check() -> None:
    rules = load_rules()
    assert keyword_hits("某某小学智慧教室建设项目", rules) == ["智慧教室"]
    assert keyword_hits("食堂食材采购智慧教室", rules) == []
    assert keyword_hits("传感器采购一批", rules) == []
    assert "传感器" in keyword_hits("数字化实验室传感器采购", rules)
    assert parse_money("预算金额：128.5万元") == 1285000
    assert parse_money("中标金额：320,000.00元") == 320000
    assert parse_money("中标公告 2026-10-13") is None
    assert sanitize_budget(202609052) is None
    assert parse_deadline("投标截止时间：2026年10月28日 09:30") == "2026-10-28"
    assert henan_city("信阳师范大学设备采购", rules) == "信阳"
    assert henan_city("平顶山学院实验室", rules) == "平顶山"
    assert henan_city("禹州市教育体育局录播教室", rules) == "许昌"
    assert keyword_hits("光山县初中理、化、生 实验室建设项目", rules)
    a = make_item(title="同一公告", url="https://a.example/1", budget=None, source_id="ggzy", type_hint="招标公告")
    b = make_item(title="同一公告", url="https://b.example/2", budget=1000, source_id="gd", type_hint="招标公告", buyer="某学校")
    merged = merge_items([[a], [b]], 120, rules)
    assert len(merged) == 1 and merged[0]["budget"] == 1000 and merged[0]["source_id"] == "gd"
    print("self-check ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取公开招标公告并写入 data/tenders.json")
    parser.add_argument("--only", default="", help="逗号分隔：gd,henan,ggzy,ccgp,gdgpo,gzggzy")
    parser.add_argument("--check", action="store_true", help="只跑本地规则自测")
    parser.add_argument("--remerge", action="store_true", help="不访问网络，只按当前规则合并已有数据")
    args = parser.parse_args()
    if args.check:
        self_check()
        return 0
    if args.remerge:
        rules = load_rules()
        keep_days = int((rules.get("抓取") or {}).get("保留天数") or 120)
        previous_items, previous_sources = load_previous()
        items = merge_items([previous_items], keep_days, rules)
        write_output(items, previous_sources)
        print(f"remerge {DATA_PATH} items={len(items)}", flush=True)
        return 0

    rules = load_rules()
    fetch_cfg = rules.get("抓取") or {}
    pages = int(fetch_cfg.get("每个关键词页数") or 1)
    henan_pages = int(fetch_cfg.get("河南每个栏目页数") or 3)
    delay = float(fetch_cfg.get("请求间隔秒") or 1.5)
    detail_cap = int(fetch_cfg.get("详情上限") or 60)
    keep_days = int(fetch_cfg.get("保留天数") or 120)
    time_range = str(fetch_cfg.get("全国时间范围") or "04")
    wanted = {s.strip() for s in args.only.split(",") if s.strip()} or None

    client = Client(delay)
    batches: list[list[dict]] = []
    sources: list[dict] = []

    def run(sid: str, fn):
        if wanted and sid not in wanted:
            return
        print(f"== {sid}", flush=True)
        try:
            items, status = fn()
        except Exception as exc:
            traceback.print_exc()
            items, status = [], {"id": sid, "name": sid, "status": "fail", "listed": 0, "kept": 0, "note": str(exc)[:180]}
        print(f"   {status['status']} kept={status.get('kept')} {status.get('note')}", flush=True)
        batches.append(items)
        sources.append(status)

    run("gd", lambda: fetch_gd(client, rules, pages, [max(24, detail_cap // 2)]))
    run("henan", lambda: fetch_henan(client, rules, henan_pages, [max(8, detail_cap // 5)]))
    run("ggzy", lambda: fetch_ggzy(client, rules, pages, time_range, [max(12, detail_cap // 3)]))
    run("ccgp", lambda: ([], probe_ccgp(client)))
    run("gdgpo", lambda: ([], probe_gdgpo(client)))
    run("gzggzy", lambda: ([], probe_gz(client)))

    previous_items, previous_sources = load_previous()
    items = merge_items(batches + [previous_items], keep_days, rules)
    if wanted:
        seen = {s["id"] for s in sources}
        sources = sources + [s for s in previous_sources if s.get("id") not in seen]
    write_output(items, sources)
    print(f"wrote {DATA_PATH} items={len(items)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        # 尽量留下失败状态，避免页面完全没有更新时间
        try:
            prev = load_previous()
            write_output(
                prev[0] if isinstance(prev, tuple) else prev,
                [{"id": "fetch", "name": "抓取任务", "status": "fail", "listed": 0, "kept": len(prev[0] if isinstance(prev, tuple) else prev), "note": "脚本异常，保留上次数据"}],
            )
        except Exception:
            pass
        sys.exit(1)
