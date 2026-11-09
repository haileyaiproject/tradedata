#!/usr/bin/env python3
"""조기 신호 품목(반도체·승용차 …)이 어느 시도에서 나오는지 비중 계산 → shares.json

사용:  python3 shares.py --key <인증키>
호출: 시도마다 A API(시도별 품목별) 5번(2자리 전체 + 27·84·85·87류 아래 4자리) × 16개 시도 ≈ 80회. 최근 확정월(analysis.json 의 end) 한 달 기준.
시군구 비중("경기 안: 평택시 36% …")은 collect.py 가 남긴 sgg_latest.json 에서 계산한다(없으면 이전 값 유지).
"""
import argparse, datetime as dt, json, os, sys, time
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
A_URL = "https://apis.data.go.kr/1220000/sidoitemtrade/getSidoitemtradeList"
OUT = os.path.join(HERE, "shares.json")

MAP = {
    "exp": {"반도체": ["8541", "8542"], "철강제품": ["72", "73"], "승용차": ["8703"], "석유제품": ["2710"], "무선통신기기": ["8517"],
            "선박": ["89"], "자동차부품": ["8708"], "컴퓨터주변기기": ["8471", "8473"], "정밀기기": ["90"],
            "가전제품": ["8415", "8418", "8450", "8508", "8509", "8516", "8528"]},
    "imp": {"반도체": ["8541", "8542"], "원유": ["2709"], "기계류": ["84"], "가스": ["2711"], "반도체 제조용 장비": ["8486"], "정밀기기": ["90"],
            "석유제품": ["2710"], "무선통신기기": ["8517"], "승용차": ["8703"], "석탄": ["2701"]},
}
SIDO = [("41", "경기", ["41"]), ("44", "충남", ["44"]), ("31", "울산", ["31"]), ("11", "서울", ["11"]), ("12", "전남광주", ["12"]),
        ("28", "인천", ["28"]), ("43", "충북", ["43"]), ("48", "경남", ["48"]), ("47", "경북", ["47"]), ("26", "부산", ["26"]),
        ("27", "대구", ["27"]), ("52", "전북", ["52"]), ("30", "대전", ["30"]), ("51", "강원", ["51"]), ("36", "세종", ["36"]), ("50", "제주", ["50"])]
calls = 0


def num(s):
    s = (s or "").replace(",", "").strip()
    return float(s) if s else 0.0


def get(key, sido, ym, hs=None):
    global calls
    p = {"serviceKey": key, "strtYymm": ym, "endYymm": ym, "sidoCd": sido}
    if hs:
        p["hsSgn"] = hs
    req = urllib.request.Request(A_URL + "?" + urllib.parse.urlencode(p), headers={"User-Agent": "trade-shares/1.0"})
    for attempt in range(5):
        try:
            calls += 1
            with urllib.request.urlopen(req, timeout=90) as r:
                text = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if "connect_rejected" in text:
                print("NETWORK_BLOCKED", file=sys.stderr); sys.exit(5)
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        print("NETWORK_BLOCKED", file=sys.stderr); sys.exit(5)
    if "NOT_REGISTERED" in text or "<returnReasonCode>30<" in text:
        print("KEY_NOT_REGISTERED", file=sys.stderr); sys.exit(4)
    root = ET.fromstring(text)
    out = {}
    for it in root.findall(".//item"):
        d = {x.tag: (x.text or "").strip() for x in it}
        if d.get("hsSgn"):
            out[d["hsSgn"]] = (num(d.get("expUsdAmt")), num(d.get("impUsdAmt")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--end", default="")
    a = ap.parse_args()
    end = a.end
    if not end:
        try:
            end = json.load(open(os.path.join(HERE, "analysis.json"), encoding="utf-8"))["end"]
        except Exception:
            sys.exit("analysis.json 에서 기준월을 읽지 못했습니다. --end YYYYMM 을 주세요")
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = {}
    if old.get("end") == end and not a.end:
        print("ALREADY_DONE", file=sys.stderr)
        sys.exit(0)

    # 필요한 2자리 류와, 4자리가 필요한 류
    need4 = sorted({p[:2] for d in MAP.values() for ps in d.values() for p in ps if len(p) == 4})
    amounts = {}  # sido -> prefix -> (exp, imp)
    for code, name, qcodes in SIDO:
        acc = {}
        for sc in qcodes:
            ch = get(a.key, sc, end)
            for h, v in ch.items():
                acc[h] = (acc.get(h, (0, 0))[0] + v[0], acc.get(h, (0, 0))[1] + v[1])
            for c in need4:
                for h, v in get(a.key, sc, end, c).items():
                    acc[h] = (acc.get(h, (0, 0))[0] + v[0], acc.get(h, (0, 0))[1] + v[1])
        amounts[code] = acc
        print(f"{name}: {len(acc)} prefixes, calls {calls}", file=sys.stderr)

    sgg_latest = {}
    p = os.path.join(HERE, "sgg_latest.json")
    if os.path.exists(p):
        try:
            sl = json.load(open(p, encoding="utf-8"))
            if sl.get("end") == end:
                sgg_latest = sl.get("regions", {})
        except Exception:
            pass

    out = {"end": end, "generated": dt.date.today().isoformat(), "calls": calls, "map": MAP}
    di = {"exp": 0, "imp": 1}
    for d, items in MAP.items():
        out[d] = {}
        for item, prefixes in items.items():
            by = []
            for code, name, _ in SIDO:
                amt = sum(amounts[code].get(pf, (0, 0))[di[d]] for pf in prefixes)
                by.append((name, amt))
            total = sum(x[1] for x in by)
            by.sort(key=lambda x: -x[1])
            e = {"total": round(total),
                 "sido": [{"name": n, "amt": round(v), "share": round(v / total, 3) if total else 0} for n, v in by]}
            # 경기 안 시군구 비중 (collect.py 가 남긴 최근월 HS6×시군구 금액에서)
            gg = sgg_latest.get("41")
            if gg:
                acc = {}
                for hs, sggs in gg.items():
                    if any(hs.startswith(pf) for pf in prefixes):
                        for sgg, v in sggs.items():
                            acc[sgg] = acc.get(sgg, 0) + v[di[d]]
                t2 = sum(acc.values())
                if t2:
                    e["sgg"] = [{"name": k, "amt": round(v), "share": round(v / t2, 3)}
                                for k, v in sorted(acc.items(), key=lambda kv: -kv[1])[:8]]
            if "sgg" not in e:
                prev = (old.get(d) or {}).get(item, {})
                if prev.get("sgg"):
                    e["sgg"] = prev["sgg"]
            out[d][item] = e
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"shares.json 저장, 호출 {calls}", file=sys.stderr)


if __name__ == "__main__":
    main()
