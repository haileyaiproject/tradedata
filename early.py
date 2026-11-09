#!/usr/bin/env python3
"""관세청 10일 단위 잠정치(전국, 주요 품목) → early.json

사용:  python3 early.py --key <공공데이터포털 인증키>

API (공공데이터포털, XML):
  수출: https://apis.data.go.kr/1220000/prlstMmUtPrviExpAcrs/getPrlstMmUtPrviExpAcrs
  수입: https://apis.data.go.kr/1220000/prlstMmUtPrviImpAcrs/getPrlstMmUtPrviImpAcrs
  파라미터: serviceKey, strtYymm=YYYYMM, endYymm=YYYYMM
  응답 item: priodMon(YYYYMM), priodDt('01~10','01~20','01~31' 등), itemUsdAmt00..10 (천 달러, 00=전체)

종료코드: 0 새 기간 반영됨 / 2 새 기간 아직 없음 / 4 인증키 미등록(활용신청 필요) / 5 네트워크 차단 / 1 그 밖의 오류
"""
import argparse, calendar, datetime as dt, json, os, re, statistics, sys, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

BASE = "https://apis.data.go.kr/1220000"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "early.json")

SERIES = {
    "exp": {
        "path": "/prlstMmUtPrviExpAcrs/getPrlstMmUtPrviExpAcrs",
        "names": ["전체", "반도체", "철강제품", "승용차", "석유제품", "무선통신기기",
                  "선박", "자동차부품", "컴퓨터주변기기", "정밀기기", "가전제품"],
    },
    "imp": {
        "path": "/prlstMmUtPrviImpAcrs/getPrlstMmUtPrviImpAcrs",
        "names": ["전체", "반도체", "원유", "기계류", "가스", "반도체 제조용 장비",
                  "정밀기기", "석유제품", "무선통신기기", "승용차", "석탄"],
    },
}

# 그 품목이 주로 나오는 지역 (대시보드 표의 힌트, shares.json 이 없을 때 쓰임)
HINT = {
    "전체": "", "반도체": "경기(이천·용인·화성·평택)·충북(청주)", "철강제품": "경북(포항)·전남(광양)·충남(당진)",
    "승용차": "울산·경기(화성)·충남(아산)·전남광주", "석유제품": "울산·전남(여수)·충남(서산)",
    "무선통신기기": "경기(수원)·경북(구미)", "선박": "울산·경남(거제)·전남(영암)", "자동차부품": "경기·경남·충남",
    "컴퓨터주변기기": "경기(이천·평택)", "정밀기기": "경기·충남", "가전제품": "경남(창원)·경기",
    "원유": "울산·전남(여수)·충남(서산)", "기계류": "경기·경남", "가스": "인천·경남(통영)·충남(보령)",
    "반도체 제조용 장비": "경기(화성·용인·평택)", "석탄": "충남·강원·경남",
}

FLAG_MOM = 0.20   # 다른 품목 중앙값보다 전월 대비 ±20%p 이상
FLAG_YOY = 0.30   # 다른 품목 중앙값보다 전년 대비 ±30%p 이상


def norm_period(raw):
    """'01~10'→'10', '01~20'→'20', '01~28/29/30/31'·'01~말일'→'말'. 모르면 원문 그대로."""
    s = (raw or "").strip().replace(" ", "")
    m = re.fullmatch(r"0?1~(\d{1,2}|말일?)", s)
    if m:
        d = m.group(1)
        if d in ("10", "20"):
            return d
        if d.startswith("말") or d in ("28", "29", "30", "31"):
            return "말"
    return s


def shift_month(ym, k):
    y, m = int(ym[:4]), int(ym[4:])
    m += k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y}{m:02d}"


def num(s):
    s = (s or "").replace(",", "").strip()
    return float(s) if s else 0.0


def fetch(key, path, strt, end):
    q = urllib.parse.urlencode({"serviceKey": key, "strtYymm": strt, "endYymm": end})
    url = f"{BASE}{path}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "trade-early/1.0"})
    last = None
    for attempt in range(4):  # 연결 끊김은 3번까지 다시 시도 (접속 거부 403은 바로 끝)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
            print(f"retry {attempt + 1}: {e}", file=sys.stderr)
            import time; time.sleep(3 * (attempt + 1))
    print(f"NETWORK: {last}", file=sys.stderr)
    sys.exit(5)


def parse(body, names):
    text = body.decode("utf-8", "replace")
    if "connect_rejected" in text or "<title>Access Denied" in text:
        print("NETWORK: 403 connect_rejected", file=sys.stderr)
        sys.exit(5)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        print("ERROR: XML 아님:", text[:300], file=sys.stderr)
        sys.exit(1)
    reason = root.findtext(".//returnReasonCode")
    code = root.findtext(".//resultCode")
    if reason == "30" or code == "30" or "NOT_REGISTERED" in text:
        print("KEY_NOT_REGISTERED", file=sys.stderr)
        sys.exit(4)
    if reason and reason not in ("00", "0"):
        print(f"ERROR: gateway {reason} {root.findtext('.//returnAuthMsg')}", file=sys.stderr)
        sys.exit(1)
    if code not in ("00", "0"):
        print(f"ERROR: resultCode {code} {root.findtext('.//resultMsg')}", file=sys.stderr)
        sys.exit(1)
    rows, raw = [], set()
    for it in root.findall(".//item"):
        d = {x.tag: (x.text or "").strip() for x in it}
        ym = d.get("priodMon", "").replace(".", "")[:6]
        rawp = d.get("priodDt", "")
        raw.add(rawp)
        vals = [num(d.get(f"itemUsdAmt{i:02d}")) for i in range(len(names))]
        rows.append({"ym": ym, "p": norm_period(rawp), "vals": vals})
    order = {"10": 0, "20": 1, "말": 2}
    rows.sort(key=lambda r: (r["ym"], order.get(r["p"], 9), r["p"]))
    return rows, sorted(raw)


