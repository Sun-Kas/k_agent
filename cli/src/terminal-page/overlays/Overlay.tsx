import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";

/**
 * 所有临时覆盖层共用的外框。
 *
 * 浮层固定使用双线边框，和常规圆角面板形成层级差；宽度集中在 design.ts 控制，
 * 避免每个对话框各自定义尺寸后在窄终端出现不一致的换行。
 */
export function Overlay({ title, children }: { title: string; children: React.ReactNode }): React.ReactElement {
  return (
    <Box
      flexDirection="column"
      borderStyle={TERMINAL_DESIGN.borders.overlay}
      borderColor={TERMINAL_DESIGN.colors.focus}
      paddingX={2}
      paddingY={1}
      width={TERMINAL_DESIGN.layout.overlayColumns}
    >
      <Text bold color={TERMINAL_DESIGN.colors.accent}>{title}</Text>
      {children}
    </Box>
  );
}
