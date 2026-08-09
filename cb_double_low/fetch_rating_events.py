# -*- coding: utf-8 -*-
"""评级变动事件采集（巨潮公告 + PDF 解析）

背景: universe.csv 的 RATING 是最新快照, 直接过滤有前视偏差
(如搜特转债上市时 AA, 2022 年才下调到 B/CCC)。
对 clean_rating 不在 ALLOWED_RATINGS 的券, 用巨潮检索其正股的评级公告,
下载 PDF 抽前 2 页文本解析转债信用等级变动。

输出: data/rating_events.csv
  列: code,stock,date,old_rating,new_rating,title,parse_ok
  无公告的券不写记录(回测侧按"始终不合格"处理); 解析失败 parse_ok=False 仍落盘。

用法:
    python fetch_rating_events.py                # 全量(快照评级不合格的券)
    python fetch_rating_events.py --codes 128100 # 测试指定券
"""
import argparse
import io
import os
import re
import sys
import time

import pandas as pd
import requests
from pypdf import PdfReader

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT_CSV = os.path.join(DATA, "rating_events.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SLEEP = 0.35
RETRY = 3

ALLOWED_RATINGS = {"AAA", "AA+", "AA", "AA-", "A+"}

_ORGID_CACHE = {}


def clean_rating(s):
    """评级归一化: strip 后去掉末尾大小写不敏感的 sti 后缀和空白"""
    s = str(s).strip()
    s = re.sub(r"(?i)\s*sti\s*$", "", s).strip()
    return s


def _orgid(code):
    global _ORGID_CACHE
    if not _ORGID_CACHE:
        try:
            r = requests.get("http://www.cninfo.com.cn/new/data/szse_stock.json",
                             headers={"User-Agent": UA}, timeout=15)
            _ORGID_CACHE = {s["code"]: s["orgId"] for s in r.json().get("stockList", [])}
        except Exception:
            pass
    if code in _ORGID_CACHE:
        return _ORGID_CACHE[code]
    return f"gssh0{code}" if code.startswith("6") else f"gssz0{code}"


def _post_query(stock, page_num, searchkey="评级"):
    data = {"stock": f"{stock},{_orgid(stock)}", "tabName": "fulltext",
            "pageSize": "50", "pageNum": str(page_num), "column": "",
            "category": "", "plate": "", "seDate": "", "searchkey": searchkey,
            "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
    headers = {"User-Agent": UA,
               "Content-Type": "application/x-www-form-urlencoded",
               "Referer": "https://www.cninfo.com.cn/new/disclosure",
               "Origin": "https://www.cninfo.com.cn"}
    last = None
    for i in range(RETRY):
        try:
            r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                              data=data, headers=headers, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"公告查询失败: {last}")


def search_rating_announcements(stock):
    """巨潮检索正股评级公告(通用翻页), 返回 [{date,title,url}]

    用两个 searchkey: "评级"(跟踪评级/信用评级报告) + "信用等级"
    (临时下调公告如"关于下调...主体及相关债项信用等级的公告"只含后者)。
    按 url 去重合并。"""
    out = {}
    for key in ("评级", "信用等级"):
        page = 1
        total = None
        fetched = 0
        while True:
            js = _post_query(stock, page, searchkey=key)
            if total is None:
                total = js.get("totalAnnouncement") or 0
            anns = js.get("announcements") or []
            fetched += len(anns)
            for a in anns:
                title = re.sub(r"</?em>", "", a.get("announcementTitle", "") or "")
                ts = a.get("announcementTime")
                dt = pd.to_datetime(int(ts), unit="ms").strftime("%Y-%m-%d") if ts else ""
                url = a.get("adjunctUrl", "") or ""
                if url:
                    out[url] = {"date": dt, "title": title, "url": url}
            page += 1
            if not anns or fetched >= total or page > 20:
                break
            time.sleep(SLEEP)
        time.sleep(SLEEP)
    return sorted(out.values(), key=lambda x: x["date"], reverse=True)


def title_keep(title):
    """标题过滤: 评级相关且指向转债(或债项整体), 排除纯公司债报告"""
    if not any(k in title for k in ("跟踪评级", "信用评级", "信用等级")):
        return False
    cb = ("可转换" in title) or ("转债" in title)
    if "公司债" in title and not cb:
        return False  # 纯公司债评级报告
    return True


def _fetch_pdf(url):
    full = "https://static.cninfo.com.cn/" + url
    last = None
    for i in range(RETRY):
        try:
            r = requests.get(full, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise RuntimeError(f"非PDF响应(反爬?): {r.content[:20]!r}")
            return r.content
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"PDF下载失败: {last}")


_GRADE = r"([ABC]{1,3}[+-]?)"


def parse_rating(text):
    """从公告文本解析转债信用等级变动, 返回 (old, new) 或 None"""
    t = re.sub(r"\s+", "", text)  # PDF 抽出的字符间常有空白, 统一去掉
    pats = [
        # "搜特转债"的信用等级由 B 下调为 CCC
        (r"信用等级由" + _GRADE + r"(?:下调|上调|调整)?为?" + _GRADE, 2),
        (r"转债.{0,30}?信用等级[为：:]" + _GRADE, 1),
        (r"维持.{0,20}?" + _GRADE + r".{0,10}?信用等级", 1),
        (r"信用等级[为：:]" + _GRADE, 1),
    ]
    for pat, ng in pats:
        m = re.search(pat, t)
        if m:
            if ng == 2:
                return m.group(1), m.group(2)
            return "", m.group(1)
    return None


def extract_text_head(pdf_bytes, pages=2):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for p in reader.pages[:pages]:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            pass
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="", help="只处理指定转债代码, 逗号分隔")
    args = ap.parse_args()

    uni = pd.read_csv(os.path.join(DATA, "universe.csv"), encoding="utf-8-sig",
                      dtype={"SECURITY_CODE": str, "CONVERT_STOCK_CODE": str})
    uni["code"] = uni["SECURITY_CODE"].str.zfill(6)
    uni["stock"] = (uni["CONVERT_STOCK_CODE"].fillna("")
                    .str.replace(r"\..*$", "", regex=True).str.zfill(6))
    uni["clean"] = uni["RATING"].map(lambda x: clean_rating(x) if pd.notna(x) else "")
    if args.codes:
        targets = uni[uni["code"].isin(
            [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()])]
    else:
        targets = uni[~uni["clean"].isin(ALLOWED_RATINGS)]
    targets = targets.reset_index(drop=True)
    print(f"待处理 {len(targets)} 只券(快照评级不合格)")

    rows = []
    for i, r in targets.iterrows():
        code, stock = r["code"], r["stock"]
        try:
            anns = [a for a in search_rating_announcements(stock)
                    if title_keep(a["title"])]
            kept = 0
            for a in anns:
                if not a["url"].lower().endswith(".pdf"):
                    continue
                kept += 1
                old, new, ok = "", "", False
                try:
                    pdf = _fetch_pdf(a["url"])
                    res = parse_rating(extract_text_head(pdf))
                    if res:
                        old, new = res
                        ok = True
                except Exception as e:
                    print(f"[WARN] {code} PDF解析失败 {a['date']}: {str(e)[:60]}",
                          flush=True)
                rows.append({"code": code, "stock": stock, "date": a["date"],
                             "old_rating": old, "new_rating": new,
                             "title": a["title"], "parse_ok": ok})
                time.sleep(SLEEP)
            if kept == 0:
                print(f"[INFO] {code}({stock}) 无符合条件的评级公告", flush=True)
        except Exception as e:
            print(f"[WARN] {code}({stock}): {str(e)[:80]}", flush=True)
        if (i + 1) % 20 == 0:
            print(f"{i+1}/{len(targets)} 已采 {len(rows)} 条", flush=True)
        time.sleep(SLEEP)

    df = pd.DataFrame(rows, columns=["code", "stock", "date", "old_rating",
                                     "new_rating", "title", "parse_ok"])
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    n_ok = int(df["parse_ok"].sum()) if len(df) else 0
    print(f"完成: {len(df)} 条事件, 涉及 {df['code'].nunique() if len(df) else 0} 只券, "
          f"parse_ok {n_ok}, parse_fail {len(df) - n_ok}")


if __name__ == "__main__":
    main()
