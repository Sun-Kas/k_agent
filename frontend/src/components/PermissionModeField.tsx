import type { PermissionMode } from "../types";

type Props = {
  value: PermissionMode;
  onChange: (value: PermissionMode) => void;
  context: "team" | "scheduled";
};

/**
 * 高风险权限必须在创建入口显式选择，不能藏在 Agent/模型配置里。
 * 定时任务文案额外强调授权会随每次触发自动生效，避免把一次选择误解成一次性授权。
 */
export function PermissionModeField({ value, onChange, context }: Props) {
  const scheduled = context === "scheduled";
  return (
    <section className={`permission-mode-field ${value === "full_access" ? "full-access" : ""}`}>
      <span className="permission-mode-label">权限</span>
      <div className="permission-mode-options" role="radiogroup" aria-label="运行权限">
        <button type="button" role="radio" aria-checked={value === "default"} className={value === "default" ? "selected" : ""} onClick={() => onChange("default")}>
          <strong>默认权限</strong>
          <span>在沙箱内运行；超出范围时暂停并请你审批</span>
        </button>
        <button type="button" role="radio" aria-checked={value === "full_access"} className={value === "full_access" ? "selected danger" : ""} onClick={() => onChange("full_access")}>
          <strong>完全权限</strong>
          <span>关闭沙箱和审批，可访问宿主机文件与网络</span>
        </button>
      </div>
      {value === "full_access" && (
        <p className="permission-risk" role="alert">
          <b>高风险：</b>{scheduled
            ? "任务每次按计划无人值守运行时都会直接使用完全权限，可读取、修改或删除本机文件并访问网络，不会等待你确认。"
            : "团队中的 Agent 可直接读取、修改或删除工作空间外的本机文件并访问网络，不会等待你确认。"}
        </p>
      )}
    </section>
  );
}
