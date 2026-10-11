/**
 * "新建 Project" 弹窗：在服务端（Access Layer，本机进程）浏览真实目录树来选路径，
 * 而不是用浏览器的 window.prompt 手输，也不是用浏览器 File System Access API——
 * 后者选中目录后浏览器不会把磁盘绝对路径交给网页（只给一个沙盒句柄），拿不到
 * Project 需要的真实 root_path。详见 access_layer/fs_browse.py 顶部注释。
 */
import { useEffect, useRef, useState } from "react";
import { browseDirectory, makeDirectory } from "../api/agui";
import type { DirectoryListing } from "../types";

// 目录浏览走本机 Access Layer，一次 list 通常几毫秒就回来。如果请求一回来就
// 立刻切换"导航中"状态再切回去，变暗遮罩的 CSS 过渡还没来得及被肉眼感知完整
// 一遍就被打断、按钮也在同一瞬间经历 disabled→enabled，看起来像闪一下而不是
// 真的在转场。这里延迟这么久才真正显示"导航中"视觉效果——如果请求比这个还快
// （本机目录浏览的常态），用户根本不会看到任何过渡，列表就是原地瞬间换掉。
const NAVIGATING_INDICATOR_DELAY_MS = 150;

export function ProjectCreateDialog({
  onClose,
  onCreate
}: {
  onClose: () => void;
  /** 由调用方（App）实际发起 createProject + 更新侧栏状态；这里只负责选出一个 root_path。 */
  onCreate: (rootPath: string) => Promise<void>;
}) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  // "正在拉取下一层目录" 和 "listing 目前是否为空" 是两件事：之前把两者合成一个
  // loading 布尔值，导致每次点文件夹都先把整个列表清空渲染"正在读取…"、数据
  // 回来后再整体换回列表——两次完全不同的子树让浏览器重新布局，看起来像闪一下。
  // 现在导航中仍然保留上一层的 listing 不清空，只叠一层半透明遮罩，数据到了再
  // 原地替换内容，DOM 结构不跳变。
  const [navigating, setNavigating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const indicatorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 每次 navigateTo 自增，让慢请求的回包发现"已经有更新的请求了"时放弃写入
  // listing——不然快速连点两个文件夹时，先发的慢请求可能在后发的快请求之后
  // 才回来，把界面内容又换回错误的那一层。
  const requestSeqRef = useRef(0);

  // 打开时从用户主目录开始浏览；之后每次导航都重新拉一次子目录列表，
  // 不在前端缓存整棵树——目录数量不可控，服务端按需单层列更安全。
  useEffect(() => {
    void navigateTo(undefined);
    return () => {
      if (indicatorTimerRef.current) clearTimeout(indicatorTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function navigateTo(path: string | undefined) {
    const seq = ++requestSeqRef.current;
    setError(null);
    // 临时的新文件夹行只属于当前目录；用户进入别处时直接收起，避免把尚未提交的
    // 名称带到另一个目录里。
    setCreatingFolder(false);
    setNewFolderName("");
    // 延迟显示"导航中"视觉效果，见顶部 NAVIGATING_INDICATOR_DELAY_MS 的说明。
    indicatorTimerRef.current = setTimeout(() => {
      if (requestSeqRef.current === seq) setNavigating(true);
    }, NAVIGATING_INDICATOR_DELAY_MS);
    try {
      const next = await browseDirectory(path);
      if (requestSeqRef.current !== seq) return; // 已经有更新的请求在跑，丢弃这个回包
      setListing(next);
    } catch (err) {
      if (requestSeqRef.current !== seq) return;
      setError(err instanceof Error ? err.message : "无法浏览该目录");
    } finally {
      if (requestSeqRef.current === seq) {
        if (indicatorTimerRef.current) clearTimeout(indicatorTimerRef.current);
        setNavigating(false);
      }
    }
  }

  async function submitNewFolder() {
    if (!listing || !newFolderName.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const entry = await makeDirectory(listing.path, newFolderName.trim());
      setCreatingFolder(false);
      setNewFolderName("");
      await navigateTo(entry.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法新建文件夹");
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmSelection() {
    if (!listing) return;
    setSubmitting(true);
    setError(null);
    try {
      await onCreate(listing.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法创建 Project");
      setSubmitting(false);
    }
  }

  return (
    <div className="marketplace-dialog-backdrop" role="presentation" onClick={onClose}>
      <section
        className="marketplace-dialog project-create-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-create-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header>
          <p className="marketplace-dialog-kicker">Project</p>
          <h3 id="project-create-title">新建 Project</h3>
        </header>
        <div className="marketplace-dialog-body">
          <div className="pcd-current-path" title={listing?.path}>
            <span className="icon">📁</span>
            <span className="pcd-path-text">{listing?.path ?? "…"}</span>
          </div>

          <div className="pcd-browser">
            <div className="pcd-browser-toolbar">
              <button
                type="button"
                className="pcd-toolbar-btn"
                disabled={!listing?.parent || navigating}
                onClick={() => listing?.parent && void navigateTo(listing.parent)}
              >
                ← 上一级
              </button>
              <button
                type="button"
                className="pcd-toolbar-btn"
                disabled={!listing || navigating || creatingFolder || submitting}
                onClick={() => {
                  setError(null);
                  setNewFolderName("");
                  setCreatingFolder(true);
                }}
              >
                ＋ 新建文件夹
              </button>
            </div>

            <div className={`pcd-entry-list ${navigating && listing ? "pcd-entry-list-stale" : ""}`}>
              {/* 只有"从没拿到过任何 listing"（刚打开弹窗那一刻）才显示纯文字的
                  读取中提示；一旦有过 listing，切换目录时旧列表原地保留、只靠
                  上面的半透明遮罩表示"正在加载下一层"，避免内容整体换成文字再换回来。 */}
              {navigating && !listing && <p className="pcd-state-note">正在读取…</p>}
              {!navigating && listing && listing.entries.length === 0 && !creatingFolder && (
                <p className="pcd-state-note">这个目录下还没有子文件夹。</p>
              )}
              {creatingFolder && (
                <form
                  className="pcd-entry-row pcd-new-folder-row"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitNewFolder();
                  }}
                >
                  <span className="icon" aria-hidden="true">📁</span>
                  <input
                    autoFocus
                    aria-label="新文件夹名称"
                    value={newFolderName}
                    placeholder="新文件夹"
                    disabled={submitting}
                    onChange={(event) => setNewFolderName(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Escape" && !submitting) {
                        setCreatingFolder(false);
                        setNewFolderName("");
                      }
                    }}
                  />
                  <span className="pcd-new-folder-hint">Enter 创建 · Esc 取消</span>
                </form>
              )}
              {listing?.entries.map((entry) => (
                <button
                  type="button"
                  key={entry.path}
                  className="pcd-entry-row"
                  disabled={navigating}
                  onClick={() => void navigateTo(entry.path)}
                >
                  <span className="icon">📁</span>
                  <span className="name">{entry.name}</span>
                  <span className="chevron">›</span>
                </button>
              ))}
            </div>
          </div>

          {error && <p className="marketplace-dialog-alert">{error}</p>}
          <p className="marketplace-dialog-hint">
            选中当前目录后点击"选择此文件夹"即可创建 Project；Project 名称默认取文件夹名，创建后可在侧栏双击重命名。
          </p>
        </div>
        <footer>
          <button className="marketplace-btn-ghost" type="button" disabled={submitting} onClick={onClose}>
            取消
          </button>
          <button
            className="marketplace-btn-primary"
            type="button"
            disabled={!listing || navigating || submitting}
            onClick={() => void confirmSelection()}
          >
            {submitting ? "正在创建…" : "选择此文件夹"}
          </button>
        </footer>
      </section>
    </div>
  );
}
