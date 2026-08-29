import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";
import { pendingApproval } from "../application/event-projector.js";
import { terminalLayout, TERMINAL_DESIGN } from "./design.js";
import { HOME_MODES, homePickItems, nextSurface, surfaceFromDigit } from "./home-catalog.js";
import { filterSlashCommands, slashCommandAvailability, slashQuery } from "./slash-catalog.js";
import type { OverlayState, TerminalFocus, TerminalPageAction, TerminalPageProps } from "./types.js";
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
import { ErrorBlock } from "./renderers/ErrorBlock.js";

/**
 * 交互式终端页面的唯一根组件。
 *
 * 这里可以改变区域和键盘表现，但禁止直接 fetch；Session 切换也只报告意图，
 * 不得因为布局变化而隐式 stop 或 cancel 服务端正在运行的任务。
 */
export function TerminalPage({ model, onAction }: TerminalPageProps): React.ReactElement {
  const { stdout } = useStdout();
  const layout = terminalLayout(stdout.columns ?? 80);
  const [draft, setDraft] = useState("");
  const [homePick, setHomePick] = useState(0);
  const [focus, setFocus] = useState<TerminalFocus>("composer");
  const [overlay, setOverlay] = useState<OverlayState>({ kind: "none" });
  const [showInspector, setShowInspector] = useState(false);
  const [dismissedInterruptId, setDismissedInterruptId] = useState<string>();
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const interrupt = pendingApproval(model.timeline);
  const interruptVisible = interrupt && interrupt.id !== dismissedInterruptId;
  const effectiveOverlay: OverlayState = interruptVisible
    ? { kind: isUserQuestion(interrupt.detail) ? "question" : "approval", approval: interrupt }
    : overlay;

  useEffect(() => {
    if (interrupt && interrupt.id !== dismissedInterruptId) setFocus("overlay");
  }, [interrupt, dismissedInterruptId]);

  const picks = homePickItems(model);
  const selectedPick = picks[Math.min(homePick, Math.max(0, picks.length - 1))];
  const query = slashQuery(draft);
  const slashMatches = useMemo(() => (query === undefined ? [] : filterSlashCommands(query)), [query]);
  const slashOpen = query !== undefined
    && !slashDismissed
    && slashMatches.length > 0
    && effectiveOverlay.kind === "none";
  const modeKeysIdle = effectiveOverlay.kind === "none" && !draft;

  // 草稿离开 `/` 状态时重置选择栏，避免下次打开时停在陈旧的高亮项。
  useEffect(() => {
    setSlashIndex(0);
    if (query === undefined) setSlashDismissed(false);
  }, [query]);

  useInput((input, key) => {
    if (effectiveOverlay.kind !== "none") return;
    if (key.ctrl && input === "k") return openOverlay("commands");
    if (key.ctrl && input === "p") return openOverlay("sessions");
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
    if (input === "?" && focus !== "composer") return openOverlay("help");
  });

  function openOverlay(kind: OverlayState["kind"]): void {
    setOverlay({ kind });
    setFocus("overlay");
  }

  function closeOverlay(): void {
    if (interruptVisible) setDismissedInterruptId(interrupt.id);
    else setOverlay({ kind: "none" });
    setFocus("composer");
  }

  function cycleFocus(direction: -1 | 1): void {
    const visible: TerminalFocus[] = layout === "wide"
      ? ["session-rail", "timeline", "inspector", "composer"]
      : layout === "standard"
        ? ["session-rail", "timeline", "composer"]
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

  function submit(value: string): void {
    const text = value.trim();
    if (!text) return;
    setDraft("");
    if (text.startsWith("/")) {
      const [command, ...args] = text.slice(1).split(/\s+/);
      // 帮助是纯页面能力，不需要往 application 层发一次无副作用的命令。
      if (command === "help") return openOverlay("help");
      onAction({ type: "slash_command", command: command ?? "", arguments: args.join(" ") });
    } else onAction({ type: "submit_prompt", text });
  }

  /**
   * `/` 选择栏和 Ctrl+K 面板执行命令的唯一出口。
   * 需要参数的命令只把草稿补全成 `/name `，不提交缺参数的动作。
   */
  function runCommand(command: string, takesArguments: boolean): void {
    if (takesArguments) {
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
    return <ChatScreen model={model} layout={layout} focus={focus} showInspector={showInspector} />;
  }, [focus, layout, model, selectedPick?.id, showInspector]);

  const overlayVisible = effectiveOverlay.kind !== "none";
  return (
    <Box flexDirection="column" minWidth={Math.min(stdout.columns ?? 80, TERMINAL_DESIGN.layout.minimumColumns)}>
      <GlobalBar model={model} />
      {model.error ? <ErrorBlock content={model.error} /> : null}
      {interrupt && dismissedInterruptId === interrupt.id ? <Text color={TERMINAL_DESIGN.colors.warning}>{TERMINAL_DESIGN.symbols.approval} 待处理输入仍存在 · {TERMINAL_DESIGN.copy.reviewPendingInput}</Text> : null}
      {overlayVisible ? (
        <Box flexGrow={1} justifyContent="center" alignItems="center">
          <OverlayHost overlay={effectiveOverlay} model={model} onAction={onAction} onRunCommand={runCommand} onClose={closeOverlay} />
        </Box>
      ) : main}
      <StatusBar model={model} />
      {!overlayVisible ? (
        <Box flexDirection="column">
          <Composer
            value={draft}
            disabled={!model.connected || model.timeline.runStatus === "running"}
            focused={focus === "composer" && effectiveOverlay.kind === "none"}
            captureDigits={modeKeysIdle}
            suppressKeys={slashOpen}
            onChange={setDraft}
            onSubmit={submit}
          />
          {slashOpen ? (
            <SlashMenu items={slashMatches} selected={Math.min(slashIndex, slashMatches.length - 1)} query={query ?? ""} model={model} />
          ) : (
            <Box paddingX={1}>
              <Text color={TERMINAL_DESIGN.colors.muted}>{TERMINAL_DESIGN.copy.composerKeys}</Text>
            </Box>
          )}
        </Box>
      ) : null}
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
