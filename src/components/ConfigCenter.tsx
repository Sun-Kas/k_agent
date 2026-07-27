import { FormEvent, useEffect, useState } from "react";
import {
  getMcpConfig,
  getModelsConfig,
  getSkillsConfig,
  saveMcpConfig,
  saveModelsConfig,
  saveSkillsConfig
} from "../api/agui";
import type { McpServerConfig, ModelProfile, SkillConfig } from "../types";

type ConfigTab = "model" | "mcp" | "skills";

export function ConfigCenter({ onBack }: { onBack: () => void }) {
  const [tab, setTab] = useState<ConfigTab>("model");
  const [modelsPath, setModelsPath] = useState("");
  const [modelsSource, setModelsSource] = useState("");
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [mcpPath, setMcpPath] = useState("");
  const [mcpSource, setMcpSource] = useState("");
  const [mcpIsTemplate, setMcpIsTemplate] = useState(false);
  const [servers, setServers] = useState<McpServerConfig[]>([]);
  const [skillsPath, setSkillsPath] = useState("");
  const [skills, setSkills] = useState<SkillConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let pending = 3;
    const complete = () => {
      pending -= 1;
      if (pending === 0) setLoading(false);
    };
    void getModelsConfig()
      .then((modelData) => {
        setModelsPath(modelData.path);
        setModelsSource(modelData.source);
        setModels(modelData.models);
      })
      .catch((error: Error) => setNotice((current) => current || `模型配置加载失败：${error.message}`))
      .finally(complete);
    void getMcpConfig()
      .then((mcpData) => {
        setMcpPath(mcpData.path);
        setMcpSource(mcpData.source ?? mcpData.path);
        setMcpIsTemplate(Boolean(mcpData.isTemplate));
        setServers(mcpData.servers);
      })
      .catch((error: Error) => setNotice((current) => current || `MCP 配置加载失败：${error.message}`))
      .finally(complete);
    void getSkillsConfig()
      .then((skillsData) => {
        setSkillsPath(skillsData.path);
        setSkills(skillsData.skills);
      })
      .catch((error: Error) => setNotice((current) => current || `Skills 配置加载失败：${error.message}`))
      .finally(complete);
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setNotice("");
    try {
      if (tab === "model") {
        await saveModelsConfig(models);
        setModels((current) => current.map((model) => ({
          ...model,
          apiKey: "",
          apiKeyConfigured: model.apiKeyConfigured || Boolean(model.apiKey)
        })));
        setNotice("模型配置已保存，可立即在对话中选择");
      } else if (tab === "mcp") {
        await saveMcpConfig(servers);
        setNotice("MCP 配置已保存，请重启后端以重新连接服务");
      } else {
        await saveSkillsConfig(skills);
        setNotice("Skills 已保存，并已即时应用到 Agent");
      }
    } catch (error) {
      setNotice(`保存失败：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="config-page">
      <header className="config-topbar">
        <button type="button" onClick={onBack}>← 返回对话</button>
        <div><p className="eyebrow">AGENT CONTROL CENTER</p><h1>配置中心</h1></div>
        <span>{loading ? "正在加载" : "配置已同步"}</span>
      </header>
      <div className="config-layout">
        <nav className="config-nav" aria-label="配置分类">
          <p>Agent 设置</p>
          <button className={tab === "model" ? "active" : ""} type="button" onClick={() => { setTab("model"); setNotice(""); }}>
            <i>AI</i><span><strong>模型管理</strong><small>多模型与能力配置</small></span><b>{models.length}</b>
          </button>
          <button className={tab === "mcp" ? "active" : ""} type="button" onClick={() => { setTab("mcp"); setNotice(""); }}>
            <i>⌁</i><span><strong>MCP 服务</strong><small>外部工具与连接器</small></span><b>{servers.length}</b>
          </button>
          <button className={tab === "skills" ? "active" : ""} type="button" onClick={() => { setTab("skills"); setNotice(""); }}>
            <i>✦</i><span><strong>Skills</strong><small>可复用的专业指令</small></span><b>{skills.length}</b>
          </button>
          <div className="config-note"><strong>安全提示</strong><p>密钥不会回传到浏览器。MCP 环境变量只显示遮蔽值。</p></div>
        </nav>

        <form className="config-content" onSubmit={save}>
          {loading ? <div className="config-loading">正在读取配置…</div> : (
            <>
              {tab === "model" && (
                <section className="config-section">
                  <ConfigHeading eyebrow="MODEL PROFILES" title="模型管理" copy="配置多个 OpenAI 兼容模型，并声明图像输入和推理能力。" />
                  <div className="path-bar"><span>配置文件</span><code>{modelsSource || modelsPath}</code></div>
                  <div className="config-cards">
                    {models.map((model, index) => (
                      <ModelCard key={`${model.id}-${index}`} model={model} onChange={(next) => setModels(models.map((item, i) => i === index ? next : item))} onRemove={() => setModels(models.filter((_, i) => i !== index))} />
                    ))}
                    <button className="add-config" type="button" onClick={() => setModels([...models, { id: `model-${models.length + 1}`, name: "新模型", model: "", baseUrl: "https://api.openai.com/v1", apiKeyConfigured: false, apiKey: "", apiKeyEnv: "OPENAI_API_KEY", multimodal: false, supportsReasoning: false, enabled: true }])}>
                      <span>＋</span><strong>添加模型</strong><small>配置 URL、模型名、密钥和能力</small>
                    </button>
                  </div>
                </section>
              )}

              {tab === "mcp" && (
                <section className="config-section">
                  <ConfigHeading eyebrow="MODEL CONTEXT PROTOCOL" title="MCP 服务" copy="管理 Agent 可调用的外部工具服务。配置写入后需重启后端建立新连接。" />
                  <div className="path-bar"><span>当前来源</span><code>{mcpSource || mcpPath}</code>{mcpIsTemplate && <b>示例配置 · 保存后写入 {mcpPath}</b>}</div>
                  <div className="config-cards">
                    {servers.map((server, index) => (
                      <McpCard key={`${server.id}-${index}`} server={server} onChange={(next) => setServers(servers.map((item, i) => i === index ? next : item))} onRemove={() => setServers(servers.filter((_, i) => i !== index))} />
                    ))}
                    <button className="add-config" type="button" onClick={() => setServers([...servers, { id: `server-${servers.length + 1}`, command: "npx", args: [], env: {}, enabled: true }])}>
                      <span>＋</span><strong>添加 MCP 服务</strong><small>配置 stdio 命令、参数和环境变量</small>
                    </button>
                  </div>
                </section>
              )}

              {tab === "skills" && (
                <section className="config-section">
                  <ConfigHeading eyebrow="REUSABLE INSTRUCTIONS" title="Skills" copy="把稳定的专业流程保存为 Skill。启用后，它们会自动加入 Agent 的系统上下文。" />
                  <div className="path-bar"><span>配置文件</span><code>{skillsPath}</code></div>
                  <div className="config-cards">
                    {skills.map((skill, index) => (
                      <SkillCard key={`${skill.id}-${index}`} skill={skill} onChange={(next) => setSkills(skills.map((item, i) => i === index ? next : item))} onRemove={() => setSkills(skills.filter((_, i) => i !== index))} />
                    ))}
                    <button className="add-config" type="button" onClick={() => setSkills([...skills, { id: `skill-${skills.length + 1}`, name: "新 Skill", description: "", instructions: "", enabled: true }])}>
                      <span>＋</span><strong>创建 Skill</strong><small>添加一组可复用的 Agent 专业指令</small>
                    </button>
                  </div>
                </section>
              )}
              <footer className="config-footer">
                <p className={notice.startsWith("保存失败") || notice.startsWith("加载失败") ? "error" : ""}>{notice}</p>
                <button type="submit" disabled={saving}>{saving ? "保存中…" : "保存更改"}</button>
              </footer>
            </>
          )}
        </form>
      </div>
    </main>
  );
}

function ConfigHeading({ eyebrow, title, copy }: { eyebrow: string; title: string; copy: string }) {
  return <header className="config-heading"><p className="eyebrow">{eyebrow}</p><h2>{title}</h2><p>{copy}</p></header>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return <label className="config-field"><span><strong>{label}</strong>{hint && <small>{hint}</small>}</span>{children}</label>;
}

function ModelCard({ model, onChange, onRemove }: { model: ModelProfile; onChange: (model: ModelProfile) => void; onRemove: () => void }) {
  return (
    <article className="config-card model-card">
      <header>
        <div className="config-card-icon">AI</div>
        <div><input aria-label="模型显示名称" value={model.name} onChange={(e) => onChange({ ...model, name: e.target.value })} /><small>{model.id}</small></div>
        <Toggle checked={model.enabled} onChange={(enabled) => onChange({ ...model, enabled })} />
        <button className="remove-button" type="button" onClick={onRemove}>×</button>
      </header>
      <div className="field-grid two">
        <Field label="模型 ID" hint="请求时发送的 model"><input required value={model.model} onChange={(e) => onChange({ ...model, model: e.target.value })} placeholder="gpt-4.1" /></Field>
        <Field label="配置 ID" hint="用于会话选择"><input required value={model.id} onChange={(e) => onChange({ ...model, id: e.target.value })} /></Field>
      </div>
      <Field label="API Base URL" hint="OpenAI 兼容接口地址"><input type="url" required value={model.baseUrl} onChange={(e) => onChange({ ...model, baseUrl: e.target.value })} /></Field>
      <Field label="API Key" hint={model.apiKeyConfigured ? "已配置；留空保持不变" : "尚未配置"}>
        <input type="password" autoComplete="new-password" value={model.apiKey ?? ""} onChange={(e) => onChange({ ...model, apiKey: e.target.value })} placeholder={model.apiKeyConfigured ? "••••••••••••••••" : "sk-…"} />
      </Field>
      <Field label="API Key 环境变量" hint="推荐通过环境变量安全引用">
        <input value={model.apiKeyEnv ?? ""} onChange={(e) => onChange({ ...model, apiKeyEnv: e.target.value })} placeholder="OPENAI_API_KEY" />
      </Field>
      <div className="capability-switches">
        <label><span><strong>多模态输入</strong><small>允许在对话中上传图片</small></span><Toggle checked={model.multimodal} onChange={(multimodal) => onChange({ ...model, multimodal })} /></label>
        <label><span><strong>思考强度</strong><small>支持 reasoning_effort 参数</small></span><Toggle checked={model.supportsReasoning} onChange={(supportsReasoning) => onChange({ ...model, supportsReasoning })} /></label>
      </div>
    </article>
  );
}

function McpCard({ server, onChange, onRemove }: { server: McpServerConfig; onChange: (server: McpServerConfig) => void; onRemove: () => void }) {
  const envText = Object.entries(server.env).map(([key, value]) => `${key}=${value}`).join("\n");
  return (
    <article className="config-card">
      <header><div className="config-card-icon">⌁</div><div><input aria-label="MCP ID" value={server.id} onChange={(e) => onChange({ ...server, id: e.target.value })} /><small>{server.connected ? "当前已连接" : "等待连接"}</small></div><Toggle checked={server.enabled} onChange={(enabled) => onChange({ ...server, enabled })} /><button className="remove-button" type="button" onClick={onRemove}>×</button></header>
      <div className="field-grid two">
        <Field label="启动命令"><input value={server.command} onChange={(e) => onChange({ ...server, command: e.target.value })} /></Field>
        <Field label="命令参数" hint="每行一个"><textarea rows={3} value={server.args.join("\n")} onChange={(e) => onChange({ ...server, args: e.target.value.split("\n").filter(Boolean) })} /></Field>
      </div>
      <Field label="环境变量" hint="KEY=VALUE，每行一个；*** 表示保留现有值">
        <textarea rows={3} value={envText} onChange={(e) => onChange({ ...server, env: parseEnv(e.target.value) })} placeholder="API_KEY=…" />
      </Field>
    </article>
  );
}

function SkillCard({ skill, onChange, onRemove }: { skill: SkillConfig; onChange: (skill: SkillConfig) => void; onRemove: () => void }) {
  return (
    <article className="config-card skill-card">
      <header><div className="config-card-icon">✦</div><div><input aria-label="Skill 名称" value={skill.name} onChange={(e) => onChange({ ...skill, name: e.target.value })} /><small>{skill.id}</small></div><Toggle checked={skill.enabled} onChange={(enabled) => onChange({ ...skill, enabled })} /><button className="remove-button" type="button" onClick={onRemove}>×</button></header>
      <Field label="简介"><input value={skill.description} onChange={(e) => onChange({ ...skill, description: e.target.value })} placeholder="这个 Skill 擅长什么？" /></Field>
      <Field label="Skill 指令" hint="启用后会注入系统上下文">
        <textarea className="config-editor" rows={8} value={skill.instructions} onChange={(e) => onChange({ ...skill, instructions: e.target.value })} placeholder="描述角色、工作流程、输出要求和限制…" />
      </Field>
    </article>
  );
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return <button className={`toggle ${checked ? "on" : ""}`} type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}><span /></button>;
}

function parseEnv(value: string) {
  return Object.fromEntries(value.split("\n").filter(Boolean).map((line) => {
    const index = line.indexOf("=");
    return index < 0 ? [line.trim(), ""] : [line.slice(0, index).trim(), line.slice(index + 1)];
  }).filter(([key]) => key));
}