def days_of(ym, p):
    y, m = int(ym[:4]), int(ym[4:])
    return {"10": 10, "20": 20}.get(p, calendar.monthrange(y, m)[1])


def pick(rows, ym, p):
    for r in rows:
        if r["ym"] == ym and r["p"] == p:
            return r
    return None


def ratio(a, b):
    return round(a / b - 1, 3) if b else None


def signals(series, latest):
    ym, p = latest["ym"], latest["p"]
    pm, py = shift_month(ym, -1), shift_month(ym, -12)
    out = []
    for d, s in series.items():
        rows, names = s["rows"], s["names"]
        cur = pick(rows, ym, p); prv_m = pick(rows, pm, p); prv_y = pick(rows, py, p); prv_f = pick(rows, pm, "말")
        if not cur:
            continue
        dc, dp = days_of(ym, p), days_of(pm, "말")
        items = []
        for i, name in enumerate(names):
            c = cur["vals"][i]
            m = prv_m["vals"][i] if prv_m else None
            y = prv_y["vals"][i] if prv_y else None
            f = prv_f["vals"][i] if prv_f else None
            items.append({"dir": d, "item": name, "cur": round(c), "prev_m": round(m) if m is not None else None,
                          "prev_y": round(y) if y is not None else None, "prev_full": round(f) if f is not None else None,
                          "pace": ratio(c, f * dc / dp) if f else None,
                          "mom": ratio(c, m) if m else None, "yoy": ratio(c, y) if y else None, "hint": HINT.get(name, "")})
        base_m = [x["mom"] for x in items[1:] if x["mom"] is not None]
        base_y = [x["yoy"] for x in items[1:] if x["yoy"] is not None]
        bm = statistics.median(base_m) if base_m else 0.0
        by = statistics.median(base_y) if base_y else 0.0
        s["_baseline"] = {"mom": bm, "yoy": by}
        for x in items:
            # 다른 품목(중앙값) 대비 얼마나 더 움직였나: (1+내 증감)/(1+중앙값 증감) - 1
            x["rel_mom"] = round((1 + x["mom"]) / (1 + bm) - 1, 3) if x["mom"] is not None and 1 + bm else None
            x["rel_yoy"] = round((1 + x["yoy"]) / (1 + by) - 1, 3) if x["yoy"] is not None and 1 + by else None
            x["flag"] = x["item"] != "전체" and (
                (x["rel_mom"] is not None and abs(x["rel_mom"]) >= FLAG_MOM) or
                (x["rel_yoy"] is not None and abs(x["rel_yoy"]) >= FLAG_YOY))
        out.extend(items)
    return out, {"cur": days_of(ym, p), "prev": days_of(pm, "말"), "prev_ym": pm}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--force", action="store_true", help="새 기간이 없어도 다시 계산")
    a = ap.parse_args()

    today = dt.date.today()
    end = today.strftime("%Y%m")
    strt = shift_month(end, -14)

    old = {}
    if os.path.exists(a.out):
        try:
            old = json.load(open(a.out, encoding="utf-8"))
        except Exception:
            old = {}

    series = {}
    for d, s in SERIES.items():
        status, body = fetch(a.key, s["path"], strt, end)
        if status == 403 and b"connect_rejected" in body:
            print("NETWORK: 403 connect_rejected", file=sys.stderr); sys.exit(5)
        rows, raw = parse(body, s["names"])
        series[d] = {"names": s["names"], "rows": rows, "raw_dt_samples": raw}
        print(f"{d}: {len(rows)} rows, periods {raw}", file=sys.stderr)

    # 두 방향 모두 올라온 가장 최근 기간
    order = {"10": 0, "20": 1, "말": 2}
    keys = None
    for d in series:
        k = {(r["ym"], r["p"]) for r in series[d]["rows"] if r["p"] in order}
        keys = k if keys is None else keys & k
    if not keys:
        print("ERROR: 공통 기간 없음", file=sys.stderr); sys.exit(1)
    ym, p = max(keys, key=lambda k: (k[0], order[k[1]]))
    latest = {"ym": ym, "p": p}
    print(f"latest: {latest}", file=sys.stderr)

    if not a.force and old.get("latest") == latest:
        print("NO_NEW_PERIOD", file=sys.stderr)
        sys.exit(2)

    sig, days = signals(series, latest)
    baseline = {d: series[d].pop("_baseline") for d in series}
    gen = today.isoformat()
    hist = [h for h in old.get("history", []) if (h.get("ym"), h.get("p")) != (ym, p)]
    hist.append({"ym": ym, "p": p, "generated": gen,
                 "flags": [{"dir": x["dir"], "item": x["item"], "cur": x["cur"], "mom": x["mom"], "yoy": x["yoy"]}
                           for x in sig if x["flag"]]})
    hist = hist[-36:]
    out = {"generated": gen, "series": series, "latest": latest, "signals": sig,
           "baseline": baseline, "days": days, "history": hist}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    flags = [f"{x['dir']} {x['item']} mom={x['mom']} yoy={x['yoy']}" for x in sig if x["flag"]]
    print("flags:", *flags, sep="\n  ", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
