#!/usr/bin/env python3
"""전국 16개 시도 × HS 6단위 × 시군구 수출입실적 수집기 → analysis.json

사용:  python3 collect.py --key <인증키> [--budget 8500] [--regions 41,11] [--workers 4]

관세청 공공데이터 API 두 가지 (XML):
  A. 시도별 품목별 수출입실적:  https://apis.data.go.kr/1220000/sidoitemtrade/getSidoitemtradeList
       serviceKey, strtYymm, endYymm, sidoCd, [hsSgn]  → hsSgn 아래 한 단계(2→4→6자리) 품목별 합계. 연도가 바뀌면 연도별 행으로 나뉨.
  B. 시군구별 품목별 수출입실적: https://apis.data.go.kr/1220000/sigunguperprlstperacrs/getSigunguPerPrlstPerAcrs
       serviceKey, strtYymm, endYymm, HsSgn(6자리), sidoCd → 시군구 × 월 행. 조회기간은 최대 1년.

방법: 시도마다 A로 2→4→6자리로 내려가며(연간 수출 또는 수입 1천만 달러 미만 가지는 더 내려가지 않음) 분석할 HS6 코드를 고르고,
      코드마다 B로 12개월치 시군구별 금액을 받는다. 금액 단위는 모두 천 달러.
튄 품목: 최근월이 직전월의 2배 이상 또는 0.5배 이하, 두 달 차이 500만 달러 이상, 두 달 중 하나가 1천만 달러 이상.

종료코드: 0 전부 완료(또는 이미 완료) / 3 예산 소진으로 중간 저장(다음 실행에서 이어감) / 4 인증키 미등록 / 5 네트워크 차단 / 1 그 밖의 오류
"""
import argparse, datetime as dt, json, os, re, sys, time, threading, concurrent.futures as cf
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "analysis.json")
SGG_OUT = os.path.join(HERE, "sgg_latest.json")
CACHE = os.path.join(HERE, "cache")
A_URL = "https://apis.data.go.kr/1220000/sidoitemtrade/getSidoitemtradeList"
B_URL = "https://apis.data.go.kr/1220000/sigunguperprlstperacrs/getSigunguPerPrlstPerAcrs"

THRESH = 10000     # 연간 수출 또는 수입 1천만 달러(천 달러 단위) 이상인 가지만 내려감
SIG_RATIO = 2.0
SIG_DELTA = 5000
SIG_LEVEL = 10000
BIG_N = 40
SGG_KEEP = 8
SGG_MIN_LATEST = 1000   # sgg_latest.json 에 남길 최근월 금액 하한(천 달러)

# 시도: (코드, 이름, 실제 조회에 쓸 코드들). 2026.7.1 전남·광주 통합(12): 그 전 달은 옛 코드 46(전남)·29(광주)로만 조회된다.
REGIONS = [
    ("41", "경기도", ["41"]), ("44", "충청남도", ["44"]), ("31", "울산광역시", ["31"]), ("11", "서울특별시", ["11"]),
    ("12", "전남광주통합특별시", ["12", "46", "29"]), ("28", "인천광역시", ["28"]), ("43", "충청북도", ["43"]),
    ("48", "경상남도", ["48"]), ("47", "경상북도", ["47"]), ("26", "부산광역시", ["26"]), ("27", "대구광역시", ["27"]),
    ("52", "전북특별자치도", ["52"]), ("30", "대전광역시", ["30"]), ("51", "강원특별자치도", ["51"]),
    ("36", "세종특별자치시", ["36"]), ("50", "제주특별자치도", ["50"]),
]

_lock = threading.Lock()
calls = 0
budget = 10**9
budget_hit = False


class Blocked(Exception):
    pass


def num(s):
    s = (s or "").replace(",", "").strip()
    return float(s) if s else 0.0


def shift_month(ym, k):
    y, m = int(ym[:4]), int(ym[4:])
    m += k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y}{m:02d}"


