#!/usr/bin/env python3
"""대시보드와 작업 파일을 GitHub 저장소에 올린다 (GitHub Pages 사이트 갱신 + 백업).

사용:  python3 publish.py [--repo haileyaiproject/tradedata] [--message "..."] [--token <PAT>]

Claude 클라우드 세션에서는 github.com 으로 가는 git 요청에 세션에 연결된 저장소의 자격 증명이 자동으로 붙는다
(저장소가 세션에 연결되어 있어야 하며, 직접 넣은 토큰은 무시된다). --token 은 그 밖의 환경에서만 의미가 있다.

올리는 것: index.html(사이트), src/*.py·template.html(스크립트), data/*.json(데이터).
종료코드: 0 성공(또는 바뀐 것 없음) / 6 저장소 접근 거부(세션에 저장소가 연결되지 않음, 토큰 만료 등) / 5 네트워크 / 1 그 밖의 오류
"""
import argparse, datetime as dt, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = ["early.py", "collect.py", "shares.py", "build.py", "publish.py", "template.html"]
DATA = ["analysis.json", "sgg_latest.json", "shares.json", "names.json", "companies.json", "early.json"]


def run(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).replace(cmd[-1] if "@github.com" in cmd[-1] else "\0", "<url>"))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="")
    ap.add_argument("--repo", default="haileyaiproject/tradedata")
    ap.add_argument("--message", default="")
    a = ap.parse_args()
    url = f"https://x-access-token:{a.token}@github.com/{a.repo}.git" if a.token else f"https://github.com/{a.repo}.git"
    work = tempfile.mkdtemp(prefix="ghpages-")
    env = {**os.environ, "GH_TOKEN": "", "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(["git", "-c", "credential.helper=", "clone", "-q", "--depth", "1", url, work], capture_output=True, text=True, env=env)
        if r.returncode:
            err = (r.stderr or "").replace(a.token or "\0", "<token>")
            if "not in this session" in err or "access denied by the git proxy" in err or "Authentication failed" in err or "403" in err or "401" in err or "could not read Username" in err:
                print("TOKEN_REJECTED: " + err.strip()[:300], file=sys.stderr); sys.exit(6)
            if "Could not resolve" in err or "Connection" in err or "timed out" in err:
                print("NETWORK: " + err.strip()[:300], file=sys.stderr); sys.exit(5)
            print("ERROR: " + err.strip()[:300], file=sys.stderr); sys.exit(1)
        os.makedirs(os.path.join(work, "src"), exist_ok=True)
        os.makedirs(os.path.join(work, "data"), exist_ok=True)
        shutil.copy(os.path.join(HERE, "index.html"), os.path.join(work, "index.html"))
        open(os.path.join(work, ".nojekyll"), "w").close()
        for f in SRC:
            p = os.path.join(HERE, f)
            if os.path.exists(p):
                shutil.copy(p, os.path.join(work, "src", f))
        for f in DATA:
            p = os.path.join(HERE, f)
            if os.path.exists(p):
                shutil.copy(p, os.path.join(work, "data", f))
        readme = os.path.join(work, "README.md")
        if not os.path.exists(readme):
            open(readme, "w", encoding="utf-8").write(
                "# 전국 수출입 급변 품목 대시보드\n\n관세청 공공데이터로 만든 대시보드입니다. `index.html` 이 사이트이고, "
                "`src/` 는 수집·분석 스크립트, `data/` 는 결과 데이터입니다. Claude 예약 작업이 자동으로 갱신합니다.\n")
        run(["git", "config", "user.email", "claude-routine@users.noreply.github.com"], work)
        run(["git", "config", "user.name", "Claude routine"], work)
        run(["git", "add", "-A"], work)
        st = run(["git", "status", "--porcelain"], work).stdout.strip()
        if not st:
            print("NO_CHANGES", file=sys.stderr); sys.exit(0)
        msg = a.message or f"update {dt.date.today().isoformat()}"
        run(["git", "commit", "-q", "-m", msg], work)
        r = subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=work, capture_output=True, text=True, env=env)
        if r.returncode:
            err = (r.stderr or "").replace(a.token or "\0", "<token>")
            if "not in this session" in err or "access denied by the git proxy" in err or "403" in err or "401" in err or "Authentication failed" in err or "Permission" in err:
                print("TOKEN_REJECTED: " + err.strip()[:300], file=sys.stderr); sys.exit(6)
            print("ERROR: push " + err.strip()[:300], file=sys.stderr); sys.exit(1)
        print(f"pushed: {len(st.splitlines())} files changed → https://{a.repo.split('/')[0]}.github.io/{a.repo.split('/')[1]}/", file=sys.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
