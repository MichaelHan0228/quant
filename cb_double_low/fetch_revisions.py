"""
下修公告日期补采（巨潮，全量正股）
====================================
东财名单的 TRANSFER_PRICE 全为空 → 无法拿"最终转股价"做锚。
改走：巨潮公告检索"向下修正转股价格"，拿到每只正股的下修公告日期。
引擎按交易所规则近似重建下修后转股价：
  新转股价 ≈ max(前20日均价, 公告日前一日收盘价)（下修不得低于该线，多数公司修到底）
"""
import os
import time

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
UA = "Mozilla/5.0"
_ORGID_CACHE = {}


def _orgid(code: str) -> str:
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


def main():
    cb = pd.read_csv(os.path.join(DATA, "cb_list.csv"),
                     dtype={"SECURITY_CODE": str, "CONVERT_STOCK_CODE": str})
    stocks = sorted(cb["CONVERT_STOCK_CODE"].dropna().astype(str).unique())
    records = []
    for i, stock in enumerate(stocks):
        try:
            r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                data={"stock": f"{stock},{_orgid(stock)}", "tabName": "fulltext",
                      "pageSize": "30", "pageNum": "1", "column": "", "category": "",
                      "plate": "", "seDate": "", "searchkey": "向下修正转股价格",
                      "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"},
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": "https://www.cninfo.com.cn/new/disclosure",
                         "Origin": "https://www.cninfo.com.cn"}, timeout=15)
            for a in (r.json().get("announcements") or []):
                import re as _re
                title = _re.sub(r"</?em>", "", a.get("announcementTitle", ""))
                if ("向下修正" in title and "转股价格" in title
                        and "暂不" not in title and "不向下" not in title
                        and "预计触发" not in title and "提议" not in title and "建议" not in title):
                    ts = a.get("announcementTime")
                    dt = pd.to_datetime(int(ts), unit="ms").strftime("%Y-%m-%d") if ts else ""
                    records.append({"stock": stock, "date": dt, "title": title})
        except Exception as e:
            print(f"[WARN] {stock}: {str(e)[:50]}")
        if (i + 1) % 100 == 0:
            print(f"{i+1}/{len(stocks)} 记录{len(records)}", flush=True)
        time.sleep(0.25)
    df = pd.DataFrame(records)
    df.to_csv(os.path.join(DATA, "downward_revisions.csv"), index=False, encoding="utf-8-sig")
    print(f"完成: {len(df)} 条下修公告, 涉及 {df['stock'].nunique() if len(df) else 0} 只正股")


if __name__ == "__main__":
    main()
