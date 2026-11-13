"""走 GitHub Contents API 提交文件——`git push` 被代理掐断时的备用通道。

适用场景：代理对 github.com:443 的 CONNECT 隧道不稳（502 / schannel / sideband 断开），
但 api.github.com 仍可访问。此时逐文件更新，绕过 git 的 pack 传输。

用法:
    set GH_TOKEN=ghp_xxx            (PowerShell: $env:GH_TOKEN="ghp_xxx")
    python scripts/push_via_api.py -m "提交说明" 证据/站点二_config.json [更多文件...]

注意:
  - 走 API 产生的 commit 与本地 commit sha 不同。推完后本地需
    `git fetch origin && git reset --hard origin/main` 对齐，否则下次 push 会分叉。
  - 只适合少量小文件；大文件或批量变更还是应该用 git push。
"""
import argparse
import base64
import json
import os
import sys
import time

import requests

API = "https://api.github.com"


def _req(sess, method, url, token, **kw):
    """带重试的 API 调用（代理不稳时 502/超时是常态）。"""
    last = None
    for i in range(1, 5):
        try:
            r = sess.request(method, url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            }, timeout=40, **kw)
            if r.status_code < 500:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        print(f"  ! 第 {i} 次失败（{last}），重试…")
        time.sleep(3 * i)
    raise SystemExit(f"[FAIL] {method} {url} 连续失败：{last}")


def put_file(sess, token, repo, branch, path, message):
    api = f"{API}/repos/{repo}/contents/{requests.utils.quote(path)}"

    r = _req(sess, "GET", f"{api}?ref={branch}", token)
    if r.status_code == 404:
        sha = None
        print(f"  {path}: 远端不存在，将新建")
    elif r.status_code == 200:
        sha = r.json()["sha"]
    else:
        raise SystemExit(f"[FAIL] 读取 {path} 失败：HTTP {r.status_code} {r.text[:200]}")

    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    body = {"message": message, "content": content, "branch": branch}
    if sha:
        body["sha"] = sha

    r = _req(sess, "PUT", api, token, data=json.dumps(body))
    if r.status_code not in (200, 201):
        raise SystemExit(f"[FAIL] 写入 {path} 失败：HTTP {r.status_code} {r.text[:300]}")
    d = r.json()
    print(f"  ✅ {path} → commit {d['commit']['sha'][:7]}")
    return d["commit"]["sha"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("--repo", default="BRFG/etf-tracker")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--max-files", type=int, default=10,
                    help="单次最多几个文件（每个文件一个 commit，太多会污染历史）")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("缺少环境变量 GH_TOKEN")

    if len(args.files) > args.max_files:
        raise SystemExit(f"文件数 {len(args.files)} 超过上限 {args.max_files}，"
                         "批量变更请用 git push")

    sess = requests.Session()
    print(f"目标仓库 {args.repo}@{args.branch}，{len(args.files)} 个文件")
    for i, path in enumerate(args.files):
        if not os.path.isfile(path):
            raise SystemExit(f"[FAIL] 文件不存在：{path}")
        msg = args.message if len(args.files) == 1 else f"{args.message} ({i+1}/{len(args.files)})"
        put_file(sess, token, args.repo, args.branch, path.replace("\\", "/"), msg)

    print("\n完成。本地对齐远端：")
    print("  git fetch origin && git reset --hard origin/main")


if __name__ == "__main__":
    sys.exit(main())
