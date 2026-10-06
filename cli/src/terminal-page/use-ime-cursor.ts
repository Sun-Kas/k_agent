import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { measureElement, useCursor, useWindowSize, type DOMElement } from "ink";
import stringWidth from "string-width";

const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export type BoxMetrics = { x: number; y: number; width: number; height: number };

/**
 * 把终端文本宽度转换成真实光标相对位置。
 * 不能使用 JavaScript 字符长度：中文、Emoji 和组合字符占用的终端列数不同。
 */
export function terminalCursorOffset(value: string, columns: number): { x: number; y: number } {
  const lineColumns = Math.max(1, Math.floor(columns));
  let x = 0;
  let y = 0;
  for (const { segment } of graphemeSegmenter.segment(value)) {
    if (segment === "\n") {
      x = 0;
      y += 1;
      continue;
    }
    const width = stringWidth(segment);
    if (width === 0) continue;
    if (x > 0 && x + width > lineColumns) {
      x = 0;
      y += 1;
    }
    x += width;
    if (x >= lineColumns) {
      y += Math.floor(x / lineColumns);
      x %= lineColumns;
    }
  }
  return { x, y };
}

export function imeCursorPosition(
  metrics: BoxMetrics,
  renderedValue: string,
  rowOffset = 0,
): { x: number; y: number } | undefined {
  // 尚未完成布局时 yoga 全是 0。宽度为 0 的 caret 仍可能有合法的 x/y。
  if (metrics.width <= 0 && metrics.height <= 0 && metrics.x === 0 && metrics.y === 0) {
    return undefined;
  }
  const columns = Math.max(1, metrics.width);
  const offset = terminalCursorOffset(renderedValue, columns);
  return { x: metrics.x + offset.x, y: metrics.y + offset.y + rowOffset };
}

/**
 * Ink 帧高不到一屏时会在帧尾补一个换行，真实光标停在最后一行的下一行；
 * 帧高占满或超过一屏时没有这个换行，光标停在最后一行本身。
 * 但 Ink 计算光标后缀时一律按「多一行」上移，满屏帧因此整体偏高一行。
 * 这里按同样条件补回 1，不能改成按 surface 判断——聊天页一样会占满屏。
 */
export function fullscreenRowOffset(frameHeight: number, rows: number): number {
  return rows > 0 && frameHeight >= rows ? 1 : 0;
}

/** 输入框所在的这一帧有多高：沿布局树走到 ink-root，读它的 yoga 高度。 */
export function liveFrameHeight(node: DOMElement): number {
  let root = node;
  while (root.parentNode) root = root.parentNode;
  return root.yogaNode?.getComputedHeight() ?? 0;
}

function sameMetrics(left: BoxMetrics, right: BoxMetrics): boolean {
  return left.x === right.x && left.y === right.y && left.width === right.width && left.height === right.height;
}

/**
 * Ink 默认把终端光标藏在输出底部，输入法候选窗只认这个真实光标。
 *
 * 输入框的屏幕原点来自布局，但插入点随当前 `renderedValue` 立刻能算。
 * 上屏后如果仍套用「上一帧算好的坐标」，会先闪回旧位置再跳到新位置。
 * render 阶段必须用「旧原点 + 新文本」算光标，和这一帧要画的字对齐。
 */
export function useImeCursor(
  lineRef: RefObject<DOMElement | null>,
  renderedValue: string,
  active: boolean,
): void {
  const { setCursorPosition } = useCursor();
  const { rows } = useWindowSize();
  const frameRef = useRef<{ metrics: BoxMetrics; rowOffset: number } | undefined>(undefined);
  const [, setLayoutTick] = useState(0);

  function positionFrom(frame: { metrics: BoxMetrics; rowOffset: number }): { x: number; y: number } | undefined {
    return imeCursorPosition(frame.metrics, renderedValue, frame.rowOffset);
  }

  useLayoutEffect(() => {
    if (!active || !lineRef.current) {
      frameRef.current = undefined;
      setLayoutTick(0);
      setCursorPosition(undefined);
      return;
    }
    const frame = {
      metrics: measureElement(lineRef.current),
      rowOffset: fullscreenRowOffset(liveFrameHeight(lineRef.current), rows),
    };
    const next = positionFrom(frame);
    if (!next) return;
    const previous = frameRef.current;
    frameRef.current = frame;
    setCursorPosition(next);
    // 输入框挪了（或帧跨过满屏边界）才再画一帧。上屏只改文本，原点不变，
    // 不能 setState，否则会先画出旧光标。
    if (!previous || !sameMetrics(previous.metrics, frame.metrics) || previous.rowOffset !== frame.rowOffset) {
      setLayoutTick((tick) => tick + 1);
    }
  });

  if (!active) setCursorPosition(undefined);
  else if (frameRef.current) {
    const next = positionFrom(frameRef.current);
    if (next) setCursorPosition(next);
  }

  useEffect(() => () => setCursorPosition(undefined), [setCursorPosition]);
}
