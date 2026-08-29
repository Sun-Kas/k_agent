import React, { useEffect, useRef, useState } from "react";
import { Box, Text, useInput, usePaste, type DOMElement } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import { useImeCursor } from "../use-ime-cursor.js";
import {
  createLineEditor,
  deleteBackward,
  deleteForward,
  deleteToLineStart,
  deleteWordBackward,
  graphemeAtCursor,
  insertText,
  moveLeft,
  moveRight,
  moveToEnd,
  moveToStart,
  moveWordLeft,
  moveWordRight,
  textAfterCursor,
  textBeforeCursor,
  type LineEditorState,
} from "../line-editor.js";

/**
 * 终端输入框。
 *
 * 行编辑保持终端习惯：←→ 移动、Ctrl+A/E 行首行尾、Ctrl+W 删词、Ctrl+U/K 删到行首行尾。
 * 插入点只用真实终端光标（给输入法候选窗定位）。不要再画一层反显块，否则会叠成重影。
 *
 * 输入法预编辑画在真实光标上，不进草稿。确认后的汉字必须按行编辑光标插入。
 * 父层改写草稿时不能把光标甩到行尾，否则会出现「拼音显示在中间、上屏却接到末尾」。
 */
export function Composer({ value, disabled, focused, captureDigits, suppressKeys, onChange, onSubmit }: {
  value: string;
  disabled: boolean;
  focused: boolean;
  /** 首页用数字键切换模式，此时空输入框不能把数字吞进草稿。 */
  captureDigits: boolean;
  /** `/` 选择栏打开时，导航键归选择栏所有，输入框只处理正文编辑。 */
  suppressKeys: boolean;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
}): React.ReactElement {
  const editorRef = useRef<LineEditorState>(createLineEditor(value));
  const [editor, setEditor] = useState(editorRef.current);
  const selfEdit = useRef(false);
  const inputLineRef = useRef<DOMElement | null>(null);
  const active = focused && !disabled;

  useEffect(() => {
    if (selfEdit.current) {
      if (editorRef.current.value === value) selfEdit.current = false;
      return;
    }
    if (editorRef.current.value === value) return;
    const next = createLineEditor(value);
    editorRef.current = next;
    setEditor(next);
  }, [value]);

  const before = textBeforeCursor(editor);
  const after = `${graphemeAtCursor(editor)}${textAfterCursor(editor)}`;
  const prompt = `${TERMINAL_DESIGN.symbols.prompt} `;
  useImeCursor(inputLineRef, `${prompt}${before}`, active);

  function apply(next: LineEditorState): void {
    selfEdit.current = true;
    editorRef.current = next;
    setEditor(next);
    onChange(next.value);
  }

  function insert(text: string): void {
    if (!text) return;
    apply(insertText(editorRef.current, text));
  }

  usePaste((text) => {
    if (!active) return;
    insert(text.replace(/\r\n/g, "\n").replace(/\r/g, "\n"));
  }, { isActive: active });

  useInput((input, key) => {
    if (!focused || disabled) return;
    if (suppressKeys && (key.return || key.tab || key.escape || key.upArrow || key.downArrow)) return;
    if (captureDigits && !editorRef.current.value && !key.ctrl && !key.meta && /^[1-4]$/.test(input)) return;
    if (key.upArrow || key.downArrow || key.tab || key.escape) return;

    if (key.leftArrow) return apply(key.meta ? moveWordLeft(editorRef.current) : moveLeft(editorRef.current));
    if (key.rightArrow) return apply(key.meta ? moveWordRight(editorRef.current) : moveRight(editorRef.current));
    if (key.ctrl) {
      if (input === "a") return apply(moveToStart(editorRef.current));
      if (input === "e") return apply(moveToEnd(editorRef.current));
      if (input === "b") return apply(moveLeft(editorRef.current));
      if (input === "f") return apply(moveRight(editorRef.current));
      if (input === "u") return apply(deleteToLineStart(editorRef.current));
      if (input === "w") return apply(deleteWordBackward(editorRef.current));
      if (input === "d") return apply(deleteForward(editorRef.current));
      return;
    }
    if (key.return) {
      // Alt+Enter 换行；普通 Enter 只把已有正文交给 application 层。
      if (key.meta) return insert("\n");
      const text = editorRef.current.value;
      if (!text.trim()) return;
      const empty = createLineEditor("");
      selfEdit.current = true;
      editorRef.current = empty;
      setEditor(empty);
      onSubmit(text);
      return;
    }
    if (key.backspace) return apply(deleteBackward(editorRef.current));
    if (key.delete) return apply(deleteForward(editorRef.current));
    // Alt+ASCII 仍留给快捷键；输入法上屏的汉字不要因为 meta 被丢掉。
    if (key.meta && isAsciiShortcut(input)) return;
    if (input) insert(input);
  }, { isActive: focused });

  return (
    <Box
      borderStyle={TERMINAL_DESIGN.borders.panel}
      borderColor={focused && !disabled ? TERMINAL_DESIGN.colors.accent : TERMINAL_DESIGN.colors.muted}
      paddingX={1}
    >
      <Box ref={inputLineRef} width="100%">
        <Text {...(disabled ? { color: TERMINAL_DESIGN.colors.muted } : {})}>
          <Text color={disabled ? TERMINAL_DESIGN.colors.muted : TERMINAL_DESIGN.colors.accent}>{prompt}</Text>
          {before}
          {after}
        </Text>
      </Box>
    </Box>
  );
}

function isAsciiShortcut(input: string): boolean {
  return !input || (input.length === 1 && input.charCodeAt(0) < 128);
}
