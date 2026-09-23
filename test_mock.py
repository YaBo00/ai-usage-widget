# -*- coding: utf-8 -*-
"""无头测试：验证 ai_usage_widget 的按模型聚合与时间筛选逻辑"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_server
import ai_usage_widget as m

BASE = {"site_url": "http://127.0.0.1:8899", "api_key": "sk-mock",
        "refresh_seconds": 180, "quota_per_unit": 500000.0,
        "currency": "$", "show_converted": True, "proxy": ""}


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + detail if detail else ""))
    return cond


def main():
    mock_server.start()
    ok = True

    # 今天：deepseek 1.5M + glm 0.2M = 模型聚合，按金额降序；token=输入+输出
    rows, err = m.fetch_usage(dict(BASE, time_range="today"))
    ok &= check("today 按模型聚合",
                rows and rows[0][0] == "deepseek-v4.1-flash" and rows[0][1] == "$3.00"
                and rows[0][2] == "1.7k" and rows[1][0] == "glm-5.3" and rows[1][1] == "$0.4000"
                and rows[1][2] == "220",
                str(rows) + " " + str(err))

    # 昨天：只有 kimi-k3，$0.60 / 330 tokens
    rows, err = m.fetch_usage(dict(BASE, time_range="yesterday"))
    ok &= check("yesterday 只有 kimi-k3",
                rows and len(rows) == 1 and rows[0][0] == "kimi-k3" and rows[0][1] == "$0.6000"
                and rows[0][2] == "330",
                str(rows))

    # 全部：kimi 也进来，deepseek 仍第一
    rows, err = m.fetch_usage(dict(BASE, time_range="all"))
    ok &= check("all 包含 kimi-k3",
                rows and len(rows) == 3 and rows[0][0] == "deepseek-v4.1-flash",
                str(rows))

    # 未配置提示
    rows, err = m.fetch_usage({"site_url": "", "api_key": ""})
    ok &= check("未配置提示", rows is None and "尚未配置" in err, str(err))

    print("==== ALL PASS ====" if ok else "==== HAS FAILURES ====")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
