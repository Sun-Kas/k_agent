import React from "react";
import { Text, useInput } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import { Overlay } from "./Overlay.js";

export function HelpOverlay({ onClose }: { onClose: () => void }): React.ReactElement {
  useInput((_input, key) => {
    if (key.escape || key.return) onClose();
  });
  return (
    <Overlay title="快捷键">
      <Text>/         展开命令选择栏（Tab 补全 · Enter 执行）</Text>
      <Text>Ctrl+K    同一命令目录的浮层入口（运行中或断线时可用）</Text>
      <Text>Shift+Tab  切换 工作 / 团队 / 自动 / 诊断</Text>
      <Text>1–4       直接跳到对应模式（输入框为空时）</Text>
      <Text>↑↓        首页选择功能 · Enter 执行</Text>
      <Text>Tab       在可见区域间移动焦点</Text>
      <Text>Ctrl+P    Session 切换      Ctrl+O  Inspector</Text>
      <Text>Ctrl+C    停止当前 Run（无运行时退出）</Text>
      <Text> </Text>
      <Text color={TERMINAL_DESIGN.colors.accent}>输入框</Text>
      <Text>←→        移动光标          Alt+←→  按词移动</Text>
      <Text>Ctrl+A/E   行首 / 行尾       Ctrl+W  删除前一个词</Text>
      <Text>Ctrl+U     删到行首          Ctrl+D  删除光标字符</Text>
      <Text>Alt+Enter  插入换行          Enter   发送</Text>
      <Text> </Text>
      <Text color={TERMINAL_DESIGN.colors.muted}>Esc 只关闭浮层，不代表拒绝审批。Enter 或 Esc 关闭本页。</Text>
    </Overlay>
  );
}
