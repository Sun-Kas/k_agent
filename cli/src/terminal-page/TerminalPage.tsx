import React, { useEffect, useMemo, useRef, useState } from "react";
import { Box, Static, Text, useInput, useStdout } from "ink";
import { pendingApproval, type TimelineItem } from "../application/event-projector.js";
import { terminalLayout, TERMINAL_DESIGN } from "./design.js";
import { HOME_MODES, homePickItems, nextSurface, surfaceFromDigit } from "./home-catalog.js";
import { pushPromptHistory, promptHistoryText } from "./prompt-history.js";
import { canFlushPromptQueue, splitSettledTimeline } from "./repl-view.js";
import { filterSlashCommands, slashCommandAvailability, slashQuery, slashStatusPanel, type SlashStatusPanel } from "./slash-catalog.js";
import { TimelineEntry } from "./panels/ConversationTimeline.js";
import type { OverlayState, RuntimeChoice, TerminalFocus, TerminalPageAction, TerminalPageProps, TerminalPageViewModel } from "./types.js";
import { GlobalBar } from "./panels/GlobalBar.js";
import { Composer } from "./panels/Composer.js";
import { SlashMenu } from "./panels/SlashMenu.js";
import { StatusBar } from "./panels/StatusBar.js";
import { HomeScreen } from "./screens/HomeScreen.js";
import { ChatScreen } from "./screens/ChatScreen.js";
import { TeamScreen } from "./screens/TeamScreen.js";
import { AutomationScreen } from "./screens/AutomationScreen.js";
import { DoctorScreen } from "./screens/DoctorScreen.js";
import { CommandPalette } from "./overlays/CommandPalette.js";
import { SessionSwitcher } from "./overlays/SessionSwitcher.js";
import { ApprovalDialog } from "./overlays/ApprovalDialog.js";
import { UserQuestionDialog } from "./overlays/UserQuestionDialog.js";
import { HelpOverlay } from "./overlays/HelpOverlay.js";
import { StatusPanel } from "./panels/StatusPanel.js";
import { McpMenu } from "./panels/McpMenu.js";
import { OptionMenu } from "./panels/OptionMenu.js";
import { SkillMenu } from "./panels/SkillMenu.js";
import { ErrorBlock } from "./renderers/ErrorBlock.js";
import { PERMISSION_CHOICES } from "../application/runtime-switch.js";

type ComposerMenu = "mcp" | "model" | "agent" | "permissions" | "skill";

/**
 * 交互式终端页面的唯一根组件。
 *
 * 这里可以改变区域和键盘表现，但禁止直接 fetch；Session 切换也只报告意图，
 * 不得因为布局变化而隐式 stop 或 cancel 服务端正在运行的任务。
 */
