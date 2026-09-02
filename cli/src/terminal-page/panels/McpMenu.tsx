import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import type { RuntimeCatalogItem, TerminalPageViewModel } from "../types.js";

/**
 * `/mcp` 管理栏。贴在 Composer 下面，不盖住输入框。
 * 选中项和按键由页面根组件持有，这里只渲染。
 */
export function McpMenu({ model, selected, detailId }: {
  model: TerminalPageViewModel;
  selected: number;
  detailId?: string;
}): React.ReactElement {
  const servers = model.runtime.mcpServers;
  const current = servers[Math.min(selected, Math.max(0, servers.length - 1))];
  const tools = detailId
    ? model.runtime.mcpTools.filter((item) => item.serverId === detailId)
    : [];

  return (
    <Box flexDirection="column" borderStyle={TERMINAL_DESIGN.borders.panel} borderColor={TERMINAL_DESIGN.colors.accent} paddingX={1}>
      <Text bold color={TERMINAL_DESIGN.colors.accent}>MCP</Text>
      {servers.length === 0
        ? <Text color={TERMINAL_DESIGN.colors.muted}>当前没有配置 MCP。</Text>
        : detailId && (servers.find((item) => item.id === detailId) ?? current)
          ? <McpToolDetail server={(servers.find((item) => item.id === detailId) ?? current)!} tools={tools} />
          : servers.map((server, itemIndex) => (
            <Text key={server.id} inverse={itemIndex === selected}>
              {itemIndex === selected ? `${TERMINAL_DESIGN.symbols.pointer} ` : "  "}
              {statusMark(server)} {server.enabled ? "on " : "off"} {server.name}
              {server.name === server.id ? "" : ` (${server.id})`}
              {typeof server.toolCount === "number" ? ` · ${server.toolCount} tools` : ""}
              {server.status ? ` · ${server.status}` : ""}
            </Text>
          ))}
      <Text color={TERMINAL_DESIGN.colors.muted}>
        {model.mcpBusy
          ? "正在写入 Access Layer…"
          : detailId
            ? "Esc 返回列表"
            : "↑↓ 选择   Space 开关   Enter 工具   r 重载   Esc 关闭"}
      </Text>
    </Box>
  );
}

function McpToolDetail({ server, tools }: {
  server: RuntimeCatalogItem;
  tools: TerminalPageViewModel["runtime"]["mcpTools"];
}): React.ReactElement {
  return (
    <Box flexDirection="column">
      <Text>{server.name} · {server.enabled ? "已启用" : "已关闭"} · {server.status || "unknown"}</Text>
      {server.error ? <Text color={TERMINAL_DESIGN.colors.danger}>{server.error}</Text> : null}
      {tools.length === 0
        ? <Text color={TERMINAL_DESIGN.colors.muted}>没有已连接的工具（关闭的 server 不会暴露 tools）。</Text>
        : tools.slice(0, 12).map((tool) => (
          <Text key={`${tool.serverId}:${tool.name}`}>
            - {tool.name}{tool.description ? `  ${tool.description}` : ""}
          </Text>
        ))}
      {tools.length > 12 ? <Text color={TERMINAL_DESIGN.colors.muted}>  还有 {tools.length - 12} 个工具</Text> : null}
    </Box>
  );
}

function statusMark(server: RuntimeCatalogItem): string {
  if (server.enabled === false) return "○";
  if (server.status === "connected") return "●";
  if (server.status === "failed") return "×";
  return "·";
}
