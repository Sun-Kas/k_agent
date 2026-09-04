# Skill / MCP 广场技术方案

> 状态：第一版已按本文落地（Access Layer 代理 + 安装落盘 + 前端广场页）  
> 日期：2026-09-04  
> 范围：Access Layer 市场聚合、安装落盘、Frontend 广场 UI  
> 关联：[Skill Catalog 与正文懒加载](../architecture/skill-catalog-runtime-boundary-technical-solution.md)、[权限模式与 HITL](../architecture/permission-and-hitl-technical-solution.md)

## 0. 最终结论

公网广场和本机 Catalog 是两套数据，生命周期不同。

| 数据层 | 是什么 | 能否进入 run / 会话勾选 | 消费方 |
| --- | --- | --- | --- |
| 公网广场 | Registry / SkillHub 的发现索引 | 否。未安装、未校验 | 广场浏览、搜索、详情、安装预览 |
| 本机 Catalog | `$K_AGENT_HOME/config/catalog/*.json` | 是。唯一运行时元数据源 | 会话选择器、Team、`selected_runtime` |
| Skill 正文 / MCP 连接 | `content/skills/<id>/`、`config/mcp.json` | 安装成功后才存在 | Backend 按需读正文；MCP 进程连接 |

一句话：

> Access Layer 代理外部注册表并归一化展示；用户明确安装且校验成功后，才写入现有 Catalog。Frontend / CLI 不直连公网广场，Agent Backend 不感知广场。

第一版只做 **MCP 广场** 和 **Skill 广场**。插件没有对等注册表和本机包格式，不做第三套 source。

## 1. 为什么不能把广场塞进 `/api/catalog`

当前链路：

```text
Frontend / CLI
  → GET /api/catalog
  → RuntimeCatalog.list_payload()
  → config/catalog/mcp.json + skills.json
```

`selected_runtime(mcp_ids, skill_ids)` 把勾选 ID 解析成 Backend 载荷。未知或 `enabled=false` 会拒绝。如果把未安装的公网条目写进同一份 JSON：

- 会话选择器会出现不能跑的 MCP/Skill；
- 用户勾选后 run 会 `CatalogError` 或连上未审查的远程进程；
- Skill 正文懒加载边界被破坏：catalog 行没有对应 `content/skills/<id>/`。

因此 `/api/catalog`、`GET/PUT /api/config/mcp`、`GET/PUT /api/config/skills` **只表示本机已安装目录**。广场使用新前缀 `/api/marketplace/*`。

## 2. 不可破坏的架构边界

### 2.1 Access Layer

负责：

- 用服务端 HTTP 客户端请求魔搭 MCP 广场与 SkillHub；
- 持有 `SKILLHUB_API_KEY`，永不下发浏览器；
- 短 TTL 缓存列表/详情，吸收 429 和 Preview 抖动；
- 把外部 JSON 归一成内部 DTO，并对照本机 catalog 打 `installed`；
- 下载 Skill zip、跟随 302、走现有 zip 校验后写入 `content/skills/`；
- 把 MCP `packages` / `remotes` 译成本地 `stdio` / `http` 配置，缺密钥时停在预览；
- 安装成功后更新 catalog，并按需 `POST /internal/mcp/reload`。

不负责：

- 把广场条目当作 run payload；
- 在浏览阶段读取或缓存 `SKILL.md` 正文；
- 替用户执行未确认的 `npm install` / `pip install`。

### 2.2 Agent Backend

零改动。继续只消费本轮请求里的 MCP 连接配置和 Skill catalog 元数据。工具调用时才读 `content/skills/<id>/SKILL.md`。

### 2.3 Frontend / CLI

- 广场只打 `/api/marketplace/*`；
- 会话勾选、Team、Composer 芯片只打 `/api/catalog`；
- 配置中心继续编辑本机 MCP/Skill，可从广场「已安装」跳转过来补 env；
- 禁止把 Registry / SkillHub 的 Origin 写进 Vite 代理或浏览器 `fetch`。

