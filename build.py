#!/usr/bin/env python3
"""analysis.json + names.json + companies.json + early.json + shares.json → korea.html

template.html 의 /*__DATA__*/ 자리에 `const D = {...};` 를 끼워 넣는다.
Artifact 도구가 게시할 때 <html>/<head>/<body> 를 씌우므로 여기서는 본문만 만든다.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, default):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    D = load("analysis.json", None)
    if D is None:
        sys.exit("analysis.json 이 없습니다")
    D["companies"] = load("companies.json", [])
    D["names"] = load("names.json", {})
    early = load("early.json", None)
    if early and early.get("latest"):
        # 페이지에는 표에 필요한 것만 (rows 전체는 빼서 용량 절약)
        D["early"] = {k: early[k] for k in ("generated", "latest", "signals", "baseline", "days", "history") if k in early}
        D["early"]["series"] = {d: {"names": s["names"], "raw_dt_samples": s.get("raw_dt_samples", [])}
                                for d, s in early.get("series", {}).items()}
    shares = load("shares.json", None)
    if shares:
        D["shares"] = shares
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    data = "const D = " + json.dumps(D, ensure_ascii=False, separators=(",", ":")) + ";"
    html = tpl.replace("/*__DATA__*/", data, 1)
    out = os.path.join(HERE, "korea.html")
    open(out, "w", encoding="utf-8").write(html)
    # GitHub Pages 용 완전한 HTML 문서 (artifact 는 게시 때 껍데기를 씌워 주지만, 일반 웹서버는 아니므로)
    full = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="robots" content="noindex"><title>전국 수출입 급변 품목</title>'
            '<style>:root{color-scheme:light}body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#000}</style>'
            '</head><body>\n' + html + '\n</body></html>\n')
    open(os.path.join(HERE, "index.html"), "w", encoding="utf-8").write(full)
    print(f"korea.html {len(html):,} bytes, index.html {len(full):,} bytes")


if __name__ == "__main__":
    main()