export function TerminalPage({ model, onAction }: TerminalPageProps): React.ReactElement {
  const { stdout } = useStdout();
  const columns = stdout.columns ?? 80;
  const layout = terminalLayout(columns);
  const [draft, setDraft] = useState("");
  const [homePick, setHomePick] = useState(0);
  const [focus, setFocus] = useState<TerminalFocus>("composer");
  const [overlay, setOverlay] = useState<OverlayState>({ kind: "none" });
  const [showInspector, setShowInspector] = useState(false);
  const [dismissedInterruptId, setDismissedInterruptId] = useState<string>();
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const [statusPanel, setStatusPanel] = useState<SlashStatusPanel>();
  const [composerMenu, setComposerMenu] = useState<ComposerMenu>();
  const [menuIndex, setMenuIndex] = useState(0);
  const [mcpDetailId, setMcpDetailId] = useState<string>();
  const staticItemsRef = useRef<TimelineItem[]>([]);
  const seenStaticKeysRef = useRef(new Set<string>());
  const [staticItems, setStaticItems] = useState<TimelineItem[]>([]);
  const promptHistoryRef = useRef<string[]>([]);
  const historyCursorRef = useRef(-1);
  const historyStashRef = useRef("");
  const [sendQueue, setSendQueue] = useState<string[]>([]);
  const sendQueueRef = useRef<string[]>([]);
  const [localRunning, setLocalRunning] = useState(false);
  const interrupt = pendingApproval(model.timeline);
  const interruptVisible = interrupt && interrupt.id !== dismissedInterruptId;
  const effectiveOverlay: OverlayState = interruptVisible
    ? { kind: isUserQuestion(interrupt.detail) ? "question" : "approval", approval: interrupt }
    : overlay;
  const busy = localRunning || !canFlushPromptQueue(model.timeline.runStatus);
  const wasBusy = useRef(busy);

  useEffect(() => {
    const status = model.timeline.runStatus;
    if (status === "complete" || status === "error" || status === "stopped") setLocalRunning(false);
  }, [model.timeline.runStatus]);

  useEffect(() => {
    if (interrupt && interrupt.id !== dismissedInterruptId) setFocus("overlay");
  }, [interrupt, dismissedInterruptId]);

  useEffect(() => {
    const previouslyBusy = wasBusy.current;
    wasBusy.current = busy;
    if (!previouslyBusy || busy) return;
    const next = sendQueueRef.current[0];
    if (!next) return;
    sendQueueRef.current = sendQueueRef.current.slice(1);
    setSendQueue(sendQueueRef.current);
    onAction({ type: "submit_prompt", text: next });
  }, [busy, onAction]);

  const picks = homePickItems(model);
  const selectedPick = picks[Math.min(homePick, Math.max(0, picks.length - 1))];
  const query = slashQuery(draft);
  const slashMatches = useMemo(() => (query === undefined ? [] : filterSlashCommands(query)), [query]);
  const slashOpen = query !== undefined
    && !slashDismissed
    && slashMatches.length > 0
    && effectiveOverlay.kind === "none";
  const composerMenuOpen = composerMenu !== undefined && effectiveOverlay.kind === "none";
  const modeKeysIdle = effectiveOverlay.kind === "none" && !draft && !composerMenuOpen;

  // 草稿离开 `/` 状态时重置选择栏，避免下次打开时停在陈旧的高亮项。
  useEffect(() => {
    setSlashIndex(0);
    if (query === undefined) setSlashDismissed(false);
  }, [query]);

  // 再打开 `/` 选择栏等于选下一项功能，收起上一份只读列表。
  useEffect(() => {
    if (slashOpen) {
      setStatusPanel(undefined);
      setComposerMenu(undefined);
      setMcpDetailId(undefined);
    }
  }, [slashOpen]);

  useInput((input, key) => {
    if (effectiveOverlay.kind !== "none") return;
    if (key.ctrl && input === "k") return openOverlay({ kind: "commands" });
    if (key.ctrl && input === "p") return openOverlay({ kind: "sessions" });
    if (key.ctrl && input === "o") return setShowInspector((value) => !value);
    if (key.ctrl && input === "c") {
      if (model.timeline.runStatus === "running") onAction({ type: "stop_run" });
      else onAction({ type: "quit" });
      return;
    }
    if (slashOpen) {
      // 选择栏打开时导航键归它所有，输入框只继续处理正文编辑。
      if (key.escape) return setSlashDismissed(true);
      if (key.upArrow) return setSlashIndex((value) => (value + slashMatches.length - 1) % slashMatches.length);
      if (key.downArrow) return setSlashIndex((value) => (value + 1) % slashMatches.length);
      const selectedCommand = slashMatches[Math.min(slashIndex, slashMatches.length - 1)];
      if (key.tab && selectedCommand) return setDraft(`/${selectedCommand.name} `);
      if (key.return && selectedCommand) {
        if (!slashCommandAvailability(selectedCommand, model).enabled) return;
        runCommand(selectedCommand.name, selectedCommand.takesArguments === true);
      }
      return;
    }
    if (composerMenuOpen && composerMenu) {
      if (key.escape) {
        if (composerMenu === "mcp" && mcpDetailId) return setMcpDetailId(undefined);
        return setComposerMenu(undefined);
      }
      if ((composerMenu === "mcp" || composerMenu === "skill") && model.mcpBusy) return;
      if (composerMenu === "mcp" && mcpDetailId) return;
      const items = menuItems(composerMenu, model);
      if (composerMenu === "mcp" && input === "r") return onAction({ type: "reload_mcp" });
      if (!items.length) return;
      if (key.upArrow) return setMenuIndex((value) => (value + items.length - 1) % items.length);
      if (key.downArrow) return setMenuIndex((value) => (value + 1) % items.length);
      const current = items[Math.min(menuIndex, items.length - 1)];
      if (!current) return;
      if (composerMenu === "mcp") {
        if (input === " ") return onAction({ type: "toggle_mcp", serverId: current.id });
        if (key.return) return setMcpDetailId(current.id);
        return;
      }
      if (composerMenu === "skill") {
        if (input === " " || key.return) return onAction({ type: "toggle_skill", skillId: current.id });
        return;
      }
      if (key.return || input === " ") {
        if (!current.enabled) return;
        if (composerMenu === "model") onAction({ type: "set_model", modelId: current.id });
        if (composerMenu === "agent") onAction({ type: "set_agent", agentKind: current.id });
        if (composerMenu === "permissions") onAction({ type: "set_permission", permissionMode: current.id });
        setComposerMenu(undefined);
      }
      return;
    }
    if (statusPanel && key.escape) return setStatusPanel(undefined);
    if (input === "!" && interrupt) {
      setDismissedInterruptId(undefined);
      setFocus("overlay");
      return;
    }
    if (key.tab && key.shift) return onAction({ type: "open_surface", surface: nextSurface(model.surface, -1) });
    if (key.tab) return cycleFocus(1);
    if (modeKeysIdle && !key.ctrl && !key.meta && /^[1-4]$/.test(input)) {
      const surface = surfaceFromDigit(input);
      if (surface) onAction({ type: "open_surface", surface });
      return;
    }
    if (model.surface === "home" && !draft && (key.upArrow || key.downArrow)) {
      const delta = key.downArrow ? 1 : -1;
      setHomePick((value) => (value + delta + picks.length) % Math.max(1, picks.length));
      return;
    }
    if (model.surface === "home" && !draft && key.return && !key.meta && selectedPick) {
      activateHomePick(selectedPick.id);
      return;
    }
    if (input === "?" && !draft && !key.ctrl && !key.meta) return openOverlay({ kind: "help" });
  });

  function openOverlay(next: OverlayState): void {
    setOverlay(next);
    setFocus("overlay");
  }

  function closeOverlay(): void {
    if (interruptVisible) setDismissedInterruptId(interrupt.id);
    else setOverlay({ kind: "none" });
    setFocus("composer");
  }

  function cycleFocus(direction: -1 | 1): void {
    const visible: TerminalFocus[] = showInspector
      ? ["timeline", "inspector", "composer"]
      : ["timeline", "composer"];
    const current = Math.max(0, visible.indexOf(focus));
    setFocus(visible[(current + direction + visible.length) % visible.length]!);
  }

  function activateHomePick(id: string): void {
    const mode = HOME_MODES.find((item) => item.id === id);
    if (mode) {
      onAction({ type: "open_surface", surface: mode.surface });
      return;
    }
    const sessionId = id.startsWith("session-") ? id.slice("session-".length) : undefined;
    if (sessionId) onAction({ type: "select_session", sessionId });
  }

  function navigateHistory(delta: 1 | -1, liveDraft: string): string | undefined {
    const currentCursor = historyCursorRef.current;
    const nextCursor = currentCursor + delta;
    const stash = currentCursor === -1 ? liveDraft : historyStashRef.current;
    const text = promptHistoryText(promptHistoryRef.current, nextCursor, stash);
    if (text === undefined) return undefined;
    if (currentCursor === -1) historyStashRef.current = liveDraft;
    historyCursorRef.current = nextCursor;
    return text;
  }

  function submit(value: string): void {
    const text = value.trim();
    if (!text) return;
    setDraft("");
    historyCursorRef.current = -1;
    historyStashRef.current = "";
    if (text.startsWith("/")) {
      const [command, ...args] = text.slice(1).split(/\s+/);
      if (command === "help") {
        setStatusPanel(undefined);
        setComposerMenu(undefined);
        return openOverlay({ kind: "help" });
      }
      if (command === "mcp" || command === "model" || command === "agent" || command === "permissions" || command === "skill") {
        setStatusPanel(undefined);
        if (args.length === 0) {
          openComposerMenu(command, model, setComposerMenu, setMenuIndex, setMcpDetailId);
          if (command === "mcp") onAction({ type: "refresh_mcp" });
          if (command === "skill") onAction({ type: "refresh_skills" });
          return;
        }
        setComposerMenu(undefined);
        setMcpDetailId(undefined);
        onAction({ type: "slash_command", command, arguments: args.join(" ") });
        return;
      }
      const status = slashStatusPanel(command ?? "", model);
      if (status) {
        setStatusPanel(status);
        return;
      }
      setStatusPanel(undefined);
      setComposerMenu(undefined);
      setMcpDetailId(undefined);
      onAction({ type: "slash_command", command: command ?? "", arguments: args.join(" ") });
      return;
    }
    setStatusPanel(undefined);
    setComposerMenu(undefined);
    setMcpDetailId(undefined);
    promptHistoryRef.current = pushPromptHistory(promptHistoryRef.current, text);
    if (busy) {
      sendQueueRef.current = [...sendQueueRef.current, text];
      setSendQueue(sendQueueRef.current);
      return;
    }
    setLocalRunning(true);
    onAction({ type: "submit_prompt", text });
  }

  /**
   * `/` 选择栏和 Ctrl+K 面板执行命令的唯一出口。
   * 需要参数的命令只把草稿补全成 `/name `，不提交缺参数的动作。
   */
  function runCommand(command: string, takesArguments: boolean): void {
    if (takesArguments) {
      setStatusPanel(undefined);
      setDraft(`/${command} `);
      setOverlay({ kind: "none" });
      setFocus("composer");
      return;
    }
    setOverlay({ kind: "none" });
    setFocus("composer");
    submit(`/${command}`);
  }

  const main = useMemo(() => {
    if (model.surface === "home") return <HomeScreen model={model} selectedId={selectedPick?.id ?? HOME_MODES[0]!.id} />;
    if (model.surface === "team") return <TeamScreen teams={model.teams} />;
    if (model.surface === "automation") return <AutomationScreen automations={model.automations} />;
    if (model.surface === "doctor") return <DoctorScreen lines={model.doctorLines} />;
    return (
      <ChatScreen
        model={model}
        layout={layout}
        focus={focus}
        showInspector={showInspector}
        pendingPrompts={sendQueue}
      />
    );
  }, [focus, layout, model, selectedPick?.id, showInspector, sendQueue]);

  const overlayVisible = effectiveOverlay.kind !== "none";
  const settledSignature = model.timeline.items.map((item) => `${item.id}:${item.sequence}`).join("|");
  useEffect(() => {
    const extra: TimelineItem[] = [];
    const sessionKey = model.activeSessionId ?? "pending";
    for (const item of splitSettledTimeline(model.timeline.items).settled) {
      const key = `${sessionKey}:${item.id}:${item.sequence}`;
      if (seenStaticKeysRef.current.has(key)) continue;
      seenStaticKeysRef.current.add(key);
      extra.push(item);
    }
    if (extra.length === 0) return;
    staticItemsRef.current = [...staticItemsRef.current, ...extra];
    setStaticItems(staticItemsRef.current);
  }, [model.activeSessionId, model.timeline.items, settledSignature]);

  return (
    <Box
      flexDirection="column"
      minWidth={Math.min(columns, TERMINAL_DESIGN.layout.minimumColumns)}
    >
      <Static items={staticItems}>
        {(item) => (
          <Box key={`${item.id}-${item.sequence}`} flexDirection="column" paddingX={1} width="100%">
            <TimelineEntry item={item} expanded={false} />
          </Box>
        )}
      </Static>
      <GlobalBar model={model} />
      {model.error ? <ErrorBlock content={model.error} /> : null}
      {interrupt && dismissedInterruptId === interrupt.id ? <Text color={TERMINAL_DESIGN.colors.warning}>{TERMINAL_DESIGN.symbols.approval} 待处理输入仍存在 · {TERMINAL_DESIGN.copy.reviewPendingInput}</Text> : null}
      {overlayVisible ? (
        <Box justifyContent="center" paddingY={1}>
          <OverlayHost overlay={effectiveOverlay} model={model} onAction={onAction} onRunCommand={runCommand} onClose={closeOverlay} />
        </Box>
      ) : (
        <Box flexDirection="column">
          {main}
        </Box>
      )}
      {!overlayVisible ? (
        <Box flexDirection="column">
          <Composer
            value={draft}
            disabled={!model.connected}
            focused={focus === "composer" && effectiveOverlay.kind === "none"}
            captureDigits={modeKeysIdle}
            suppressKeys={slashOpen || composerMenuOpen}
            lockInput={composerMenuOpen}
            historyEnabled={!(model.surface === "home" && !draft)}
            onHistory={(delta, liveDraft) => navigateHistory(delta, liveDraft)}
            onChange={setDraft}
            onSubmit={submit}
          />
          {slashOpen ? (
            <SlashMenu items={slashMatches} selected={Math.min(slashIndex, slashMatches.length - 1)} query={query ?? ""} model={model} />
          ) : composerMenuOpen && composerMenu ? (
            <ComposerMenuPanel
              kind={composerMenu}
              model={model}
              selected={menuIndex}
              {...(mcpDetailId ? { detailId: mcpDetailId } : {})}
            />
          ) : (
            <>
              {statusPanel ? <StatusPanel panel={statusPanel} /> : null}
              <StatusBar model={model} queuedCount={sendQueue.length} />
            </>
          )}
        </Box>
      ) : null}
      {/* Home 内容可能高于视口，Ink 此时不会在帧尾追加换行。保留一个结构化空行，
          让终端光标坐标仍以完整输出为原点；输入框内部的 Y 无需任何补偿。 */}
      {model.surface === "home" && !overlayVisible ? <Text> </Text> : null}
    </Box>
  );
}

