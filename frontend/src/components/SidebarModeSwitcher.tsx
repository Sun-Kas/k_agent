import { useEffect, useRef, useState } from "react";

export function SidebarModeSwitcher({ mode = "chat", onChange }: { mode?: "chat" | "team"; onChange: (mode: "chat" | "team") => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", dismiss);
    return () => window.removeEventListener("pointerdown", dismiss);
  }, [open]);
  return <div className="sidebar-mode" ref={root} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }} onKeyDown={(event) => {
    if (event.key === "Escape") { event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
  }}>
    <button ref={trigger} type="button" className="sidebar-mode-trigger" aria-label="切换工作模式" aria-expanded={open}
      onClick={() => setOpen(!open)}>
      {mode === "team" ? <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8" cy="6" r="3"/><path d="M2 17v-2a5 5 0 0 1 10 0v2m1-14a3 3 0 0 1 0 6m1 3a4 4 0 0 1 4 4v1"/></svg> : <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 5 4 4-4 4m6 1h6" /></svg>}
      <span>{mode === "team" ? "Agent Team" : "Work"}</span>
      <svg className="sidebar-mode-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4" /></svg>
    </button>
    {open && <div className="sidebar-mode-options" aria-label="工作模式">
      <button type="button" onClick={() => { setOpen(false); onChange("chat"); trigger.current?.focus(); }}>
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 5 4 4-4 4m6 1h6" /></svg><span>Work</span>{mode === "chat" && <span aria-hidden="true">✓</span>}
      </button>
      <button type="button" onClick={() => { setOpen(false); onChange("team"); }}>
        <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8" cy="6" r="3"/><path d="M2 17v-2a5 5 0 0 1 10 0v2m1-14a3 3 0 0 1 0 6m1 3a4 4 0 0 1 4 4v1"/></svg><span>Agent Team</span>{mode === "team" && <span aria-hidden="true">✓</span>}
      </button>
    </div>}
  </div>;
}
