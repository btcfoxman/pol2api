"""Read an existing authorized Chrome session via native CDP; never launch/navigate it."""

import json
from urllib.request import build_opener, ProxyHandler
import websocket


def read_session(port: int) -> dict:
    if not 1024 <= int(port) <= 65535:
        raise ValueError("无效 CDP 端口")
    opener = build_opener(ProxyHandler({}))
    with opener.open(
        f"http://127.0.0.1:{int(port)}/json/version", timeout=5
    ) as response:
        info = json.load(response)
    ws = websocket.create_connection(
        info["webSocketDebuggerUrl"],
        timeout=10,
        suppress_origin=True,
        http_no_proxy=["127.0.0.1", "localhost"],
    )
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            reply = json.loads(ws.recv())
            if reply.get("id") == 1:
                break
        if "error" in reply:
            raise ValueError("CDP 无法读取 Cookie")
        records = [
            x
            for x in reply["result"]["cookies"]
            if x.get("domain", "").lstrip(".") == "pollo.ai"
            and x.get("path", "/") == "/"
        ]
        if not any(
            x["name"].startswith("__Secure-next-auth.session-token") for x in records
        ):
            raise ValueError("浏览器中没有 Pollo 登录会话，请先手动登录")
        return {
            "cookie_records": records,
            "cookie_header": "; ".join(f"{x['name']}={x['value']}" for x in records),
            "user_agent": info.get("User-Agent", ""),
        }
    finally:
        ws.close()
