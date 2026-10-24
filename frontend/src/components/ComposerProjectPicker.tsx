import { useEffect, useRef, useState } from "react";
import type { ProjectSummary } from "../types";

export function ComposerProjectPicker({ projects, value, disabled, onChange, onCreate }: {
  projects: ProjectSummary[]; value: string | null; disabled: boolean;
  onChange: (projectId: string | null) => void; onCreate: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const project = projects.find((item) => item.id === value);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    window.addEventListener("pointerdown", dismiss);
    return () => window.removeEventListener("pointerdown", dismiss);
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  function choose(id: string | null) { onChange(id); setOpen(false); trigger.current?.focus(); }
  return <div className="composer-project" ref={root} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }} onKeyDown={(event) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
  }}>
    <button ref={trigger} type="button" className="composer-project-trigger" aria-label={`项目归属：${project?.name || "无项目"}`} aria-expanded={open} disabled={disabled}
      title={disabled ? "项目归属在新建对话时选择" : "选择新对话所属的项目"} onClick={() => setOpen(!open)}>
      <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 6V4.5h5L9 6h8.5v10h-15V6Zm0 2h15"/></svg><span>{project?.name || "无项目"}</span>
      {!disabled && <svg className="trigger-chevron" viewBox="0 0 12 12" aria-hidden="true"><path d="m3 4.75 3 3 3-3"/></svg>}
    </button>
    {open && <section className="composer-project-popover" aria-label="选择项目归属">
      <header>项目归属</header>
      <div className="composer-project-options">
        <button type="button" className={!value ? "selected" : ""} onClick={() => choose(null)}><span>无项目</span>{!value && <i aria-hidden="true">✓</i>}</button>
        {projects.map((item) => <button type="button" className={value === item.id ? "selected" : ""} key={item.id} onClick={() => choose(item.id)} title={item.rootPath}><span>{item.name}</span>{value === item.id && <i aria-hidden="true">✓</i>}</button>)}
      </div>
      <button className="composer-project-create" type="button" onClick={() => { setOpen(false); onCreate(); }}>＋ 新建项目</button>
    </section>}
  </div>;
}
