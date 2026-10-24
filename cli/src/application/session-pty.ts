import { randomUUID } from "node:crypto";

interface PtyTab { id: string; sessionId: string; socket: WebSocket; chunks: Buffer[]; bytes: number; closed: boolean }
export interface PtyStreams { stdin: NodeJS.ReadStream; stdout: NodeJS.WriteStream }

/**
 * PTY 内容来自用户主动打开的 shell，需原样交给宿主终端解释 VT 序列。
 * 它与模型/工具输出的净化渲染严格分离；调用前必须通过 Ink suspendTerminal 交还终端。
 */
export class SessionPtyHub {
  private tabs = new Map<string, PtyTab>();
  private attached: PtyTab | undefined;
  private output: NodeJS.WriteStream | undefined;
  constructor(private endpoint: string, private connect: (url: string) => WebSocket = (url) => new WebSocket(url)) {}

  private open(sessionId: string, id: string): PtyTab {
    const key = `${sessionId}:${id}`;
    const existing = this.tabs.get(key);
    if (existing && !existing.closed) return existing;
    const url = new URL(this.endpoint);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = url.pathname.replace(/\/$/, "") + `/api/sessions/${encodeURIComponent(sessionId)}/terminal/${encodeURIComponent(id)}`;
    const tab: PtyTab = { id, sessionId, socket: this.connect(url.toString()), chunks: [], bytes: 0, closed: false };
    this.tabs.set(key, tab);
    tab.socket.addEventListener("message", (event) => {
      try {
        const frame = JSON.parse(String(event.data)) as { type: string; data?: string };
        if (frame.type !== "output" || !frame.data) return;
        const bytes = Buffer.from(frame.data, "base64");
        tab.chunks.push(bytes); tab.bytes += bytes.length;
        while (tab.bytes > 4 * 1024 * 1024 && tab.chunks.length > 1) tab.bytes -= tab.chunks.shift()!.length;
        if (this.attached === tab) this.output?.write(bytes);
      } catch { /* 畸形 WebSocket 帧不改变终端所有权。 */ }
    });
    tab.socket.addEventListener("close", () => { tab.closed = true; });
    return tab;
  }

  async attach(sessionId: string, terminalId: string, streams: PtyStreams = { stdin: process.stdin, stdout: process.stdout }): Promise<{ terminalId: string; running: boolean }> {
    const { stdin, stdout } = streams;
    if (!stdin.isTTY || !stdout.isTTY) throw new Error("会话 PTY 需要真实 TTY");
    let tab = this.open(sessionId, terminalId);
    const oldRaw = stdin.isRaw;
    const resize = () => {
      if (tab.socket.readyState === WebSocket.OPEN) tab.socket.send(JSON.stringify({ type: "resize", cols: Math.max(2, Math.min(500, stdout.columns || 80)), rows: Math.max(2, Math.min(200, stdout.rows || 24)) }));
    };
    const select = (next: PtyTab) => {
      tab = next; this.attached = tab; this.output = stdout;
      stdout.write("\u001b[0m\u001b[2J\u001b[H");
      stdout.write(`K-Agent PTY ${tab.id} · Ctrl+] 返回 · Alt+T 新标签 · Alt+N/P 切换 · Alt+Q 关闭\r\n`);
      for (const chunk of tab.chunks) stdout.write(chunk);
      resize();
    };
    stdin.setRawMode(true);
    stdin.resume();
    try {
      await new Promise<void>((resolve, reject) => {
        let timer: ReturnType<typeof setTimeout> | undefined;
        const listeners: Array<{ tab: PtyTab; type: string; listener: EventListener }> = [];
        const cleanup = () => {
          if (timer) clearTimeout(timer); stdin.off("data", input); stdout.off("resize", resize);
          for (const item of listeners) item.tab.socket.removeEventListener(item.type, item.listener);
        };
        const finish = () => { cleanup(); resolve(); };
        const watch = (target: PtyTab) => {
          const handlers: Record<string, EventListener> = {
            open: () => { if (tab === target) resize(); if (timer) clearTimeout(timer); },
            error: () => { if (tab === target) { cleanup(); reject(new Error("会话终端连接失败")); } },
            close: (event) => {
              if (tab !== target) return;
              const code = (event as CloseEvent).code;
              if (code === 4409 || code === 4404 || code === 1006) { cleanup(); reject(new Error(code === 4409 ? "会话终端标签已达服务端上限" : "会话终端连接已失效")); } else finish();
            },
          };
          for (const [type, listener] of Object.entries(handlers)) { target.socket.addEventListener(type, listener, { once: true }); listeners.push({ tab: target, type, listener }); }
          if (timer) clearTimeout(timer);
          if (target.socket.readyState !== WebSocket.OPEN) timer = setTimeout(() => { cleanup(); reject(new Error("连接会话 PTY 超时")); }, 10_000);
        };
        const input = (data: Buffer | string) => {
          const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data);
          if (bytes.equals(Buffer.from([0x1d]))) return finish();
          if (bytes.equals(Buffer.from([0x1b, 0x74]))) { const next = this.open(sessionId, `cli-${randomUUID().slice(0, 8)}`); select(next); watch(next); return; }
          if (bytes.equals(Buffer.from([0x1b, 0x6e])) || bytes.equals(Buffer.from([0x1b, 0x70]))) {
            const tabs = [...this.tabs.values()].filter((item) => item.sessionId === sessionId && !item.closed);
            const index = tabs.indexOf(tab); const delta = bytes[1] === 0x6e ? 1 : -1;
            const next = tabs[(index + tabs.length + delta) % tabs.length];
            if (next) { select(next); watch(next); }
            return;
          }
          if (bytes.equals(Buffer.from([0x1b, 0x71]))) { if (tab.socket.readyState === WebSocket.OPEN) tab.socket.send(JSON.stringify({ type: "close" })); tab.socket.close(); return finish(); }
          if (tab.socket.readyState === WebSocket.OPEN) tab.socket.send(JSON.stringify({ type: "input", data: bytes.toString("base64") }));
        };
        select(tab); watch(tab);
        stdin.on("data", input); stdout.on("resize", resize);
      });
    } finally {
      this.attached = undefined; this.output = undefined;
      stdin.setRawMode(oldRaw ?? false);
      // 远端仍在 vim 等全屏程序中时，也要恢复 CLI 的屏幕、鼠标与光标状态。
      stdout.write("\u001b[?1049l\u001b[?1000l\u001b[?1002l\u001b[?1003l\u001b[?1006l\u001b[?2004l\u001b[?25h\u001b[0m");
    }
    return { terminalId: tab.id, running: !tab.closed && tab.socket.readyState === WebSocket.OPEN };
  }
  closeAll(): void { for (const tab of this.tabs.values()) tab.socket.close(); this.tabs.clear(); }
}