def http(url, params):
    """캐시 → 없으면 호출. 연결 끊김은 4번까지 재시도. 예산을 넘기면 None."""
    global calls, budget_hit
    os.makedirs(CACHE, exist_ok=True)
    key = re.sub(r"[^0-9A-Za-z_]", "_", "_".join(f"{k}{v}" for k, v in sorted(params.items()) if k != "serviceKey"))
    name = ("A" if url == A_URL else "B") + "_" + key + ".xml"
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        return open(path, "rb").read()
    with _lock:
        if calls >= budget:
            budget_hit = True
            return None
        calls += 1
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "trade-collect/1.0"})
    body = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read()
            break
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 403 and b"connect_rejected" in body:
                raise Blocked("NETWORK_BLOCKED 403 connect_rejected")
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            time.sleep(2 * (attempt + 1))
    if body is None:
        raise Blocked("NETWORK_BLOCKED connection failed repeatedly")
    text = body.decode("utf-8", "replace")
    if "connect_rejected" in text:
        raise Blocked("NETWORK_BLOCKED 403 connect_rejected")
    if "SERVICE_KEY_IS_NOT_REGISTERED" in text or "<returnReasonCode>30<" in text:
        print("KEY_NOT_REGISTERED", file=sys.stderr)
        sys.exit(4)
    if "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS" in text or "<returnReasonCode>22<" in text:
        with _lock:
            budget_hit = True
        return None
    if "<returnReasonCode>" in text:
        raise RuntimeError("gateway error: " + text[:200])
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise RuntimeError("XML parse error: " + text[:200])
    code = root.findtext(".//resultCode")
    if code not in ("00", "0"):
        raise RuntimeError(f"resultCode {code}: {root.findtext('.//resultMsg')}")
    open(path, "wb").write(body)
    return body


def items(body):
    root = ET.fromstring(body)
    for it in root.findall(".//item"):
        yield {x.tag: (x.text or "").strip() for x in it}


def a_children(key, sido_codes, strt, end, hs=None):
    """A API: hs 아래 한 단계 품목들의 기간 합계 {hs: {'exp','imp','name'}} (여러 시도 코드·연도 행을 합침). 예산 소진이면 None."""
    agg = {}
    for sc in sido_codes:
        p = {"serviceKey": key, "strtYymm": strt, "endYymm": end, "sidoCd": sc}
        if hs:
            p["hsSgn"] = hs
        body = http(A_URL, p)
        if body is None:
            return None
        for d in items(body):
            h = d.get("hsSgn")
            if not h:
                continue
            e = agg.setdefault(h, {"exp": 0.0, "imp": 0.0, "name": ""})
            e["exp"] += num(d.get("expUsdAmt")); e["imp"] += num(d.get("impUsdAmt"))
            if d.get("korePrlstNm") and not e["name"]:
                e["name"] = d["korePrlstNm"]
    return agg


def b_series(key, sido_codes, months, hs):
    """B API: {sggName: {'exp':[12], 'imp':[12]}} 와 품목 이름. 예산 소진이면 None."""
    idx = {m: i for i, m in enumerate(months)}
    out, name = {}, ""
    for sc in sido_codes:
        body = http(B_URL, {"serviceKey": key, "strtYymm": months[0], "endYymm": months[-1], "HsSgn": hs, "sidoCd": sc})
        if body is None:
            return None, None
        for d in items(body):
            ym = d.get("priodTitle", "").replace(".", "")
            if ym not in idx:
                continue
            sgg = d.get("sggNm", "").strip()
            sgg = sgg.split(" ", 1)[1] if " " in sgg else sgg      # "경기도 이천시" → "이천시"
            s = out.setdefault(sgg, {"exp": [0.0] * len(months), "imp": [0.0] * len(months)})
            s["exp"][idx[ym]] += num(d.get("expUsdAmt")); s["imp"][idx[ym]] += num(d.get("impUsdAmt"))
            if d.get("korePrlstNm") and not name:
                name = d["korePrlstNm"]
    return out, name


def find_end(key, today):
    """가장 최근에 공개된 달: 이번 달부터 거꾸로 경기도 총계가 0보다 큰 첫 달."""
    ym = today.strftime("%Y%m")
    for _ in range(4):
        body = http(A_URL, {"serviceKey": key, "strtYymm": ym, "endYymm": ym, "sidoCd": "41"})
        if body is None:
            break
        tot = [d for d in items(body) if d.get("priodTitle") == "총계"]
        if tot and num(tot[0].get("expUsdAmt")) > 0:
            return ym
        ym = shift_month(ym, -1)
    return None


def _pmap(fn, keys, workers):
    """keys 각각에 fn 을 병렬 적용 → {key: result}. 하나라도 None(예산 소진)이면 None."""
    out = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, k): k for k in keys}
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
    if any(v is None for v in out.values()):
        return None
    return out


