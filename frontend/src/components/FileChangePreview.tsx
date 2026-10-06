import type { DiffLine, FileChangePreviewModel } from "../tools/file-change-preview";

export function FileChangePreview({ change }: { change: FileChangePreviewModel }) {
  const numbered = change.hunks.some((hunk) => hunk.oldStart > 0 || hunk.newStart > 0);
  return (
    <div className="file-change-preview">
      <header>
        <code title={change.path}>{change.path}</code>
        <span>
          <b>+{change.added}</b>
          <b>−{change.removed}</b>
        </span>
        {change.subtitle && <small>{change.subtitle}</small>}
      </header>
      {change.hunks.map((hunk, hunkIndex) => (
        <pre className={`file-change-lines ${numbered ? "numbered" : ""}`} key={`${hunk.oldStart}-${hunk.newStart}-${hunkIndex}`}>
          {hunk.lines.map((line, index) => (
            <span className={`file-change-line ${line.kind}`} key={`${index}-${line.kind}-${line.oldLine ?? 0}-${line.newLine ?? 0}`}>
              {numbered && <b>{displayLineNumber(line)}</b>}
              <i>{line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}</i>
              <em>{line.text || " "}</em>
            </span>
          ))}
        </pre>
      ))}
      {change.truncated && <span className="file-change-truncated">后续行已截断</span>}
    </div>
  );
}

function displayLineNumber(line: DiffLine): number | "" {
  if (line.kind === "del") return line.oldLine ?? "";
  return line.newLine ?? line.oldLine ?? "";
}
