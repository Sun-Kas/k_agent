"""只读校验工作台路由和 HTTP 方法；不启动服务或执行任何业务动作。"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from access_layer.main import create_app

root = Path(__file__).resolve().parents[2]
output = subprocess.run(
    ["node", "--import", "tsx", "--input-type=module", "-e",
     'import {WORKBENCH_OPERATIONS} from "./src/application/workbench-catalog.ts"; console.log(JSON.stringify(WORKBENCH_OPERATIONS));'],
    cwd=root / "cli", check=True, capture_output=True, text=True,
)
operations = json.loads(output.stdout)
schema = create_app().openapi()
checked = 0
for operation in operations:
    if operation.get("local"):
        continue
    expected = re.sub(r":\w+", "{}", operation["path"])
    route = next((path for path in schema["paths"] if re.sub(r"\{[^}]+\}", "{}", path) == expected), None)
    assert route is not None, f"未注册路由: {operation['id']} {expected}"
    assert operation["method"].lower() in schema["paths"][route], f"HTTP 方法不匹配: {operation['id']}"
    endpoint = schema["paths"][route][operation["method"].lower()]
    body_schema = endpoint.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    while "$ref" in body_schema:
        body_schema = schema["components"]["schemas"][body_schema["$ref"].split("/")[-1]]
    fields = {field["key"] for field in operation["fields"] if field["location"] == "body"}
    missing = set(body_schema.get("required", [])) - fields
    assert not missing, f"工作台缺少必填业务字段: {operation['id']} {missing}"
    checked += 1
print(f"{checked} 个工作台 HTTP 路由、方法与必填请求字段契约通过；未执行业务请求。")