def drill(key, codes, strt, end, workers=4):
    """2→4→6자리로 내려가 분석 대상 HS6 {hs: {'name','full','exp','imp'}} 와 본 HS6 수. 예산 소진 시 (None, 0)."""
    ch = a_children(key, codes, strt, end)
    if ch is None:
        return None, 0
    chapters = [c for c, v in sorted(ch.items()) if len(c) == 2 and max(v["exp"], v["imp"]) >= THRESH]
    hd_by_ch = _pmap(lambda c: a_children(key, codes, strt, end, c), chapters, workers)
    if hd_by_ch is None:
        return None, 0
    headings = []  # (chapter, heading)
    for c in chapters:
        for h, v in sorted(hd_by_ch[c].items()):
            if len(h) == 4 and max(v["exp"], v["imp"]) >= THRESH:
                headings.append((c, h))
    sub_by_hd = _pmap(lambda ch_h: a_children(key, codes, strt, end, ch_h[1]), headings, workers)
    if sub_by_hd is None:
        return None, 0
    picked, seen6 = {}, 0
    for c, h in headings:
        cv, hv = ch[c], hd_by_ch[c][h]
        for s, sv in sorted(sub_by_hd[(c, h)].items()):
            if len(s) != 6:
                continue
            seen6 += 1
            if max(sv["exp"], sv["imp"]) >= THRESH:
                picked[s] = {"name": sv["name"] or None,
                             "full": " › ".join(x for x in (cv["name"], hv["name"], sv["name"]) if x),
                             "exp": sv["exp"], "imp": sv["imp"]}
    return picked, seen6


def analyze(code, name, months, picked, series_by_hs):
    n = len(months)
    tot = {"exp": [0.0] * n, "imp": [0.0] * n}
    sig, cand = [], []
    latest_sgg = {}
    for hs, (sggs, _nm) in series_by_hs.items():
        meta = picked[hs]
        for d in ("exp", "imp"):
            ser = [0.0] * n
            for s in sggs.values():
                for i in range(n):
                    ser[i] += s[d][i]
            for i in range(n):
                tot[d][i] += ser[i]
            last, prev = ser[-1], ser[-2]
            avg = sum(ser[:-1]) / (n - 1)
            delta = last - prev
            ratio = (last / prev) if prev else (99.0 if last else 1.0)
            ranked = sorted(sggs.items(), key=lambda kv: -abs(kv[1][d][-1] - kv[1][d][-2]))
            row = {"hs": hs, "name": meta["name"], "full": meta["full"], "dir": d, "last": round(last), "prev": round(prev),
                   "avg": round(avg), "delta": round(delta), "ratio": round(min(ratio, 99.0), 2)}
            is_sig = (ratio >= SIG_RATIO or ratio <= 1 / SIG_RATIO) and abs(delta) >= SIG_DELTA and max(last, prev) >= SIG_LEVEL
            if is_sig:
                row["series"] = [round(v) for v in ser]
                row["allSgg"] = [{"sgg": k, "series": [round(v) for v in v2[d]]} for k, v2 in ranked[:SGG_KEEP]]
                sig.append(row)
            else:
                row["top"] = [k for k, _ in ranked[:3]]
                cand.append(row)
            if d == "exp":
                keep = {k: [round(v["exp"][-1]), round(v["imp"][-1])] for k, v in sggs.items()
                        if max(v["exp"][-1], v["imp"][-1]) >= SGG_MIN_LATEST}
                if keep:
                    latest_sgg[hs] = keep
    sig.sort(key=lambda r: -abs(r["delta"]))
    cand.sort(key=lambda r: -abs(r["delta"]))
    region = {"code": code, "name": name, "gg_total": {d: [round(v) for v in tot[d]] for d in tot},
              "n_codes": len(series_by_hs), "hs6count": 0, "sig": sig, "big": cand[:BIG_N]}
    return region, latest_sgg


