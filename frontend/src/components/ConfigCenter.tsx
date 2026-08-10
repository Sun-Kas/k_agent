/**
 * 配置中心：模型 / MCP / Skills / 语音 的读写 UI。
 * 挂载时并行拉三类配置；按 tab 保存，MCP 变更后可触发 reload。
 */
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  getMcpCapabilities,
  getMcpConfig,
  getModelsConfig,
  getSkillsConfig,
  importSkill,
  reloadMcp,
  saveMcpConfig,
  saveModelsConfig,
  saveSkillsConfig
} from "../api/agui";
import type { McpCapabilities, McpServerConfig, ModelProfile, SkillConfig } from "../types";
import {
  readVoiceConfig,
  VOICE_STYLES,
  writeVoiceConfig,
  type VoiceConfig
} from "../voice-config";

type ConfigTab = "model" | "mcp" | "skills" | "voice";

export function ConfigCenter({ onBack }: { onBack: () => void }) {
  const [tab, setTab] = useState<ConfigTab>("model");
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [mcpIsTemplate, setMcpIsTemplate] = useState(false);
  const [mcpWarnings, setMcpWarnings] = useState<string[]>([]);
  const [mcpCapabilities, setMcpCapabilities] = useState<McpCapabilities | null>(null);
  const [mcpCatalogPath, setMcpCatalogPath] = useState("config/catalog/mcp.json");
  const [servers, setServers] = useState<McpServerConfig[]>([]);
  const [skills, setSkills] = useState<SkillConfig[]>([]);
  const [loadedSkills, setLoadedSkills] = useState<SkillConfig[]>([]);
  const [skillCatalogPath, setSkillCatalogPath] = useState("config/catalog/skills.json");
  const [skillDir, setSkillDir] = useState("content/skills");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfig>(readVoiceConfig);

  useEffect(() => {
    // 三个独立请求并行；用 pending 计数统一关闭 loading。
    let pending = 3;
    const complete = () => {
      pending -= 1;
      if (pending === 0) setLoading(false);
    };
    void getModelsConfig()
      .then((modelData) => {
        setModels(modelData.models);
      })
      .catch((error: Error) => setNotice((current) => current || `模型配置加载失败：${error.message}`))
      .finally(complete);
    void getMcpConfig()
      .then((mcpData) => {
        setMcpIsTemplate(Boolean(mcpData.isTemplate));
        setMcpWarnings([...(mcpData.warnings ?? []), ...(mcpData.blocked ?? []).map((id) => `已按策略屏蔽：${id}`)]);
        setServers(mcpData.servers.map((server) => ({ ...server, isNew: false })));
        if (mcpData.source) setMcpCatalogPath(mcpData.source);
      })
      .catch((error: Error) => setNotice((current) => current || `MCP 配置加载失败：${error.message}`))
      .finally(complete);
    void getSkillsConfig()
      .then((skillsData) => {
        setSkills(skillsData.skills.map((skill) => ({ ...skill, isNew: false })));
        setLoadedSkills(skillsData.loadedSkills ?? skillsData.skills);
        if (skillsData.path) setSkillCatalogPath(skillsData.path);
        if (skillsData.skillDir) setSkillDir(skillsData.skillDir);
      })
      .catch((error: Error) => setNotice((current) => current || `Skills 配置加载失败：${error.message}`))
      .finally(complete);
  }, []);

  useEffect(() => {
    if (tab !== "mcp") return;
    void getMcpCapabilities()
      .then(setMcpCapabilities)
      .catch((error: Error) => setNotice((current) => current || `MCP 能力加载失败：${error.message}`));
  }, [tab, servers.length]);

  /** 按当前 tab 写回对应配置；语音只写 localStorage。 */
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setNotice("");
    try {
      if (tab === "model") {
        assertUniqueIds(models, "模型");
        await saveModelsConfig(models);
        setModels((current) => current.map((model) => ({
          ...model,
          isNew: false,
          apiKey: "",
          apiKeyConfigured: model.apiKeyConfigured || Boolean(model.apiKey)
        })));
        setNotice("模型配置已保存，可立即在对话中选择");
      } else if (tab === "mcp") {
        assertUniqueIds(servers, "MCP 服务");
        const mcpData = await saveMcpConfig(servers);
        const capabilityData = await getMcpCapabilities();
        setServers(mcpData.servers.map((server) => ({ ...server, isNew: false })));
        setMcpCapabilities(capabilityData);
        setMcpWarnings([...(mcpData.warnings ?? []), ...(mcpData.blocked ?? []).map((id) => `已按策略屏蔽：${id}`)]);
        setNotice(mcpConnectionNotice(mcpData.servers));
      } else if (tab === "skills") {
        assertUniqueIds(skills, "Skill");
        await saveSkillsConfig(skills);
        const skillsData = await getSkillsConfig();
        setSkills(skillsData.skills.map((skill) => ({ ...skill, isNew: false })));
        setLoadedSkills(skillsData.loadedSkills ?? skillsData.skills);
        setNotice("Skills 已保存，并已即时应用到 Agent");
      } else {
        const saved = writeVoiceConfig(voiceConfig);
        setVoiceConfig(saved);
        setNotice("语音配置已保存，将用于下一次朗读");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "未知错误";
      const isTimeout = error instanceof DOMException
        && (error.name === "TimeoutError" || error.name === "AbortError");
      setNotice(
        isTimeout
          ? "保存失败：MCP 下载或连接超过 120 秒，请检查网络后重试"
          : `保存失败：${message}`
      );
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
          <button className={tab === "voice" ? "active" : ""} type="button" onClick={() => { setTab("voice"); setNotice(""); }}>
            <i>声</i><span><strong>语音</strong><small>音色、风格与语速</small></span>
          </button>
          <div className="config-note"><strong>安全提示</strong><p>密钥不会回传到浏览器。MCP 环境变量只显示遮蔽值。</p></div>
        </nav>

        <form className="config-content" onSubmit={save}>
          {loading ? <div className="config-loading">正在读取配置…</div> : (
            <>
              {tab === "model" && (
                <section className="config-section">
                  <ConfigHeading eyebrow="MODEL PROFILES" title="模型管理" copy="配置多个 OpenAI 兼容模型，并声明图像输入和推理能力。" />
                  <div className="path-bar"><span>读取方式</span><code>GET /api/config/models</code></div>
                  <div className="config-cards">
                    {models.map((model, index) => (
                      <ModelCard key={index} model={model} onChange={(next) => setModels(models.map((item, i) => i === index ? next : item))} onRemove={() => setModels(models.filter((_, i) => i !== index))} />
                    ))}
                    <button className="add-config" type="button" onClick={() => setModels([...models, { id: nextId("model", models), name: "新模型", model: "", baseUrl: "https://api.openai.com/v1", apiKeyConfigured: false, apiKey: "", apiKeyEnv: "OPENAI_API_KEY", multimodal: false, supportsReasoning: false, contextWindow: 128000, maxOutputTokens: 8192, contextSafetyTokens: 4096, enabled: true, isNew: true }])}>
                      <span>＋</span><strong>添加模型</strong><small>配置 URL、模型名、密钥和能力</small>
                    </button>
                  </div>
                </section>
              )}

              {tab === "mcp" && (
                <section className="config-section">
                  <ConfigHeading eyebrow="MODEL CONTEXT PROTOCOL" title="MCP 服务" copy="接入层统一管理服务摘要与连接配置；保存后通知 Agent Backend 重新载入连接。数据位于 $K_AGENT_HOME。" />
                  <div className="path-bar"><span>列表摘要</span><code>{mcpCatalogPath}</code>{mcpIsTemplate && <b>当前使用示例配置</b>}</div>
                  <CapabilityStrip capabilities={mcpCapabilities} warnings={mcpWarnings} onReload={async () => { await reloadMcp(); const [mcpData, capabilityData] = await Promise.all([getMcpConfig(), getMcpCapabilities()]); setServers(mcpData.servers); setMcpCapabilities(capabilityData); setNotice("MCP 已重新载入"); }} />
                  <div className="config-cards">
                    {servers.map((server, index) => (
                      <McpCard key={index} server={server} onChange={(next) => setServers(servers.map((item, i) => i === index ? next : item))} onRemove={() => setServers(servers.filter((_, i) => i !== index))} />
                    ))}
                    <button className="add-config" type="button" onClick={() => setServers([...servers, { id: nextId("server", servers), name: "新 MCP 服务", description: "", type: "stdio", command: "", args: [], env: {}, envPassthrough: [], cwd: "", url: "", bearerTokenEnv: "", headers: {}, envHeaders: {}, enabled: true, isNew: true }])}>
                      <span>＋</span><strong>添加 MCP 服务</strong><small>配置 stdio 命令、参数和环境变量</small>
                    </button>
                  </div>
                </section>
              )}

              {tab === "skills" && (
                <section className="config-section">
                  <ConfigHeading eyebrow="REUSABLE INSTRUCTIONS" title="Skills" copy="接入层从 $K_AGENT_HOME/config/catalog/skills.json 提供列表，选中后再读取对应 Skill 指令并随运行请求发送。" />
                  <div className="path-bar"><span>列表摘要</span><code>{skillCatalogPath}</code></div>
                  <div className="config-cards">
                    {skills.map((skill, index) => (
                      <SkillCard key={index} skill={skill} onChange={(next) => setSkills(skills.map((item, i) => i === index ? next : item))} onRemove={() => setSkills(skills.filter((_, i) => i !== index))} />
                    ))}
                    <button className="add-config" type="button" onClick={() => setSkills([...skills, { id: nextId("skill", skills), name: "新 Skill", description: "", instructions: "", enabled: true, isNew: true }])}>
                      <span>＋</span><strong>创建 Skill</strong><small>添加一组可复用的 Agent 专业指令</small>
                    </button>
                  </div>
                  <LoadedSkills skills={loadedSkills} skillDir={skillDir} onImport={async (file) => {
                    const result = await importSkill(file);
                    if (result.skills) {
                      setSkills(result.skills.map((skill) => ({ ...skill, isNew: false })));
                      setLoadedSkills(result.loadedSkills ?? result.skills);
                    } else {
                      const skillsData = await getSkillsConfig();
                      setSkills(skillsData.skills.map((skill) => ({ ...skill, isNew: false })));
                      setLoadedSkills(skillsData.loadedSkills ?? skillsData.skills);
                    }
                    setNotice(`Skill「${result.name}」已通过 zip 校验并导入`);
                  }} />
                </section>
              )}
              {tab === "voice" && (
                <VoiceConfigSection
                  value={voiceConfig}
                  onChange={setVoiceConfig}
                  onNotice={setNotice}
                />
              )}
              <footer className="config-footer">
                <p className={notice.includes("失败") ? "error" : ""}>{notice}</p>
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

function VoiceConfigSection({
  value,
  onChange,
  onNotice
}: {
  value: VoiceConfig;
  onChange: (value: VoiceConfig) => void;
  onNotice: (notice: string) => void;
}) {
  const supported = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [previewing, setPreviewing] = useState(false);
  const [voicePickerOpen, setVoicePickerOpen] = useState(false);
  const [voiceSearch, setVoiceSearch] = useState("");
  const [voiceScope, setVoiceScope] = useState<"chinese" | "all">("chinese");
  const previewActiveRef = useRef(false);
  const voicePickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!supported) return;
    const refreshVoices = () => {
      // Chromium and Safari often expose an empty catalog initially, then notify
      // voiceschanged after native voice services finish loading.
      const available = [...window.speechSynthesis.getVoices()].sort((left, right) => {
        const leftChinese = /^zh(?:-|_)/i.test(left.lang) ? 0 : 1;
        const rightChinese = /^zh(?:-|_)/i.test(right.lang) ? 0 : 1;
        return leftChinese - rightChinese
          || Number(right.localService) - Number(left.localService)
          || left.name.localeCompare(right.name);
      });
      setVoices(available);
    };
    refreshVoices();
    window.speechSynthesis.addEventListener("voiceschanged", refreshVoices);
    return () => {
      window.speechSynthesis.removeEventListener("voiceschanged", refreshVoices);
      // Preview shares the browser synthesis singleton, so only cancel speech
      // started by this panel when leaving it.
      if (previewActiveRef.current) window.speechSynthesis.cancel();
    };
  }, [supported]);

  useEffect(() => {
    if (!voicePickerOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!voicePickerRef.current?.contains(event.target as Node)) setVoicePickerOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setVoicePickerOpen(false);
    };
    // The picker is rendered inside the scrolling config page, so document-level
    // listeners close it consistently when the user clicks another setting.
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [voicePickerOpen]);

  const selectedVoiceAvailable = !value.voiceURI || voices.some((voice) => voice.voiceURI === value.voiceURI);
  const selectedVoice = voices.find((voice) => voice.voiceURI === value.voiceURI);
  const normalizedVoiceSearch = voiceSearch.trim().toLocaleLowerCase();
  const chineseVoiceCount = voices.filter((voice) => isChineseVoice(voice)).length;
  const visibleVoices = voices.filter((voice) => {
    if (voiceScope === "chinese" && !isChineseVoice(voice)) return false;
    if (!normalizedVoiceSearch) return true;
    return `${voice.name} ${voice.lang}`.toLocaleLowerCase().includes(normalizedVoiceSearch);
  });

  function previewVoice() {
    if (!supported) {
      onNotice("试听失败：当前浏览器不支持语音输出");
      return;
    }
    const style = VOICE_STYLES.find((item) => item.id === value.style) ?? VOICE_STYLES[0];
    const utterance = new SpeechSynthesisUtterance(style.preview);
    const selectedVoice = voices.find((voice) => voice.voiceURI === value.voiceURI);
    if (selectedVoice) {
      utterance.voice = selectedVoice;
      utterance.lang = selectedVoice.lang;
    } else {
      utterance.lang = "zh-CN";
    }
    utterance.rate = value.rate;
    utterance.pitch = value.pitch;
    utterance.volume = value.volume;
    window.speechSynthesis.cancel();
    previewActiveRef.current = true;
    utterance.onstart = () => {
      setPreviewing(true);
      onNotice("正在试听当前语音配置");
    };
    utterance.onend = () => {
      previewActiveRef.current = false;
      setPreviewing(false);
      onNotice("试听完成，保存后用于对话朗读");
    };
    utterance.onerror = (event) => {
      previewActiveRef.current = false;
      setPreviewing(false);
      // "canceled" is expected when the user starts another preview quickly.
      if (event.error !== "canceled" && event.error !== "interrupted") {
        onNotice(`试听失败：${event.error || "系统语音服务不可用"}`);
      }
    };
    window.speechSynthesis.speak(utterance);
  }

  return (
    <section className="config-section voice-config-section">
      <ConfigHeading eyebrow="VOICE CONVERSATION" title="语音配置" copy="选择当前设备的朗读音色，并调整语音对话的表达风格与播放参数。" />
      <div className="voice-config-block">
        <div className="config-field voice-picker-field">
          <span><strong>朗读音色</strong><small>{supported ? `${voices.length} 个系统音色可用` : "当前浏览器不支持语音输出"}</small></span>
          <div className={`voice-picker ${voicePickerOpen ? "open" : ""}`} ref={voicePickerRef}>
            <button
              className="voice-picker-trigger"
              type="button"
              disabled={!supported}
              aria-haspopup="listbox"
              aria-expanded={voicePickerOpen}
              onClick={() => setVoicePickerOpen((open) => {
                if (!open) setVoiceSearch("");
                return !open;
              })}
            >
              <span>
                <strong>{selectedVoice?.name ?? (value.voiceURI ? "原音色当前不可用" : "系统自动选择")}</strong>
                <small>{selectedVoice ? `${selectedVoice.lang} · ${selectedVoice.localService ? "本地" : "网络"}` : "根据文本语言自动匹配"}</small>
              </span>
              <i aria-hidden="true">⌄</i>
            </button>
            {voicePickerOpen && (
              <div className="voice-picker-popover">
                <header>
                  <input
                    autoFocus
                    value={voiceSearch}
                    onChange={(event) => setVoiceSearch(event.target.value)}
                    placeholder="搜索音色名称或语言"
                    aria-label="搜索音色"
                  />
                  <div className="voice-scope-switch" role="group" aria-label="音色语言范围">
                    <button type="button" className={voiceScope === "chinese" ? "active" : ""} onClick={() => setVoiceScope("chinese")}>中文 {chineseVoiceCount}</button>
                    <button type="button" className={voiceScope === "all" ? "active" : ""} onClick={() => setVoiceScope("all")}>全部 {voices.length}</button>
                  </div>
                </header>
                <div className="voice-picker-options" role="listbox" aria-label="朗读音色">
                  <button
                    type="button"
                    role="option"
                    aria-selected={!value.voiceURI}
                    className={!value.voiceURI ? "selected" : ""}
                    onClick={() => { onChange({ ...value, voiceURI: "" }); setVoiceSearch(""); setVoicePickerOpen(false); }}
                  >
                    <span className="voice-option-mark">{!value.voiceURI ? "✓" : "A"}</span>
                    <span><strong>系统自动选择</strong><small>根据文本语言自动匹配</small></span>
                  </button>
                  {!selectedVoiceAvailable && (
                    <button type="button" role="option" aria-selected className="selected unavailable" onClick={() => setVoicePickerOpen(false)}>
                      <span className="voice-option-mark">!</span><span><strong>原音色当前不可用</strong><small>保留设置，等待系统重新载入</small></span>
                    </button>
                  )}
                  {visibleVoices.map((voice) => (
                    <button
                      key={`${voice.voiceURI}-${voice.lang}`}
                      type="button"
                      role="option"
                      aria-selected={value.voiceURI === voice.voiceURI}
                      className={value.voiceURI === voice.voiceURI ? "selected" : ""}
                      onClick={() => { onChange({ ...value, voiceURI: voice.voiceURI }); setVoiceSearch(""); setVoicePickerOpen(false); }}
                    >
                      <span className="voice-option-mark">{value.voiceURI === voice.voiceURI ? "✓" : voice.name.slice(0, 1).toLocaleUpperCase()}</span>
                      <span><strong>{voice.name}</strong><small>{voice.lang} · {voice.localService ? "本地音色" : "网络音色"}</small></span>
                    </button>
                  ))}
                  {visibleVoices.length === 0 && <p>没有匹配的音色</p>}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="voice-config-block">
        <header className="voice-config-label"><strong>对话风格</strong><small>同时控制回复措辞和试听内容</small></header>
        <div className="voice-style-grid" role="radiogroup" aria-label="语音对话风格">
          {VOICE_STYLES.map((style) => (
            <button
              key={style.id}
              className={value.style === style.id ? "active" : ""}
              type="button"
              role="radio"
              aria-checked={value.style === style.id}
              onClick={() => onChange({ ...value, style: style.id })}
            >
              <strong>{style.name}</strong>
              <small>{style.description}</small>
            </button>
          ))}
        </div>
      </div>

      <div className="voice-config-block voice-range-list">
        <VoiceRange label="语速" value={value.rate} minimum={0.7} maximum={1.4} step={0.05} suffix="×" onChange={(rate) => onChange({ ...value, rate })} />
        <VoiceRange label="音调" value={value.pitch} minimum={0.7} maximum={1.3} step={0.05} suffix="×" onChange={(pitch) => onChange({ ...value, pitch })} />
        <VoiceRange label="音量" value={value.volume} minimum={0.2} maximum={1} step={0.05} suffix="%" displayValue={Math.round(value.volume * 100)} onChange={(volume) => onChange({ ...value, volume })} />
      </div>

      <div className="voice-preview-row">
        <span>{value.voiceURI ? voices.find((voice) => voice.voiceURI === value.voiceURI)?.name ?? "原音色当前不可用" : "系统自动选择"}</span>
        <button type="button" disabled={!supported} onClick={previewVoice}>{previewing ? "正在试听" : "试听当前设置"}</button>
      </div>
    </section>
  );
}

function isChineseVoice(voice: SpeechSynthesisVoice) {
  return /^(?:zh|yue)(?:-|_)/i.test(voice.lang);
}

function VoiceRange({
  label,
  value,
  minimum,
  maximum,
  step,
  suffix,
  displayValue = value,
  onChange
}: {
  label: string;
  value: number;
  minimum: number;
  maximum: number;
  step: number;
  suffix: string;
  displayValue?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label>
      <span><strong>{label}</strong><output>{displayValue.toFixed(displayValue < 10 ? 2 : 0)}{suffix}</output></span>
      <input type="range" min={minimum} max={maximum} step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  );
}

function ModelCard({ model, onChange, onRemove }: { model: ModelProfile; onChange: (model: ModelProfile) => void; onRemove: () => void }) {
  const [expanded, setExpanded] = useState(Boolean(model.isNew));
  useEffect(() => {
    if (model.isNew === false) setExpanded(false);
  }, [model.isNew]);
  return (
    <article className={`config-card model-card ${expanded ? "expanded" : "collapsed"}`}>
      <ConfigCardHeader icon="AI" title={model.name || "未命名模型"} summary={`${model.id} · ${model.model || "尚未设置模型"}`} enabled={model.enabled} expanded={expanded} onToggleExpanded={() => setExpanded(!expanded)} onToggleEnabled={(enabled) => onChange({ ...model, enabled })} onRemove={onRemove} />
      {expanded && <div className="config-card-details">
        <Field label="显示名称"><input required value={model.name} onChange={(e) => onChange({ ...model, name: e.target.value })} /></Field>
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
        <div className="field-grid two">
          <Field label="上下文窗口" hint="模型可接受的总 token">
            <input type="number" min={8000} step={1000} value={model.contextWindow ?? 128000} onChange={(e) => onChange({ ...model, contextWindow: Number(e.target.value) })} />
          </Field>
          <Field label="最大输出 Token" hint="为模型回答预留">
            <input type="number" min={256} step={256} value={model.maxOutputTokens ?? 8192} onChange={(e) => onChange({ ...model, maxOutputTokens: Number(e.target.value) })} />
          </Field>
        </div>
        <Field label="上下文安全余量" hint="避免在压缩前撞到模型上限">
          <input type="number" min={0} step={256} value={model.contextSafetyTokens ?? 4096} onChange={(e) => onChange({ ...model, contextSafetyTokens: Number(e.target.value) })} />
        </Field>
        <div className="capability-switches">
          <label><span><strong>多模态输入</strong><small>允许在对话中上传图片</small></span><Toggle checked={model.multimodal} onChange={(multimodal) => onChange({ ...model, multimodal })} /></label>
          <label><span><strong>思考强度</strong><small>支持 reasoning_effort 参数</small></span><Toggle checked={model.supportsReasoning} onChange={(supportsReasoning) => onChange({ ...model, supportsReasoning })} /></label>
        </div>
      </div>}
    </article>
  );
}

function McpCard({ server, onChange, onRemove }: { server: McpServerConfig; onChange: (server: McpServerConfig) => void; onRemove: () => void }) {
  const [expanded, setExpanded] = useState(Boolean(server.isNew));
  useEffect(() => {
    if (server.isNew === false) setExpanded(false);
  }, [server.isNew]);
  const type = server.type ?? "stdio";
  const isRemote = type === "http";
  return (
    <article className={`config-card ${expanded ? "expanded" : "collapsed"}`}>
      <ConfigCardHeader icon="⌁" title={server.name || server.id || "未命名 MCP"} summary={`${server.id} · ${server.description || "暂无简介"}`} status={server.status} enabled={server.enabled} expanded={expanded} onToggleExpanded={() => setExpanded(!expanded)} onToggleEnabled={(enabled) => onChange({ ...server, enabled })} onRemove={onRemove} />
      {expanded && <div className="config-card-details">
      <div className="field-grid two">
        <Field label="显示名称"><input required value={server.name ?? ""} onChange={(e) => onChange({ ...server, name: e.target.value })} placeholder="MCP 服务名称" /></Field>
        <Field label="MCP ID"><input required value={server.id} onChange={(e) => onChange({ ...server, id: e.target.value })} placeholder="MCP server ID" /></Field>
      </div>
      <Field label="简介"><input value={server.description ?? ""} onChange={(e) => onChange({ ...server, description: e.target.value })} placeholder="这个 MCP 服务提供什么能力？" /></Field>
      <div className="mcp-type-row">
        <strong>类型</strong>
        <div className="mcp-type-switch" role="group" aria-label="MCP 类型">
          <button type="button" className={type === "stdio" ? "active" : ""} onClick={() => onChange({ ...server, type: "stdio" })}>STDIO</button>
          <button type="button" className={type === "http" ? "active" : ""} onClick={() => onChange({ ...server, type: "http" })}>流式 HTTP</button>
        </div>
      </div>
      {isRemote ? (
        <>
          <Field label="URL"><input type="url" required value={server.url ?? ""} onChange={(e) => onChange({ ...server, url: e.target.value })} placeholder="https://mcp.example.com/mcp" /></Field>
          <Field label="Bearer 令牌环境变量" hint="填写大写变量名，不要填写令牌内容"><input value={server.bearerTokenEnv ?? ""} onChange={(e) => onChange({ ...server, bearerTokenEnv: e.target.value })} placeholder="MCP_BEARER_TOKEN" pattern="[A-Z_][A-Z0-9_]*" /></Field>
          <KeyValueList title="标头" addLabel="添加标头" keyPlaceholder="键" valuePlaceholder="值" value={server.headers ?? {}} onChange={(headers) => onChange({ ...server, headers })} />
          <KeyValueList title="来自环境变量的标头" addLabel="添加变量" keyPlaceholder="键" valuePlaceholder="环境变量名" value={server.envHeaders ?? {}} onChange={(envHeaders) => onChange({ ...server, envHeaders })} />
        </>
      ) : (
        <>
          <Field label="启动命令"><input required value={server.command ?? ""} onChange={(e) => onChange({ ...server, command: e.target.value })} placeholder="openai-dev-mcp serve-sqlite" /></Field>
          <StringList title="参数" addLabel="添加参数" placeholder="" value={server.args} onChange={(args) => onChange({ ...server, args })} />
          <KeyValueList title="环境变量" addLabel="添加环境变量" keyPlaceholder="键" valuePlaceholder="值" value={server.env} onChange={(env) => onChange({ ...server, env })} />
          <StringList title="环境变量传递" addLabel="添加变量" placeholder="" value={server.envPassthrough ?? []} onChange={(envPassthrough) => onChange({ ...server, envPassthrough })} />
          <Field label="工作目录"><input value={server.cwd ?? ""} onChange={(e) => onChange({ ...server, cwd: e.target.value })} placeholder="~/code" /></Field>
        </>
      )}
      {server.error && <p className="config-error-line">{server.error}</p>}
      </div>}
    </article>
  );
}

function StringList({ title, addLabel, placeholder, value, onChange }: { title: string; addLabel: string; placeholder: string; value: string[]; onChange: (value: string[]) => void }) {
  const rows = value.length ? value : [""];
  return (
    <section className="config-list-field">
      <strong>{title}</strong>
      {rows.map((item, index) => (
        <div className="config-list-row single" key={index}>
          <input value={item} placeholder={placeholder} onChange={(event) => onChange(updateArray(rows, index, event.target.value).filter((entry, entryIndex) => entry || entryIndex < rows.length - 1))} />
          <button type="button" aria-label={`删除${title}`} onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}>×</button>
        </div>
      ))}
      <button className="config-list-add" type="button" onClick={() => onChange([...value, ""])}>＋ {addLabel}</button>
    </section>
  );
}

function KeyValueList({ title, addLabel, keyPlaceholder, valuePlaceholder, value, onChange }: { title: string; addLabel: string; keyPlaceholder: string; valuePlaceholder: string; value: Record<string, string>; onChange: (value: Record<string, string>) => void }) {
  const rows = Object.entries(value);
  const visibleRows = rows.length ? rows : [["", ""] as [string, string]];
  const update = (nextRows: Array<[string, string]>) => onChange(Object.fromEntries(nextRows.filter(([key]) => key.trim()).map(([key, itemValue]) => [key.trim(), itemValue])));
  return (
    <section className="config-list-field">
      <strong>{title}</strong>
      {visibleRows.map(([key, itemValue], index) => (
        <div className="config-list-row" key={index}>
          <input value={key} placeholder={keyPlaceholder} onChange={(event) => update(updatePair(visibleRows, index, 0, event.target.value))} />
          <input value={itemValue} placeholder={valuePlaceholder} onChange={(event) => update(updatePair(visibleRows, index, 1, event.target.value))} />
          <button type="button" aria-label={`删除${title}`} onClick={() => update(visibleRows.filter((_, rowIndex) => rowIndex !== index))}>×</button>
        </div>
      ))}
      <button className="config-list-add" type="button" onClick={() => update([...rows, ["", ""]])}>＋ {addLabel}</button>
    </section>
  );
}

function SkillCard({ skill, onChange, onRemove }: { skill: SkillConfig; onChange: (skill: SkillConfig) => void; onRemove: () => void }) {
  const [expanded, setExpanded] = useState(Boolean(skill.isNew));
  useEffect(() => {
    if (skill.isNew === false) setExpanded(false);
  }, [skill.isNew]);
  return (
    <article className={`config-card skill-card ${expanded ? "expanded" : "collapsed"}`}>
      <ConfigCardHeader icon="✦" title={skill.name || "未命名 Skill"} summary={`${skill.id} · ${skill.description || "暂无简介"}`} enabled={skill.enabled} expanded={expanded} onToggleExpanded={() => setExpanded(!expanded)} onToggleEnabled={(enabled) => onChange({ ...skill, enabled })} onRemove={onRemove} />
      {expanded && <div className="config-card-details">
        <div className="field-grid two">
          <Field label="Skill 名称"><input required value={skill.name} onChange={(e) => onChange({ ...skill, name: e.target.value })} /></Field>
          <Field label="Skill ID"><input required value={skill.id} onChange={(e) => onChange({ ...skill, id: e.target.value })} /></Field>
        </div>
        <Field label="简介"><input value={skill.description} onChange={(e) => onChange({ ...skill, description: e.target.value })} placeholder="这个 Skill 擅长什么？" /></Field>
        <Field label="Skill 指令" hint="启用后会注入系统上下文">
          <textarea className="config-editor" rows={8} value={skill.instructions} onChange={(e) => onChange({ ...skill, instructions: e.target.value })} placeholder="描述角色、工作流程、输出要求和限制…" />
        </Field>
      </div>}
    </article>
  );
}

function ConfigCardHeader({ icon, title, summary, status, enabled, expanded, onToggleExpanded, onToggleEnabled, onRemove }: {
  icon: string;
  title: string;
  summary: string;
  status?: McpServerConfig["status"];
  enabled: boolean;
  expanded: boolean;
  onToggleExpanded: () => void;
  onToggleEnabled: (enabled: boolean) => void;
  onRemove: () => void;
}) {
  return (
    <header className="config-card-header">
      <button className="config-card-summary" type="button" aria-expanded={expanded} onClick={onToggleExpanded}>
        <span className="config-card-chevron" aria-hidden="true">
          <svg viewBox="0 0 20 20" focusable="false">
            <path d="M5 7.5 10 12.5 15 7.5" />
          </svg>
        </span>
        <span className="config-card-icon">{icon}</span>
        <span className="config-card-copy"><strong>{title}</strong><small>{summary}</small></span>
      </button>
      {status && <span className={`mcp-status ${status}`}>{mcpStatusLabel(status)}</span>}
      <Toggle checked={enabled} onChange={onToggleEnabled} />
      <button className="remove-button" type="button" aria-label={`删除 ${title}`} title="删除" onClick={onRemove}>×</button>
    </header>
  );
}

function mcpStatusLabel(status: NonNullable<McpServerConfig["status"]>) {
  if (status === "connected") return "已连接";
  if (status === "failed") return "连接失败";
  if (status === "disabled") return "已禁用";
  if (status === "pending") return "连接中";
  return "状态未知";
}

function mcpConnectionNotice(servers: McpServerConfig[]) {
  const failed = servers.filter((server) => server.enabled && server.status === "failed");
  if (failed.length) {
    return `连接失败：${failed.map((server) => `${server.name || server.id}${server.error ? `（${server.error}）` : ""}`).join("；")}`;
  }
  const enabled = servers.filter((server) => server.enabled);
  const connected = enabled.filter((server) => server.status === "connected");
  if (enabled.length && connected.length === enabled.length) {
    return `MCP 配置已保存，${connected.length} 个服务连接成功`;
  }
  if (!enabled.length) return "MCP 配置已保存；当前没有启用的服务";
  return `MCP 配置已保存；已连接 ${connected.length}/${enabled.length}，请查看各服务状态`;
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return <button className={`toggle ${checked ? "on" : ""}`} type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}><span /></button>;
}

function CapabilityStrip({ capabilities, warnings, onReload }: { capabilities: McpCapabilities | null; warnings: string[]; onReload: () => Promise<void> }) {
  const toolCount = capabilities?.tools.length ?? 0;
  const resourceCount = Object.values(capabilities?.resources ?? {}).reduce((count, items) => count + items.length, 0);
  const promptCount = Object.values(capabilities?.prompts ?? {}).reduce((count, items) => count + items.length, 0);
  return (
    <div className="capability-strip">
      <span>工具 {toolCount}</span><span>资源 {resourceCount}</span><span>Prompts {promptCount}</span>
      <button type="button" onClick={() => { void onReload(); }}>重新载入</button>
      {warnings.length > 0 && <small>{warnings.slice(0, 2).join("；")}</small>}
    </div>
  );
}

function LoadedSkills({
  skills,
  skillDir,
  onImport
}: {
  skills: SkillConfig[];
  skillDir: string;
  onImport: (file: File) => Promise<void>;
}) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  async function handleFiles(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    setError("");
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setError("请上传 .zip 文件");
      return;
    }
    setUploading(true);
    try {
      await onImport(file);
    } catch (importError) {
      setError(importError instanceof Error ? importError.message : "导入失败");
    } finally {
      setUploading(false);
    }
  }

  return (
    <section className="loaded-skills">
      <header><strong>运行时载入结果</strong><small>{skills.length} 个 Skill，来自 {skillDir}</small></header>
      <div className="skill-table">
        {skills.map((skill) => (
          <div key={`${skill.source}-${skill.id}`}>
            <b>{skill.name}</b><span>{skill.source ?? "config"} · {skill.loadedFrom ?? "config"}</span><small>{skill.description || "由后端加载器管理"}</small>
          </div>
        ))}
      </div>
      <label
        className={`skill-upload ${uploading ? "uploading" : ""}`}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          void handleFiles(event.dataTransfer.files);
        }}
      >
        <input type="file" accept=".zip,application/zip" disabled={uploading} onChange={(event) => { void handleFiles(event.target.files); event.currentTarget.value = ""; }} />
        <span>⇧</span>
        <strong>{uploading ? "正在校验并导入…" : "拖拽 zip 文件或点击上传"}</strong>
        <small>压缩包需包含一个 SKILL.md，且 frontmatter 必须有 name 和 description</small>
      </label>
      {error && <p className="config-error-line">{error}</p>}
    </section>
  );
}

function updateArray(items: string[], index: number, value: string) {
  return items.map((item, itemIndex) => itemIndex === index ? value : item);
}

function updatePair(items: Array<[string, string]>, index: number, column: 0 | 1, value: string): Array<[string, string]> {
  return items.map((item, itemIndex) => itemIndex === index ? (column === 0 ? [value, item[1]] : [item[0], value]) : item);
}

function nextId(prefix: string, items: Array<{ id: string }>) {
  const existing = new Set(items.map((item) => item.id));
  let index = items.length + 1;
  while (existing.has(`${prefix}-${index}`)) index += 1;
  return `${prefix}-${index}`;
}

function assertUniqueIds(items: Array<{ id: string }>, label: string) {
  const ids = items.map((item) => item.id.trim());
  const duplicate = ids.find((id, index) => id && ids.indexOf(id) !== index);
  if (duplicate) throw new Error(`${label} ID「${duplicate}」重复`);
  if (ids.some((id) => !id)) throw new Error(`${label} ID 不能为空`);
}
