/**
 * 终端页面的集中设计配置。
 *
 * 修改颜色、区域宽度、文案和快捷键时优先改这里，避免视觉常量散落到
 * HTTP、事件投影或生命周期代码中，方便独立调整页面而不改变运行语义。
 */
export const TERMINAL_DESIGN = {
  layout: {
    wideMinColumns: 144,
    standardMinColumns: 110,
    compactMinColumns: 80,
    minimumColumns: 56,
    sessionRailColumns: 26,
    inspectorColumns: 42,
    overlayColumns: 68,
    composerMinRows: 1,
    composerMaxRows: 8,
    contentPadding: 1,
    timelinePreviewLines: 4,
    /** `/` 选择栏一次最多显示的候选数量，超出部分靠窗口滚动。 */
    slashMenuVisibleRows: 7,
    /** Ctrl+K 面板是全屏浮层，可以比行内选择栏多显示几行。 */
    paletteVisibleRows: 10,
  },
  density: {
    timelineGapRows: 0,
    timelineVisibleItems: 18,
    timelineTextPreviewLines: 8,
    sessionVisibleRows: 12,
    toolPreviewRows: 3,
    thinkingPreviewRows: 2,
  },
  colors: {
    accent: "cyan" as const,
    focus: "blueBright" as const,
    muted: "gray" as const,
    success: "green" as const,
    warning: "yellow" as const,
    danger: "red" as const,
  },
  borders: {
    /** REPL 提示符和浮层都用圆角，避免再做成 Web 面板的双边框。 */
    panel: "round" as const,
    overlay: "round" as const,
  },
  symbols: {
    brand: "✻",
    running: "●",
    waiting: "○",
    success: "✓",
    error: "×",
    stopped: "■",
    thinking: "◇",
    tool: "◆",
    approval: "!",
    question: "?",
    unread: "•",
    pointer: "❯",
    prompt: "❯",
  },
  fallbackSymbols: {
    brand: "*",
    running: "*",
    waiting: "o",
    success: "+",
    error: "x",
    stopped: "#",
    thinking: "?",
    tool: ">",
    approval: "!",
    question: "?",
    unread: ".",
    pointer: ">",
    prompt: ">",
  },
  copy: {
    brand: "K Agent",
    welcome: "欢迎使用 K Agent",
    tagline: "本地优先的 Agent 工作台",
    version: "0.1.0",
    /**
     * 开场标用 3 行窄宽字符（与 Claude Code 同结构），禁止全角方块 █。
     * █ 在不少终端按 2 列渲染，会撑破 Yoga 布局：模式行消失、输入框右边框对不齐。
     */
    logo: [
      "  ▐▛███▜▌  ",
      " ▝▜█████▛▘ ",
      "   ▘▘ ▝▝   ",
    ],
    logoFallback: [
      "  |\\  /|  ",
      "  | \\/ |  ",
      "  |    |  ",
    ],
    connected: "已连接",
    disconnected: "连接已断开",
    running: "正在运行",
    approvalRequired: "需要你的确认",
    questionRequired: "Agent 正在等待你的回答",
    newActivity: "有新的运行活动",
    reviewPendingInput: "按 ! 返回待处理输入",
    /** 提示符下方的 REPL 脚注，风格对齐 Claude Code：短、灰、可发现。 */
    composerKeys: "? 帮助   / 命令   ↑ 历史   Ctrl+P 会话",
    slashKeys: "↑↓ 选择   Tab 补全   Enter 执行   Esc 收起",
    paletteKeys: "输入以过滤   ↑↓ 选择   Enter 执行   Esc 关闭",
    startGuideTitle: "开始",
    startGuide: [
      "输入目标后回车即可开始",
      "运行中仍可输入，回车后排队发送",
      "终端滚动查看上文 · / 命令 · ↑ 历史 · Ctrl+P 会话 · Shift+Tab 切换模式",
    ],
  },
  keys: {
    commandPalette: "Ctrl+K",
    sessionSwitcher: "Ctrl+P",
    toggleInspector: "Ctrl+O",
    stopRun: "Ctrl+C",
    closeOverlay: "Esc",
    modeSwitch: "Shift+Tab",
    slashMenu: "/",
  },
} as const;

export type TerminalLayout = "wide" | "standard" | "compact" | "minimum";

export function terminalLayout(columns: number): TerminalLayout {
  if (columns >= TERMINAL_DESIGN.layout.wideMinColumns) return "wide";
  if (columns >= TERMINAL_DESIGN.layout.standardMinColumns) return "standard";
  if (columns >= TERMINAL_DESIGN.layout.compactMinColumns) return "compact";
  return "minimum";
}
