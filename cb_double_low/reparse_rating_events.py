# -*- coding: utf-8 -*-
"""评级事件重解析: 用改进的解析器重新解析 parse_fail 的公告 PDF

背景: data/rating_events.csv 中 501/805 条 parse_ok=False。
原 fetch_rating_events.py 只抽前 2 页且正则覆盖窄。本脚本:
  1. 重新检索巨潮拿到 parse_fail 行的公告 URL (CSV 未存 URL)
  2. 下载 PDF 缓存到 data/rating_pdf_cache/ (已缓存的直接复用, 不重复下载)
  3. 用改进解析器(最多抽 5 页 + 多组句式)重解析
  4. 合并原 parse_ok 行, 输出 data/rating_events_v2.csv (不覆盖原文件)

用法:
    python reparse_rating_events.py --sample 20   # 抽样验证(只打印不落盘)
    python reparse_rating_events.py               # 全量重解析, 写 v2
"""
import argparse
import io
import os
import re
import sys
import time

import pandas as pd
from pypdf import PdfReader

import fetch_rating_events as fre  # 复用检索/下载逻辑

sys.stdout.reconfigure(encoding="utf-8")

DATA = fre.DATA
IN_CSV = os.path.join(DATA, "rating_events.csv")
OUT_CSV = os.path.join(DATA, "rating_events_v2.csv")
CACHE = os.path.join(DATA, "rating_pdf_cache")
SLEEP = 0.6

_G = r"([ABC]{1,3}[+-]?)"


def normalize(text):
    t = re.sub(r"\s+", "", text)
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"),
                 ("＂", '"'), ("：", ":")):
        t = t.replace(a, b)
    return t


def text_quality(text):
    """文本可用度: 常规字符占比。扫描件/坏字体(CID乱码)占比低"""
    if not text:
        return 0.0
    good = len(re.findall(r"[0-9A-Za-z一-鿿，。、；：？！“”‘’（）()《》【】\[\]—…·%‰/\-.+<'\">\s]",
                          text))
    return good / len(text)


def _find_near(t, anchor_pat, grade_pat, span=120, last=False):
    """在 anchor 匹配位置附近找债项等级"""
    ms = list(re.finditer(anchor_pat, t))
    if not ms:
        return None
    m = ms[-1] if last else ms[0]
    seg = t[max(0, m.start() - 10): m.end() + span]
    g = re.search(grade_pat, seg)
    return g.group(1) if g else None


def _grades(m):
    """从 match 的 groups 里挑出真正的等级(跳过分支组)"""
    return [g for g in m.groups()
            if g and re.fullmatch(r"[ABC]{1,3}[+-]?", g)]


def _split_grades(s):
    """把封面表格里粘连的等级串拆成 [本次, 上次], 拆不开返回 [s]"""
    if re.fullmatch(r"[ABC]{1,3}[+-]?", s):
        # 整串是单个合法等级, 但也可能是两个粘连(如 "AA"=A|A), 由调用方决定
        pass
    for i in range(1, len(s)):
        a, b = s[:i], s[i:]
        if (re.fullmatch(r"[ABC]{1,3}[+-]?", a)
                and re.fullmatch(r"[ABC]{1,3}[+-]?", b)):
            return [a, b]
    if re.fullmatch(r"[ABC]{1,3}[+-]?", s):
        return [s]
    return []