function OverlayHost({ overlay, model, onAction, onRunCommand, onClose }: {
  overlay: OverlayState;
  model: TerminalPageProps["model"];
  onAction: (action: TerminalPageAction) => void;
  onRunCommand: (command: string, takesArguments: boolean) => void;
  onClose: () => void;
}): React.ReactElement | null {
  if (overlay.kind === "commands") return <CommandPalette model={model} onRun={onRunCommand} onClose={onClose} />;
  if (overlay.kind === "sessions") return <SessionSwitcher sessions={model.sessions} activeSessionId={model.activeSessionId} onSelect={(sessionId) => onAction({ type: "select_session", sessionId })} onClose={onClose} />;
  if (overlay.kind === "help") return <HelpOverlay onClose={onClose} />;
  if (overlay.kind === "approval" && overlay.approval) return <ApprovalDialog approval={overlay.approval} onAnswer={(payload) => onAction({ type: "answer_interrupt", interruptId: overlay.approval!.id, payload })} onClose={onClose} />;
  if (overlay.kind === "question" && overlay.approval) return <UserQuestionDialog approval={overlay.approval} onSubmit={(answers) => onAction({ type: "answer_interrupt", interruptId: overlay.approval!.id, payload: { action: "answer", answers } })} onClose={onClose} />;
  return null;
}