### 2.4 跨模块约束

- `access_layer/**` 不导入 `backend/**`；
- Backend 不新增 marketplace 模块；
- 公网原始 JSON 不写入 `history.jsonl`、Session API、Langfuse 用户正文。

## 3. 外部数据源

### 3.1 MCP：魔搭 ModelScope MCP 广场

- Base：`https://www.modelscope.cn`
- 文档：[魔搭 MCP 广场](https://modelscope.cn/mcp)、OpenAPI `PUT /openapi/v1/mcp/servers`、`GET /openapi/v1/mcp/servers/{id}`
- 场景：国内可访问的 MCP 发现目录，中文名称/简介，列表带 `total_count`，可按页翻。

只用只读发现接口：

```text
PUT /openapi/v1/mcp/servers
  body: { "filter": {}, "page_number": 1, "page_size": 24, "search": "" }
GET /openapi/v1/mcp/servers/{urlEncodedId}
```

`id` 形如 `@amap/amap-maps`，必须 URL-encode。安装映射读详情里的 `server_config`（stdio 的 command/args/env）和 `env_schema`（必填密钥）。

环境变量：

```text
MODELSCOPE_MCP_BASE_URL=https://www.modelscope.cn
MODELSCOPE_MCP_TIMEOUT_SECONDS=15
MODELSCOPE_API_TOKEN=
```

公开列表可不带 Token；Token 只在 Access Layer。

第一版不再默认使用 Official MCP Registry（`registry.modelcontextprotocol.io`）。

### 3.2 Skill：SkillHub Open API

- Base：`https://api.skillhub.cn`
- 文档：[Open API 总览](https://github.com/Tencent/skillhub/blob/main/docs/api/README.md)
- 场景：中文搜索、分类、榜单、详情、评测、下载。文档把「搭建自己的技能广场」列为用法。

第一版使用：

```text
GET /api/skills?keyword=&category=&sortBy=&page=&pageSize=
GET /api/skills/top
GET /api/v1/categories
GET /api/v1/skills/{slug}
GET /api/v1/skills/{slug}/evaluation
GET /api/v1/skills/{slug}/versions
GET /api/v1/download?slug={slug}
```

响应格式不统一，代理层必须拆开：

- `/api/skills`、`/api/skills/top`：`{ "code": 0, "data": { ... } }`，业务在 `data`；
- `/api/v1/*`：直接业务对象；
- 下载与单文件：`302` 到对象存储，服务端必须 follow redirects（httpx `follow_redirects=True`）。

Header（只在 Access Layer）：

```text
X-API-Key: $SKILLHUB_API_KEY
X-Client-User-Id: sha256(local-stable-id)[:16]
```

公开列表现在可无 Key，但文档写明后续 `X-API-Key` 会必填。实现必须从第一天走 Header；Key 空时仍可尝试公开接口，401/403 返回明确「未配置 SkillHub Key」。

环境变量：

```text
SKILLHUB_BASE_URL=https://api.skillhub.cn
SKILLHUB_API_KEY=
SKILLHUB_TIMEOUT_SECONDS=20
K_AGENT_MARKETPLACE_CLIENT_ID=   # 可选；默认从 K_AGENT_HOME 路径做稳定哈希
```

`.env.example` 只放空 Key 注释，不放真实 Key。`.env` 保持 gitignore。

### 3.3 明确不做（第一版）

- **skills.sh**：Vercel OIDC，不适合本机 FastAPI。需要时作为第二 Skill source，接口保持同一 DTO。
- **插件广场**：无包格式、无安装目录、无注册表。UI 不放空 tab。
- **把 MEMORY.md 或 session compact 当 Skill 商店。**

## 4. 内部 DTO

广场列表/详情只返回这一份形状。外部字段名不准漏到 Frontend。

```json
{
  "kind": "mcp",
  "source": "modelscope",
  "sourceId": "io.github.user/filesystem",
  "title": "Filesystem",
  "summary": "...",
  "version": "1.0.2",
  "icons": [],
  "homepage": "https://github.com/...",
  "categories": [],
  "tags": [],
  "stats": { "downloads": null, "installs": null, "score": null },
  "officialStatus": "active",
  "installed": true,
  "localId": "filesystem",
  "installPreview": {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
    "envKeys": ["ALLOWED_PATHS"],
    "missingEnvKeys": [],
    "blockedReason": null
  }
}
```

Skill 的 `kind` 为 `"skill"`，`source` 为 `"skillhub"`，`sourceId` 为 slug。`installPreview` 为：

```json
{
  "archive": "zip",
  "requiresFrontmatter": ["name", "description"],
  "blockedReason": null
}
```

列表响应：

```json
{
  "items": [],
  "page": {
    "nextCursor": "opaque-or-null",
    "page": 1,
    "pageSize": 20,
    "total": 107008
  },
  "sourceStatus": "ok",
  "warnings": []
}
```

MCP 用 cursor；SkillHub 用 page/total。两种分页字段都出现，前端按 `kind` 选用。`sourceStatus`：`ok` | `degraded` | `unavailable`。外部失败时 `items=[]` 且 `warnings` 说明原因，HTTP 仍 200，避免广场打挂工作台。

`installed` 判定：

- MCP：`catalog/mcp.json` 任一行 `marketplace.source == "modelscope"` 且 `marketplace.sourceId == @org/name`；否则再比本地 `id` 与 name 最后一段（弱匹配，只用于展示，安装仍以 sourceId 为准）。
- Skill：`catalog/skills.json` 的 `marketplace.sourceId == slug`；否则比规范化后的 `id` 与 slug。

弱匹配只影响 `installed` 徽章，不能覆盖安装时的冲突检查。

## 5. Access Layer 公开 API

全部只读发现走 GET；改变本机状态走 POST。不把外部查询参数原样暴露成必填同名，内部再翻译。

### 5.1 发现

```text
GET /api/marketplace/mcp?q=&page=1&pageSize=24
GET /api/marketplace/mcp/{sourceId}
GET /api/marketplace/skills?q=&category=&sortBy=&page=1&pageSize=20
GET /api/marketplace/skills/top
GET /api/marketplace/skills/categories
GET /api/marketplace/skills/{slug}
GET /api/marketplace/skills/{slug}/evaluation
GET /api/marketplace/skills/{slug}/versions
```

`sourceId` 路径参数对 MCP 使用 `quote(server.name, safe="")` 编码，避免 `/` 截断。

详情接口合并本机安装状态。评测/版本透传归一化后的子集：分数、等级、版本号、发布时间；不转存评测全文到 catalog。

### 5.2 安装预览（不写盘）

```text
POST /api/marketplace/mcp/{sourceId}/preview
POST /api/marketplace/skills/{slug}/preview
```

返回第 4 节 DTO + 冲突信息：

- `conflict.localId`：将占用的本地 id；
- `conflict.exists`：目录或 catalog 行已存在；
- MCP：`missingEnvKeys`、`blockedReason`（无法映射的包类型）。

### 5.3 安装提交（写盘）

```text
POST /api/marketplace/mcp/{sourceId}/install
POST /api/marketplace/skills/{slug}/install
```

MCP body：

```json
{
  "id": "filesystem",
  "enabled": true,
  "env": { "ALLOWED_PATHS": "/Users/me/proj" },
  "headers": {}
}
```

`id` 可选；默认取 `server.name` 的最后一段再做与现有 MCP id 相同的安全裁剪。`env`/`headers` 只接受 preview 声明过的 key。未声明 key 丢弃。缺 `missingEnvKeys` 中的必填项则 400，不写盘。

Skill body：

```json
{
  "id": null,
  "enabled": true
}
```

`id` 为空时用 zip 内 `SKILL.md` 的 `name` 走现有 `_normalize_imported_skill_id`。若目标已存在：409，提示走配置中心覆盖或先删除。第一版安装不覆盖已有包。

成功响应：本机 catalog 刷新后的轻量行 + `localId`。MCP 安装成功后调用现有 `/internal/mcp/reload`。Skill 不 reload Backend 进程；下一轮 run 的 catalog 选择即可，正文仍懒加载。

### 5.4 卸载（可选，第一版建议做）

```text
POST /api/marketplace/mcp/{localId}/uninstall
POST /api/marketplace/skills/{localId}/uninstall
```

复用配置中心删除语义：MCP 从 `mcp.json` + catalog 去掉；Skill 删除 `content/skills/<id>/` 并更新 catalog。禁止只删 catalog 行留下孤儿目录。进行中的 session 勾选了该 id 时，下一轮 `selected_runtime` 自然失败或过滤，不在卸载接口里改 history。

## 6. MCP 安装映射

Registry 条目不是 `mcp.json`。Access Layer 按优先级翻译，**只选一种传输**：

1. 若 `server.remotes` 含 `streamable-http` 或 `http`：`type=http`，`url=remote.url`。需要的 header 名列入 `envKeys` / `missingEnvKeys`（值由用户填）。
2. 否则取第一个可执行的 `server.packages[]`：
   - `registryType=npm`：`command=npx`，`args=["-y", identifier]`，必要时附加 package 声明的 `runtimeArguments` / `packageArguments`（只接受官方 schema 里的字面量，禁止执行任意 shell）。
   - `registryType=pypi`：`command=uvx` 或 `python -m`；若本机探测不到 runner，`blockedReason=unsupported_runtime`。
   - `oci` / `nuget` / 未知：第一版 `blockedReason=unsupported_package`，允许收藏但不安装。
3. 若 packages 与 remotes 都空：`blockedReason=no_install_target`。

翻译结果写入现有结构：

```text
config/mcp.json          # command/args/env 或 url/headers
config/catalog/mcp.json  # id/name/description/enabled + marketplace
```

`marketplace` 块：

```json
{
  "source": "modelscope",
  "sourceId": "io.github.user/filesystem",
  "version": "1.0.2",
  "installedAt": "2026-09-04T03:00:00Z"
}
```

`_mcp_summary()` 必须保留 `marketplace`（未知字段今天会被丢掉）。`selected_runtime` 发给 Backend 的 MCP 配置 **去掉** `marketplace`，避免污染连接器。

不在安装时替用户执行全局 `npm i -g`。stdio 使用 `npx -y` 等一次性 runner；失败显示 Backend 连接 error，与现有 MCP 配置中心一致。

## 7. Skill 安装映射

```text
preview → GET SkillHub /api/v1/download?slug=
       → 跟随 302，限制最终 host 为文档/常见对象存储，限制体积 ≤ 现有 MAX_SKILL_ZIP_BYTES
       → 内存 bytes 交给 _validate_and_install_skill_zip
       → 解析 SKILL.md frontmatter
       → write_skill_summaries（含 marketplace）
```

禁止：

- 把 SkillHub 详情里的 Markdown 说明当成 `SKILL.md` 写入；
- 浏览列表时预下载 zip；
- 把评测报告写进 catalog 或 Prompt。

`skill_catalog_row()` 增加可选 `marketplace`，规则同 MCP。`selected_runtime` 返回给 Backend 的 skill 行必须继续只有现有运行时字段（id/name/description/enabled/frontmatter 元数据），**剥离 marketplace**。否则会把公网 slug 泄漏进模型可见结构。

本地 id 与 slug 不一致是常态。对照表只存在 catalog 的 `marketplace.sourceId`。

下载失败、非 zip、缺 name/description、路径穿越：沿用 `_validate_and_install_skill_zip` 的 400/413，外加 `source=skillhub` 日志字段（只记 slug，不记文件内容）。

## 8. 缓存、限流与降级

模块：`access_layer/marketplace/`（新包）。进程内 TTL 缓存即可，第一版不引入 Redis。

| 资源 | TTL | 失败 |
| --- | --- | --- |
| MCP 列表（按 q+cursor+limit） | 60s | 返回上次成功缓存，否则 `sourceStatus=unavailable` |
| MCP 详情 | 5min | 同上 |
| Skill 列表 / top / categories | 60s | 同上 |
| Skill 详情 / evaluation / versions | 5min | 同上 |
| Skill zip | 不缓存 | 每次安装现拉 |

并发：同一 cache key 合并 in-flight 请求。SkillHub `429`/`5xx` 指数退避，最多 2 次；仍失败降级。超时用上面的 `*_TIMEOUT_SECONDS`。

结构化日志事件（无正文、无 Key）：

```text
marketplace_fetch_ok
marketplace_fetch_failed
marketplace_install_started
marketplace_install_succeeded
marketplace_install_failed
```

字段：source、sourceId、localId、latencyMs、httpStatus、errorCode。

## 9. 前端

配置中心保持「本机已安装编辑器」。广场是独立 Manage 页面，不要把搜索框塞进现有 Skills tab 的同一列表。

信息架构：

```text
工作台（Chat / Team / Auto）
  会话勾选 MCP/Skill ← GET /api/catalog

Manage
  配置中心 ← /api/config/{mcp,skills,models}
  广场
    MCP ← /api/marketplace/mcp
    Skill ← /api/marketplace/skills
```

广场每张卡片：标题、摘要、来源徽章、版本、`installed`。已安装显示「打开配置」；未安装显示「预览安装」。MCP 预览弹出 env 表单后再提交。Skill 预览展示将生成的 `localId` 和冲突，确认后安装。

安装成功后：

1. 刷新 `/api/catalog`（App 里现有 `mcpServers` / `skills` state）；
2. 不自动勾进当前会话，避免未读说明的 Skill 直接进入 Prompt；
3. Toast 提示去会话选择器启用。

CLI 第一版不接广场搜索。安装完成后，现有 `/mcp`、`/skill` 开关继续只读 catalog。需要时第二期加 `k-agent marketplace search`。

## 10. Catalog 写入规则（安装之后）

允许新增字段（Catalog 层，非 Backend 载荷）：

```text
marketplace.source
marketplace.sourceId
marketplace.version
marketplace.installedAt
```

禁止：

- 把广场 `summary` 覆盖已经用户编辑过的 `description`（重装 409，不静默覆盖）；
- 把 icons、评测、downloads 写入 catalog；
- 未安装成功写半行 catalog。

Skill 安装与现有 `POST /api/skills` zip 导入共享同一落盘函数，避免两套解压逻辑。Marketplace 只是下载字节的来源。

MCP 安装与 `PUT /api/config/mcp` 共享 `_write_local_mcp_config` + `write_mcp_summaries`。Marketplace 先读出完整 servers 列表，追加或拒绝冲突，再整表写回。

## 11. 安全

- 浏览器永不带 SkillHub Key、永不 follow 到任意 302 下载 zip。
- Skill zip：现有路径穿越、文件数、解压体积、禁止绝对路径；额外限制下载最终 URL host（允许列表或与 SkillHub 文档一致的对象存储后缀）。
- MCP stdio：只生成 argv 数组，不拼接用户字符串进 shell。
- MCP http：`url` 必须来自 Registry remote，用户只能补 header/env 值，不能改成任意 URL 绕过 preview（高级用户仍可在配置中心手改，那是本机信任面）。
- `X-Client-User-Id` 只用稳定匿名哈希，不上报邮箱。
- 安装审计只写 Access Layer 日志，不写对话 history。

## 12. 代码地图

| 路径 | 职责 |
| --- | --- |
| `access_layer/marketplace/__init__.py` | 导出门面 |
| `access_layer/marketplace/models.py` | 内部 DTO / preview / page |
| `access_layer/marketplace/mcp_registry.py` | 魔搭 MCP 广场客户端 |
| `access_layer/marketplace/skillhub.py` | SkillHub 客户端（信封、302、Header） |
| `access_layer/marketplace/cache.py` | TTL + singleflight |
| `access_layer/marketplace/install_mcp.py` | packages/remotes → mcp.json |
| `access_layer/marketplace/install_skill.py` | 下载 zip → 现有校验安装 |
| `access_layer/marketplace/match.py` | sourceId ↔ local catalog |
| `access_layer/main.py` | 注册 `/api/marketplace/*` |
| `access_layer/catalog.py` | `_mcp_summary` / `skill_catalog_row` 保留 marketplace；`selected_runtime` 剥离 |
| `frontend/src/api/marketplace.ts` | 仅打 Access Layer |
| `frontend/src/components/Marketplace.tsx` | 广场 UI |
| `.env.example` | `SKILLHUB_API_KEY`、`MODELSCOPE_MCP_BASE_URL` 空模板 |

Agent Backend、`history.jsonl`、compact state 不出现在本表。

## 13. 实施顺序

### 阶段 A：代理与 DTO（可先合并）

- 新包 + MCP/SkillHub 客户端 + 缓存；
- `GET /api/marketplace/mcp` 与 `GET /api/marketplace/skills`；
- 对照 catalog 打 `installed`；
- 单测：fixtures 回放外部 JSON，断言归一化字段；Key 不出现在响应。

### 阶段 B：安装

- preview / install；
- MCP 映射表（npm stdio、http remote、unsupported）；
- Skill 走现有 zip 安装 + marketplace 元数据；
- catalog 读写兼容旧文件（无 marketplace 键）。

### 阶段 C：前端广场

- Manage 入口、两 tab、搜索/分页、已安装徽章、安装预览表单；
- 安装后刷新 `/api/catalog`，不自动勾选。

### 阶段 D：卸载与配置中心衔接

- uninstall；
- 配置中心卡片显示「来自 MCP Registry / SkillHub」只读来源。

阶段 A 不写盘。阶段 B 未完成前前端可以只读浏览。

## 14. 测试

Access Layer（`backend/tests` 或 `access_layer` 现有 test 布局，与 `test_access_catalog.py` 放一起）：

- Registry 列表 fixture → DTO；`cursor` 原样传递；name 编码；
- SkillHub 列表信封 vs v1 详情；无 Key 时 Header 省略或空，响应不含 Key；
- `installed` 强匹配 sourceId、弱匹配不误装；
- MCP npm package → `npx -y identifier`；http remote → `type=http`；oci → blocked；
- 缺 env 的 install → 400 且 mcp.json 未变；
- Skill 302 下载 → zip 校验失败不留半目录；
- 已存在 skill id → 409；
- `selected_runtime` 载荷无 `marketplace`；
- 外部 503 → HTTP 200 + `sourceStatus=unavailable`。

Frontend：广场请求 URL 断言不包含 `skillhub.cn` / `registry.modelcontextprotocol.io` / `modelscope.cn`。

回归：

```bash
.venv/bin/python -m pytest backend/tests/test_access_catalog.py backend/tests/test_marketplace.py -q
npm --prefix frontend run check
```

## 15. 验收标准

- 会话选择器条目数量在浏览广场后不变，除非用户安装成功；
- 未安装条目不能出现在 `GET /api/catalog`；
- 安装成功的 Skill 可在配置中心看到正文，run 勾选后 Backend 才能读 `SKILL.md`；
- 安装成功的 MCP 出现在 `GET /api/config/mcp`，reload 后 capabilities 增加或给出连接 error；
- Frontend 网络面板没有对 SkillHub / Registry 的直连；
- `SKILLHUB_API_KEY` 不出现在 Session API、catalog JSON 明文以外的配置文件、前端 bundle；
- Registry 或 SkillHub 宕机时 Chat 仍可用。

## 16. 明确不在本轮实现

- skills.sh 双源聚合与安全审计展示；
- 插件广场；
- 覆盖安装 / 自动升级到 latest；
- 代表用户执行全局包管理器安装；
- 把广场搜索做进 CLI；
- 高级 MCP 搜索（官方也不提供，需要自建倒排时另开方案）；
- 将评测分数写入 Prompt 或 compact summary。