def main():
    global budget
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--budget", type=int, default=8500)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--regions", default="", help="쉼표로 구분한 시도 코드. 비우면 전체 16개")
    ap.add_argument("--end", default="", help="기준월 YYYYMM (비우면 자동)")
    a = ap.parse_args()
    budget = a.budget
    key = a.key
    today = dt.date.today()

    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = {}
    sgg_latest = {}
    if os.path.exists(SGG_OUT):
        try:
            sgg_latest = json.load(open(SGG_OUT, encoding="utf-8"))
        except Exception:
            sgg_latest = {}

    try:
        end = a.end or find_end(key, today)
    except Blocked as e:
        print(e, file=sys.stderr); sys.exit(5)
    if not end:
        print("ERROR: 최근 달을 찾지 못함", file=sys.stderr); sys.exit(1)
    months = [shift_month(end, -11 + i) for i in range(12)]
    want = [r for r in REGIONS if not a.regions or r[0] in a.regions.split(",")]
    # 2026.7 통합 전 달이 조회 범위에 없으면 옛 코드(46 전남·29 광주)는 호출할 필요가 없다
    if months[0] >= "202607":
        want = [(c, n, [q for q in qs if q not in ("46", "29")] or qs) for c, n, qs in want]
    print(f"end={end} months={months[0]}..{months[-1]} regions={[r[0] for r in want]}", file=sys.stderr)

    # 같은 달에 이미 완료된 시도는 그대로 둔다
    done = {}
    if old.get("end") == end:
        done = {r["code"]: r for r in old.get("regions", [])}
    else:
        sgg_latest = {}
    if all(r[0] in done for r in want) and old.get("end") == end:
        print("ALREADY_COMPLETE", file=sys.stderr)
        sys.exit(0)

    history = [h for h in old.get("history", []) if h.get("end") != end]
    hist_now = next((h for h in old.get("history", []) if h.get("end") == end), None) or {"end": end, "generated": today.isoformat(), "regions": []}
    errors = []
    exit_code = 0

    def save(complete, pending):
        regions = [done[r[0]] for r in REGIONS if r[0] in done]
        hist_now["generated"] = today.isoformat()
        hist_now["regions"] = [{"name": r["name"], "n_codes": r["n_codes"], "exp_total": r["gg_total"]["exp"][-1],
                                "imp_total": r["gg_total"]["imp"][-1],
                                "sig": [{k: s[k] for k in ("hs", "name", "dir", "last", "prev", "avg", "delta", "ratio")} |
                                        {"top": s["allSgg"][0]["sgg"] if s["allSgg"] else ""} for s in r["sig"]]}
                               for r in regions]
        out = {"months": [m[:4] + "." + m[4:] for m in months], "end": end, "generated": today.isoformat(), "calls": calls,
               "errors": errors, "complete": complete, "pending": pending, "regions": regions,
               "history": history + [hist_now]}
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        json.dump({"end": end, "regions": sgg_latest}, open(SGG_OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
        json.dump(out, open(os.path.join(HERE, "data", f"{end}.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    try:
        for code, name, qcodes in want:
            if code in done:
                continue
            t0 = time.time()
            picked, seen6 = drill(key, qcodes, months[0], months[-1], a.workers)
            if picked is None:
                break
            print(f"[{name}] HS6 {len(picked)}개 (본 코드 {seen6}개), 호출 {calls}", file=sys.stderr)
            series_by_hs = {}
            stop = False
            with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
                futs = {ex.submit(b_series, key, qcodes, months, hs): hs for hs in sorted(picked)}
                for f in cf.as_completed(futs):
                    hs = futs[f]
                    try:
                        sggs, nm = f.result()
                    except Blocked:
                        raise
                    except Exception as e:
                        errors.append(f"{code} {hs}: {e}"); continue
                    if sggs is None:
                        stop = True; continue
                    if nm and not picked[hs]["name"]:
                        picked[hs]["name"] = nm
                    series_by_hs[hs] = (sggs, nm)
            if stop or budget_hit:
                print(f"[{name}] 예산 소진: {len(series_by_hs)}/{len(picked)} 코드까지 받음 (캐시에 저장됨)", file=sys.stderr)
                break
            region, latest = analyze(code, name, months, picked, series_by_hs)
            region["hs6count"] = seen6
            done[code] = region
            sgg_latest[code] = latest
            print(f"[{name}] 완료: 튄 품목 {len(region['sig'])}개, 호출 누계 {calls}, {time.time() - t0:.0f}s", file=sys.stderr)
            save(False, [r[1] for r in REGIONS if r[0] not in done])
    except Blocked as e:
        print(e, file=sys.stderr)
        save(False, [r[1] for r in REGIONS if r[0] not in done])
        sys.exit(5)

    pending = [r[1] for r in REGIONS if r[0] not in done]
    complete = not pending
    save(complete, pending)
    print(f"calls={calls} complete={complete} pending={pending} errors={len(errors)}", file=sys.stderr)
    sys.exit(0 if complete or (a.regions and all(r[0] in done for r in want)) else 3)


if __name__ == "__main__":
    main()