function isUserQuestion(detail: Record<string, unknown>): boolean {
  return Array.isArray(detail.questions) || detail.kind === "user_question" || detail.type === "user_question";
}

function menuItems(kind: ComposerMenu, model: TerminalPageViewModel): RuntimeChoice[] {
  if (kind === "mcp") return model.runtime.mcpServers;
  if (kind === "skill") return model.runtime.skills;
  if (kind === "model") return model.runtime.models;
  if (kind === "agent") return model.runtime.agents;
  return PERMISSION_CHOICES;
}

function currentMenuIndex(kind: ComposerMenu, model: TerminalPageViewModel): number {
  const id = kind === "model"
    ? model.runtime.modelId
    : kind === "agent"
      ? model.runtime.agentKind
      : kind === "permissions"
        ? model.runtime.permissionMode
        : "";
  const items = menuItems(kind, model);
  const index = items.findIndex((item) => item.id === id);
  return index < 0 ? 0 : index;
}

function openComposerMenu(
  kind: ComposerMenu,
  model: TerminalPageViewModel,
  setComposerMenu: (kind: ComposerMenu) => void,
  setMenuIndex: (index: number) => void,
  setMcpDetailId: (id: string | undefined) => void,
): void {
  setComposerMenu(kind);
  setMenuIndex(currentMenuIndex(kind, model));
  setMcpDetailId(undefined);
}

