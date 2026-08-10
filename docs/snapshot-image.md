# K Agent 私有快照镜像

`Dockerfile.snapshot` 用于把当前机器上的 K Agent 完整状态封装进一个本地镜像。

## 镜像包含内容

- 当前 `.env` 中的全部环境变量和凭据
- `.k_agent/config` 中的模型、MCP、权限与目录配置
- `.k_agent/content` 中的 Memory 与 Skills
- `.k_agent/state` 中的 Sessions、Workspace、Agent Team 和定时任务数据库
- React 生产构建、Access Layer、Agent Backend 与 Python/Node 运行环境

> [!CAUTION]
> 该镜像及其构建缓存包含明文密钥、私人会话和工作区文件。不要推送到 Docker Hub、GHCR 或任何共享 Registry，不要导出给不可信设备。

## 构建

为了获得一致的 SQLite 和 Session 快照，构建前先停止正在运行的 K Agent 服务：

```bash
cd frontend
# 如果 npm run dev 正在前台运行，按 Ctrl+C 停止
cd ..
```

然后执行：

```bash
chmod +x scripts/build-snapshot-image.sh
./scripts/build-snapshot-image.sh k-agent:snapshot
```

也可以使用 Compose：

```bash
docker compose -f docker-compose.snapshot.yml build
docker compose -f docker-compose.snapshot.yml up -d
```

## 运行

```bash
docker run --rm \
  --name k-agent-snapshot \
  -p 127.0.0.1:3001:3001 \
  k-agent:snapshot
```

打开 <http://127.0.0.1:3001>。容器内的 Agent Backend 只监听 `127.0.0.1:3002`，不会映射到宿主机。

## 镜像行为

- 镜像是构建时的数据快照；容器运行后的新数据写入容器可写层。
- 删除容器会删除运行后新增的数据，但不会改变镜像内的原始快照。
- 从同一镜像重新创建容器，会再次回到构建时的状态。
- 需要更新快照时，停止服务并重新构建镜像。

## 本机归档

如需离线备份，可保存成 tar 文件。该文件同样包含所有密钥与私人数据：

```bash
docker save k-agent:snapshot -o k-agent-snapshot.tar
chmod 600 k-agent-snapshot.tar
```