def parse_rating_v2(text):
    """改进版: 返回 (old, new) 或 None。优先转债/债项, 兜底主体"""
    t = normalize(text)
    if not t:
        return None

    bond_grade = r"债项?信用等级[为是:]?\s*\"?" + _G

    # R0 中证鹏元封面表: "评级结果本次评级上次评级主体信用等级AA评级展望
    #    负面负面华钰转债AA" —— 本次/上次两列的值粘连, 需拆分
    i = t.find("本次评级上次评级")
    if i >= 0:
        seg = t[i:i + 160]
        m = re.search(r"转债\"?([ABC+-]{1,7})", seg)
        if m:
            gs = _split_grades(m.group(1))
            if len(gs) >= 2:
                return gs[1], gs[0]
            if gs:
                return "", gs[0]
        m = re.search(r"主体信用等级([ABC+-]{1,7})", seg)
        if m:
            gs = _split_grades(m.group(1))
            if len(gs) >= 2:
                return gs[1], gs[0]
            if gs:
                return "", gs[0]

    # R1 调整前/调整后 ... 债项信用等级
    idx = t.find("调整后")
    if idx >= 0:
        new = _find_near(t[idx:], r"", bond_grade, span=120)
        mo = list(re.finditer(bond_grade, t[:idx]))
        old = mo[-1].group(1) if mo else None
        if new:
            return (old or ""), new

    # R1.5 前次/本次分节公告: "前次债券评级:..."富淼转债"的信用等级为"A+"
    #    本次债券评级:..."富淼转债"的信用等级为"A"" —— 按本次/前次分段取数
    bond_is = r"转债\"?的?信用等级[为是:]\s*\"?" + _G
    for marker in ("本次债券评级", "本次跟踪信用评级结果", "本次跟踪评级结果",
                   "本次评级结果", "本期债券评级"):
        j = t.find(marker)
        if j < 0:
            continue
        m_new = re.search(bond_is, t[j:j + 150])
        if m_new:
            mo = list(re.finditer(bond_is, t[:j]))
            return (mo[-1].group(1) if mo else ""), m_new.group(1)

    # R2 前次/本次 评级
    r2 = [
        r"前次债[券项]?评级[结果]*[:为]?\s*\"?" + _G + r"\"?.{0,80}?本次债[券项]?评级[结果]*[:为]?\s*\"?" + _G,
        r"前次(债项|债券)?(信用)?评级结果?[:为]?\s*\"?" + _G + r"\"?.{0,80}?本次(债项|债券)?(信用)?评级结果?[:为]?\s*\"?" + _G,
    ]
    for pat in r2:
        m = re.search(pat, t)
        if m:
            gs = [g for g in m.groups() if g and re.fullmatch(r"[ABC]{1,3}[+-]?", g)]
            if len(gs) >= 2:
                return gs[0], gs[-1]

    # R2.5 新世纪封面表: "本次跟踪:A-/负面/A-/..." + "前次评级:A/负面/A/..."
    # (格式: 主体/展望/债项/评级时间)
    m_new = re.search(r"本次跟踪[:：]\s*\"?" + _G + r"/[^/]{0,8}/\"?" + _G, t)
    m_old = re.search(r"前次评级[:：]\s*\"?" + _G + r"/[^/]{0,8}/\"?" + _G, t)
    if m_new:
        new = m_new.group(2)
        old = m_old.group(2) if m_old else ""
        return old, new

    # R3 转债限定的 "由X下调/上调/调整至Y" (\d? 容忍页码粘连如 "由4BBB-")
    r3 = [
        r"转债\"?的?(债项|债券)?信用等级由\"?\d?\"?" + _G + r"\"?(下调|上调|调整|调低|调高)?(至|为)\"?" + _G,
        r"转债\"?.{0,12}?评级结果?由\"?\d?\"?" + _G + r"\"?(下调|上调|调整|调低|调高)?(至|为)\"?" + _G,
        r"信用等级由\"?\d?\"?" + _G + r"\"?(下调|上调|调整|调低|调高)?(至|为)?\"?" + _G,
        r"(评级结果|评级)由\"?\d?\"?" + _G + r"\"?(下调|上调|调整)(至|为)\"?" + _G,
    ]
    for pat in r3:
        m = re.search(pat, t)
        if m:
            gs = [g for g in m.groups() if g and re.fullmatch(r"[ABC]{1,3}[+-]?", g)]
            if len(gs) >= 2:
                return gs[0], gs[1]

    # R4 转债限定的新评级(无旧值)
    r4 = [
        r"维持\"?.{0,12}?转债\"?(的债项|的债券|的)?信用等级[为是:]\s*\"?" + _G,
        r"\"?.{0,12}?转债\"?的信用等级[为:]\s*\"?" + _G,
        r"\"?.{0,12}?转债\"?(的债项|的债券)?(信用)?评级结果?[为:]\s*\"?" + _G,
        # 大公: "富淼转债"的信用等级调整为A / 信用等级维持A+
        r"转债\"?的?信用等级(调整为|调整至|确定为|评定为|维持|上调至|下调至)\s*\"?" + _G,
        # 新世纪/联合: 下调城地转债信用等级至A-级 / 下调"蓝盾转债"的债项信用等级至BBB-
        r"(下调|上调)\"?.{0,8}?转债\"?(的债项|的债券|的)?信用等级至\"?" + _G + r"级?",
        r"债项及评级结果.{0,25}?\"?" + _G,
        r"跟踪债项.{0,25}?\"?" + _G,
        r"本期(债券|转债)信用等级(下调|上调|调整|维持)?[为是:]\s*\"?" + _G,
        r"债券信用等级(下调|上调|调整|维持)?[为:]\s*\"?" + _G,
        r"债项信用等级(下调|上调|调整|维持)?[为是:]\s*\"?" + _G,
    ]
    for pat in r4:
        m = re.search(pat, t)
        if m:
            gs = _grades(m)
            if gs:
                return "", gs[-1]

    # R5 主体兜底
    r5 = [
        r"主体(长期)?信用等级(下调|上调|调整|维持)?[为是:至]\s*\"?" + _G,
        r"主体(信用)?评级结果[:为]?\s*\"?" + _G,
        r"主体(长期)?评级[为:]\s*\"?" + _G,
        r"维持.{0,20}?" + _G + r".{0,10}?信用等级",
    ]
    for pat in r5:
        m = re.search(pat, t)
        if m:
            gs = _grades(m)
            if gs:
                return "", gs[-1]
    return None


