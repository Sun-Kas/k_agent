import { useEffect, useRef, useState, type CSSProperties } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

import { appConfig } from "../config";

const MAX_TABS = 8;

type TerminalTab = {
  id: string;
  shell: string;
};

function bytesToBase64(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function base64ToBytes(value: string) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function terminalSocketUrl(sessionId: string, terminalId: string) {
  const httpBase = appConfig.apiBaseUrl || window.location.origin;
  const wsBase = httpBase.replace(/^http/, "ws");
  return `${wsBase}/api/sessions/${encodeURIComponent(sessionId)}/terminal/${encodeURIComponent(terminalId)}`;
}

function newTab(): TerminalTab {
  return { id: crypto.randomUUID(), shell: "zsh" };
}

function TerminalGlyph() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.2" />
      <path d="M4 6.1 6.1 8 4 9.9M7.3 10.2h4.2" />
    </svg>
  );
}

function TerminalPane({
  sessionId,
  terminalId,
  active,
  open,
  height,
  takeFocus,
  onShell
}: {
  sessionId: string;
  terminalId: string;
  active: boolean;
  open: boolean;
  height: number;
  takeFocus: boolean;
  onShell: (terminalId: string, shell: string) => void;
}) {
  const screenRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const onShellRef = useRef(onShell);
  onShellRef.current = onShell;

  useEffect(() => {
    const host = screenRef.current;
    if (!host) return;
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 13,
      theme: {
        background: "#1a1a1a",
        foreground: "#cccccc",
        cursor: "#cccccc",
        selectionBackground: "#264f78"
      }
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    const previousFocus = document.activeElement;
    term.open(host);
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
    else term.blur();
    termRef.current = term;
    fitRef.current = fit;
    term.onData((data) => {
      const socket = socketRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const bytes = new TextEncoder().encode(data);
      socket.send(JSON.stringify({ type: "input", data: bytesToBase64(bytes) }));
    });
    const socket = new WebSocket(terminalSocketUrl(sessionId, terminalId));
    socketRef.current = socket;
    const fitSoon = () => {
      if (!active) return;
      fit.fit();
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
    };
    socket.addEventListener("open", () => window.requestAnimationFrame(fitSoon));
    socket.addEventListener("message", (event) => {
      const payload = JSON.parse(String(event.data)) as { type?: string; data?: string; shell?: string };
      if (payload.type === "ready") {
        onShellRef.current(terminalId, payload.shell || "zsh");
        window.requestAnimationFrame(fitSoon);
        return;
      }
      if (payload.type === "output" && payload.data) term.write(base64ToBytes(payload.data));
    });
    socket.addEventListener("close", () => {
      if (socketRef.current === socket) socketRef.current = null;
    });
    return () => {
      socket.close();
      socketRef.current = null;
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
    // 连接跟这块标签走。收起底栏或切到别的标签都不能拆掉它。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, terminalId]);

  useEffect(() => {
    if (!active || !open) return;
    const frame = window.requestAnimationFrame(() => {
      const addon = fitRef.current;
      const term = termRef.current;
      const socket = socketRef.current;
      if (!addon || !term) return;
      addon.fit();
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
      if (takeFocus) term.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [active, open, height, takeFocus]);

  return <div ref={screenRef} className={`conversation-terminal-screen ${active ? "active" : ""}`} />;
}

export function ConversationTerminal({
  sessionId,
  open,
  height,
  onClose
}: {
  sessionId: string | null;
  open: boolean;
  height: number;
  onClose: () => void;
}) {
  const [tabs, setTabs] = useState<TerminalTab[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !sessionId || tabs.length > 0) return;
    const tab = newTab();
    setTabs([tab]);
    setActiveId(tab.id);
  }, [open, sessionId, tabs.length]);

  function addTab() {
    if (!sessionId || tabs.length >= MAX_TABS) return;
    const tab = newTab();
    setTabs((current) => [...current, tab]);
    setActiveId(tab.id);
    setFocusId(tab.id);
  }

  function closeTab(id: string) {
    const remaining = tabs.filter((tab) => tab.id !== id);
    setTabs(remaining);
    if (focusId === id) setFocusId(null);
    if (remaining.length === 0) {
      setActiveId(null);
      onClose();
      return;
    }
    if (activeId === id) setActiveId(remaining[remaining.length - 1].id);
  }

  function rename(id: string, shell: string) {
    setTabs((current) => current.map((tab) => (tab.id === id && tab.shell !== shell ? { ...tab, shell } : tab)));
  }

  return (
    <section
      className={`conversation-terminal ${open ? "open" : ""}`}
      style={{ "--terminal-height": `${height}px` } as CSSProperties}
      aria-label="终端"
      aria-hidden={!open}
    >
      <div className="conversation-terminal-tabs" role="tablist" aria-label="终端标签">
        {tabs.map((tab) => (
          <div key={tab.id} className={`conversation-terminal-tab ${tab.id === activeId ? "active" : ""}`} role="tab" aria-selected={tab.id === activeId}>
            <button type="button" className="conversation-terminal-tab-main" onClick={() => { setActiveId(tab.id); setFocusId(tab.id); }}>
              <TerminalGlyph />
              <span>{tab.shell}</span>
            </button>
            <button type="button" className="conversation-terminal-tab-close" aria-label={`关闭 ${tab.shell}`} onClick={() => closeTab(tab.id)}>
              ×
            </button>
          </div>
        ))}
        <button type="button" className="conversation-terminal-add" aria-label="新建终端" onClick={addTab} disabled={!sessionId || tabs.length >= MAX_TABS}>
          +
        </button>
        <button type="button" className="conversation-terminal-panel-close" aria-label="关闭底部终端" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="conversation-terminal-body">
        {!sessionId && <p className="conversation-terminal-empty">先打开或创建会话</p>}
        {sessionId && tabs.map((tab) => (
          <TerminalPane
            key={tab.id}
            sessionId={sessionId}
            terminalId={tab.id}
            active={tab.id === activeId}
            open={open}
            height={height}
            takeFocus={tab.id === focusId}
            onShell={rename}
          />
        ))}
      </div>
    </section>
  );
}
