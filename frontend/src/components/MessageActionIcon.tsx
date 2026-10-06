/** 消息操作图标：同一套 16px 描边，避免用字符充当按钮。 */

type MessageActionIconName = "speak" | "stop" | "copy" | "copied" | "fork";

export function MessageActionIcon({ name }: { name: MessageActionIconName }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {name === "speak" && (
        <>
          <path d="M2.75 6.2h2.05L7.9 3.45v9.1L4.8 9.8H2.75V6.2Z" />
          <path d="M10.05 6.05a2.4 2.4 0 0 1 0 3.9" />
          <path d="M11.65 4.45a4.35 4.35 0 0 1 0 7.1" />
        </>
      )}
      {name === "stop" && <rect x="4.15" y="4.15" width="7.7" height="7.7" rx="1.3" />}
      {name === "copy" && (
        <>
          <rect x="5.6" y="5.6" width="7" height="7" rx="1.3" />
          <path d="M10.4 5.6V4.3A1.3 1.3 0 0 0 9.1 3H4.3A1.3 1.3 0 0 0 3 4.3v4.8A1.3 1.3 0 0 0 4.3 10.4H5.6" />
        </>
      )}
      {name === "copied" && <path d="M3.4 8.15 6.35 11.15 12.6 4.7" />}
      {name === "fork" && (
        <>
          <circle cx="4" cy="3.2" r="1.35" />
          <circle cx="4" cy="12.8" r="1.35" />
          <circle cx="12.1" cy="6.4" r="1.35" />
          <path d="M4 4.55v6.9" />
          <path d="M4 8.05c0-1.35 1.2-1.65 6.75-1.65" />
        </>
      )}
    </svg>
  );
}