def extract_text_pages(pdf_bytes, pages=40):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for p in reader.pages[:pages]:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            pass
    return "\n".join(parts)


def get_pdf(url):
    name = url.replace("/", "_")
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read(), True
    data = fre._fetch_pdf(url)
    with open(path, "wb") as f:
        f.write(data)
    time.sleep(SLEEP)
    return data, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="抽样验证条数(不落盘)")
    args = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)

    df = pd.read_csv(IN_CSV, dtype=str)
    fail = df[df["parse_ok"] == "False"].copy()
    print(f"总 {len(df)} 条, parse_fail {len(fail)} 条待重解析", flush=True)

    if args.sample:
        fail = fail.groupby("code").head(2).head(args.sample).copy()

    search_cache = {}
    stats = {"ok": 0, "garbage": 0, "no_text": 0, "miss_url": 0, "dl_fail": 0, "still_fail": 0}
    results = {}  # (code,date,title) -> (old,new)
    for n, (_, r) in enumerate(fail.iterrows(), 1):
        key = (r["code"], r["date"], str(r["title"]).strip())
        stock = r["stock"]
        try:
            if stock not in search_cache:
                search_cache[stock] = fre.search_rating_announcements(stock)
                time.sleep(SLEEP)
            anns = search_cache[stock]
            hit = [a for a in anns
                   if a["date"] == r["date"] and a["title"].strip() == key[2]]
            if not hit:
                stats["miss_url"] += 1
                continue
            url = hit[0]["url"]
            if not url.lower().endswith(".pdf"):
                stats["miss_url"] += 1
                continue
            pdf, cached = get_pdf(url)
            text = extract_text_pages(pdf, pages=40)
            if len(text.strip()) < 50:
                stats["no_text"] += 1  # 扫描件无文本层
                continue
            if text_quality(text) < 0.72:
                stats["garbage"] += 1  # CID 乱码/坏字体
                continue
            res = parse_rating_v2(text)
            if res:
                results[key] = res
                stats["ok"] += 1
                if args.sample:
                    print(f"[OK] {r['code']} {r['date']} -> {res} | {key[2][:36]}", flush=True)
            else:
                stats["still_fail"] += 1
                if args.sample:
                    print(f"[FAIL] {r['code']} {r['date']} | {key[2][:36]}", flush=True)
        except Exception as e:
            stats["dl_fail"] += 1
            print(f"[WARN] {r['code']} {r['date']}: {str(e)[:70]}", flush=True)
        if n % 50 == 0:
            print(f"{n}/{len(fail)} stats={stats}", flush=True)

    print(f"重解析完成: {stats}", flush=True)

    if args.sample:
        return

    out = df.copy()
    for i, r in out.iterrows():
        if r["parse_ok"] == "False":
            key = (r["code"], r["date"], str(r["title"]).strip())
            if key in results:
                old, new = results[key]
                out.at[i, "old_rating"] = old
                out.at[i, "new_rating"] = new
                out.at[i, "parse_ok"] = "True"
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    n_ok = int((out["parse_ok"] == "True").sum())
    print(f"写出 {OUT_CSV}: 总 {len(out)} 条, parse_ok {n_ok} "
          f"({n_ok / len(out) * 100:.1f}%), fail {len(out) - n_ok}")
    print(f"失败构成: 扫描件无文本 {stats['no_text']}, CID乱码 {stats['garbage']}, "
          f"URL未匹配 {stats['miss_url']}, 下载失败 {stats['dl_fail']}, "
          f"句式未覆盖 {stats['still_fail']}")


if __name__ == "__main__":
    main()
