import React from "react";
import { Box, Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";

/**
 * 所有临时覆盖层共用的外框。
 *
 * 浮层用圆角框，和 REPL 提示符同一套边框语言。
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
