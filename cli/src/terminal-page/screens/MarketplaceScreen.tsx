import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import { ScreenFrame } from "../panels/ScreenFrame.js";
import type { TerminalPageViewModel } from "../types.js";

/** 网页侧栏「技能」「连接器」进入的广场，页内不再互相切换。 */
export function MarketplaceScreen({ model }: { model: TerminalPageViewModel }): React.ReactElement {
  const kind = model.surface === "connectors" ? "连接器" : "技能";
  const listing = model.marketplace;
  const items = listing?.items ?? [];
  return (
    <ScreenFrame title={kind} count={listing?.loading ? "正在读取" : `${items.length} 项`}>
      {listing?.notice ? <Text color={TERMINAL_DESIGN.colors.warning}>{listing.notice}</Text> : null}
      {!listing?.loading && items.length === 0 ? <Text color={TERMINAL_DESIGN.colors.muted}>广场暂时没有结果。本机会话不受影响。</Text> : null}
      {items.slice(0, 12).map((item) => (
        <Text key={item.sourceId} wrap="truncate-end">
          {item.installed ? TERMINAL_DESIGN.symbols.success : " "} {item.title}
          <Text color={TERMINAL_DESIGN.colors.muted}>  {item.summary}</Text>
        </Text>
      ))}
      <Text color={TERMINAL_DESIGN.colors.muted}>安装与配置仍可在 Ctrl+G 的广场里完成</Text>
    </ScreenFrame>
  );
}
