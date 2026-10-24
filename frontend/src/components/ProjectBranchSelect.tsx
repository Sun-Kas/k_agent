import { useEffect, useId, useRef, useState } from "react";

/** 分支菜单留在编辑弹窗内，让点击外部、Escape 和焦点归还沿用同一层级。 */
export function ProjectBranchSelect({ value, branches, disabled, onChange }: {
  value: string;
  branches: string[];
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const options = ["", ...new Set([...branches, ...(value ? [value] : [])])];
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus();
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", dismiss);
    return () => window.removeEventListener("pointerdown", dismiss);
  }, [open]);
  return <div className="project-branch-select" ref={root} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }}>
    <button ref={trigger} type="button" className="project-branch-trigger" disabled={disabled}
      aria-label="统一目标分支" aria-haspopup="listbox" aria-expanded={open} aria-controls={id}
      onClick={() => setOpen(!open)} onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); }
      }}>
      <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="6" cy="4" r="2"/><circle cx="6" cy="16" r="2"/><circle cx="14" cy="5" r="2"/><path d="M6 6v8m8-7v1a5 5 0 0 1-5 5H6"/></svg>
      <span>{value || "跟随当前分支"}</span>
      <svg className="project-branch-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>
    </button>
    {open && <div id={id} role="listbox" aria-label="统一目标分支" className="project-branch-menu"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus();
        }
        const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
        if (!keys.includes(event.key)) return;
        event.preventDefault();
        const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]'));
        const current = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }}>
      {options.map((branch) => <button key={branch} type="button" role="option" tabIndex={-1}
        aria-selected={value === branch} onClick={() => { onChange(branch); setOpen(false); trigger.current?.focus(); }}>
        <span>{branch || "跟随当前分支"}</span>
        {branch === value && <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>}
      </button>)}
    </div>}
  </div>;
}