function ComposerMenuPanel({ kind, model, selected, detailId }: {
  kind: ComposerMenu;
  model: TerminalPageViewModel;
  selected: number;
  detailId?: string;
}): React.ReactElement {
  if (kind === "mcp") {
    return (
      <McpMenu
        model={model}
        selected={Math.min(selected, Math.max(0, model.runtime.mcpServers.length - 1))}
        {...(detailId ? { detailId } : {})}
      />
    );
  }
  if (kind === "skill") {
    return <SkillMenu model={model} selected={Math.min(selected, Math.max(0, model.runtime.skills.length - 1))} />;
  }
  if (kind === "model") {
    return (
      <OptionMenu
        title="Model"
        items={model.runtime.models}
        selected={selected}
        currentId={model.runtime.modelId}
        hint="↑↓ 选择   Enter / Space 切换（下一轮生效）   Esc 关闭"
      />
    );
  }
  if (kind === "agent") {
    return (
      <OptionMenu
        title="Agent"
        items={model.runtime.agents}
        selected={selected}
        currentId={model.runtime.agentKind}
        hint="↑↓ 选择   Enter / Space 切换（下一轮生效）   Esc 关闭"
      />
    );
  }
  return (
    <OptionMenu
      title="Permissions"
      items={PERMISSION_CHOICES}
      selected={selected}
      currentId={model.runtime.permissionMode}
      hint="↑↓ 选择   Enter / Space 切换（下一轮生效）   Esc 关闭"
    />
  );
}
