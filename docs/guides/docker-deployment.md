# K Agent Docker 部署

镜像只包含应用代码、前端静态文件和运行依赖。`.env`、API Key、MCP 凭据、Sessions、Skills、Memory、Team、定时任务数据库和 Workspace 均不会进入镜像层。

## 数据边界

| 内容 | 注入方式 | 是否进入镜像 |
| --- | --- | --- |
| 环境变量与 API Key | `--env-file .env` | 否 |
| K Agent 状态 | Docker named volume | 否 |
| 应用代码与前端构建 | Docker image | 是 |

`.dockerignore` 明确排除了 `.env*`、`.k_agent/`、`data/`、`.git/`、虚拟环境和前端依赖目录。

## 推荐：Docker Compose

### 1. 准备配置

```bash
cp .env.example .env
```

填写模型 API 配置：

```env
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini
```

Compose 会覆盖容器内部监听地址，无需修改 `.env` 中用于本地开发的 `HOST`。

### 2. 构建

```bash
docker compose build
```

### 3. 启动

```bash
docker compose up -d
```

### 4. 验证

```bash
docker compose ps
docker compose logs -f k-agent
curl http://127.0.0.1:3001/api/health
curl http://127.0.0.1:3001/api/health/scheduled-tasks
```

部署机浏览器打开 <http://127.0.0.1:3001>。同一内网的设备打开
`http://<部署机内网 IP>:3001`；Compose 默认监听宿主机全部 IPv4 网卡，因此不需要
提前把动态内网 IP 写入配置。

可以在部署机查询当前内网地址：

```bash
# macOS 常见 Wi-Fi 接口
ipconfig getifaddr en0

# Linux
hostname -I
```

> [!WARNING]
> `0.0.0.0:3001` 会让所有能够连接部署机的网络访问 K Agent。请通过开发机防火墙
> 将来源限制在可信内网；在未增加登录认证前，不要通过路由器端口转发或公网安全组
> 暴露该端口。

### 5. 停止或更新

```bash
docker compose stop

# 更新代码后重新构建并替换容器
docker compose up -d --build
```

停止并删除容器，但保留数据：

```bash
docker compose down
```

删除容器和全部 K Agent 数据：

```bash
docker compose down -v
```

> [!CAUTION]
> `down -v` 会永久删除 Sessions、Skills、Team 和定时任务数据。

## 使用现有 `.k_agent` 数据

默认 Compose 使用名为 `k-agent-data` 的 Docker Volume。首次导入当前项目数据：

```bash
# 先让 Docker 初始化 named volume 的目录与权限，然后停止服务。
docker compose up -d
docker compose stop

# 以镜像内的非 root 用户复制，避免导入后 SQLite 目录不可写。
docker run --rm \
  --volumes-from k-agent \
  -v "$PWD/.k_agent:/import:ro" \
  --entrypoint sh \
  k-agent:local \
  -c 'cp -R /import/. /app/.k_agent/'

docker compose start
```

数据导入发生在容器运行时，不会写入镜像层。导入 SQLite 数据前应先停止本地 K Agent 服务，避免复制到不一致的数据库状态。

## 不使用 Compose

```bash
docker build -t k-agent:local .
docker volume create k-agent-data
docker run -d \
  --name k-agent \
  --restart unless-stopped \
  --env-file .env \
  -e HOST=0.0.0.0 \
  -e AGENT_BACKEND_HOST=127.0.0.1 \
  -e AGENT_BACKEND_URL=http://127.0.0.1:3002 \
  -e K_AGENT_HOME=/app/.k_agent \
  -p 0.0.0.0:3001:3001 \
  -v k-agent-data:/app/.k_agent \
  k-agent:local
```

## 发布前检查

```bash
docker history --no-trunc k-agent:local
docker run --rm --entrypoint sh k-agent:local -c \
  'test ! -e /app/.env && test ! -e /app/.k_agent/config && echo sanitized'
```

镜像可以发布到私有或公共 Registry，但仍应在 CI 中执行密钥扫描，并避免通过 Docker build arguments 传入凭据。
