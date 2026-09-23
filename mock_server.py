# -*- coding: utf-8 -*-
"""本地模拟接口，用于测试悬浮窗（无需真实中转站）"""
import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8899
TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(TZ)
TODAY0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

# 模拟调用日志：今天 2 条 deepseek + 1 条 glm，昨天 1 条 kimi
LOG_ITEMS = [
    {"id": 1, "user_id": 1, "created_at": int((TODAY0 + datetime.timedelta(hours=10)).timestamp()),
     "username": "mock", "token_name": "t", "model_name": "deepseek-v4.1-flash",
     "quota": 1000000, "prompt_tokens": 1000, "completion_tokens": 100,
     "use_time": 3, "is_stream": True, "channel": 1, "token_id": 1,
     "group": "g", "ip": "1.1.1.1", "request_id": "r1"},
    {"id": 2, "user_id": 1, "created_at": int((TODAY0 + datetime.timedelta(hours=11)).timestamp()),
     "username": "mock", "token_name": "t", "model_name": "deepseek-v4.1-flash",
     "quota": 500000, "prompt_tokens": 500, "completion_tokens": 50,
     "use_time": 2, "is_stream": True, "channel": 1, "token_id": 1,
     "group": "g", "ip": "1.1.1.1", "request_id": "r2"},
    {"id": 3, "user_id": 1, "created_at": int((TODAY0 + datetime.timedelta(hours=12)).timestamp()),
     "username": "mock", "token_name": "t", "model_name": "glm-5.3",
     "quota": 200000, "prompt_tokens": 200, "completion_tokens": 20,
     "use_time": 1, "is_stream": True, "channel": 2, "token_id": 1,
     "group": "g", "ip": "1.1.1.1", "request_id": "r3"},
    {"id": 4, "user_id": 1, "created_at": int((TODAY0 - datetime.timedelta(hours=1)).timestamp()),
     "username": "mock", "token_name": "t", "model_name": "kimi-k3",
     "quota": 300000, "prompt_tokens": 300, "completion_tokens": 30,
     "use_time": 2, "is_stream": True, "channel": 3, "token_id": 1,
     "group": "g", "ip": "1.1.1.1", "request_id": "r4"},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/log/token"):
            body = self._logs()
        elif self.path.startswith("/api/usage/token"):
            body = json.dumps({"code": True, "data": {
                "amount_unit": "USD", "quota_per_unit": 500000, "name": "t",
                "total_available": 1000000, "total_used": 1700000,
                "total_granted": 2700000, "unlimited_quota": False, "expires_at": 0}}).encode("utf-8")
        elif self.path.startswith("/api/user/self"):
            body = json.dumps({"success": True, "data": {
                "quota": 1000000, "used_quota": 1700000, "request_count": 4}}).encode("utf-8")
        elif self.path == "/":
            body = ("<html><body><div>账户余额：¥35.00</div><div>已用：¥15.00</div>"
                    "</body></html>").encode("utf-8")
        else:
            body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _logs(self):
        # 模拟真实站点：忽略时间/分页参数，直接返回全部记录
        return json.dumps({"data": LOG_ITEMS}).encode("utf-8")

    def log_message(self, *args):
        pass


def start(port=PORT):
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("mock server: http://127.0.0.1:%d" % port)
    return srv


if __name__ == "__main__":
    start()
    threading.Event().wait()
