#!/usr/bin/env python3
import json
import logging
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.DEBUG)

import os as _os

# token 从环境变量读（审计 P3）：硬编码占位模板会诱导把真 token 填进来随仓库提交
_GITHUB_TOKEN = _os.environ.get("GITHUB_TOKEN", "").strip()
headers = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "GameAuto-Leak-Searcher/1.0",
    **({"Authorization": f"token {_GITHUB_TOKEN}"} if _GITHUB_TOKEN else {}),
}

url = "https://api.github.com/search/code?q=sk-&per_page=5"

print(f"请求: {url}")
request = Request(url, headers=headers)

try:
    with urlopen(request, timeout=30) as response:
        print(f"状态码: {response.status}")
        print(f"响应头: {dict(response.headers)}")
        data = json.loads(response.read().decode("utf-8"))
        print(f"\n响应数据:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"错误: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
