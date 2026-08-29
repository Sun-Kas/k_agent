import React from "react";
import { Box, Text } from "ink";
import { lexer, type Token, type Tokens } from "marked";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { TERMINAL_DESIGN } from "../design.js";
import { CodeBlock } from "./CodeBlock.js";

/**
 * 把助手 Markdown 编成 Ink 排版：粗体、行内代码、列表、标题和围栏代码。
 * 先清理不可信控制字符，再词法分析，避免把 OSC/ANSI 当 Markup。
 */
export function MarkdownBlock({ content }: { content: string }): React.ReactElement {
  const tokens = lexer(sanitizeTerminalContent(content));
  return (
    <Box flexDirection="column">
      {tokens.map((token, index) => <BlockToken key={`${token.type}-${index}`} token={token} />)}
    </Box>
  );
}

function BlockToken({ token }: { token: Token }): React.ReactElement | null {
  if (token.type === "space") return null;
  if (token.type === "heading") {
    const heading = token as Tokens.Heading;
    const body = inline(heading.tokens ?? [{ type: "text", raw: heading.text, text: heading.text } as Tokens.Text]);
    if (heading.depth === 1) return <Text bold color={TERMINAL_DESIGN.colors.accent} wrap="wrap">{body}</Text>;
    return <Text bold wrap="wrap">{body}</Text>;
  }
  if (token.type === "paragraph") {
    const paragraph = token as Tokens.Paragraph;
    return <Text wrap="wrap">{inline(paragraph.tokens ?? [])}</Text>;
  }
  if (token.type === "blockquote") {
    const quote = token as Tokens.Blockquote;
    return (
      <Box flexDirection="column" borderStyle="single" borderLeft borderRight={false} borderTop={false} borderBottom={false} borderColor={TERMINAL_DESIGN.colors.muted} paddingLeft={1}>
        {quote.tokens.map((child, index) => <BlockToken key={index} token={child} />)}
      </Box>
    );
  }
  if (token.type === "list") {
    const list = token as Tokens.List;
    return (
      <Box flexDirection="column">
        {list.items.map((item, index) => (
          <Box key={index} flexDirection="row">
            <Text color={TERMINAL_DESIGN.colors.muted}>{list.ordered ? `${Number(list.start || 1) + index}. ` : `${TERMINAL_DESIGN.symbols.unread} `}</Text>
            <Box flexDirection="column" flexGrow={1}>
              {item.tokens.map((child, childIndex) => <BlockToken key={childIndex} token={child} />)}
            </Box>
          </Box>
        ))}
      </Box>
    );
  }
  if (token.type === "code") {
    const code = token as Tokens.Code;
    return <CodeBlock content={code.text} language={code.lang || "text"} />;
  }
  if (token.type === "hr") {
    return <Text color={TERMINAL_DESIGN.colors.muted}>──</Text>;
  }
  if (token.type === "table") {
    return <TableToken token={token as Tokens.Table} />;
  }
  if (token.type === "html") {
    return <Text wrap="wrap">{stripTags((token as Tokens.HTML).text)}</Text>;
  }
  if (token.type === "text") {
    const text = token as Tokens.Text;
    return <Text wrap="wrap">{text.tokens ? inline(text.tokens) : text.text}</Text>;
  }
  return <Text wrap="wrap">{token.raw}</Text>;
}

function TableToken({ token }: { token: Tokens.Table }): React.ReactElement {
  const rows = [token.header, ...token.rows];
  return (
    <Box flexDirection="column">
      {rows.map((row, rowIndex) => {
        const label = row.map((cell) => cell.text.trim()).filter(Boolean).join("  ·  ");
        if (rowIndex === 0) return <Text key={rowIndex} wrap="wrap" color={TERMINAL_DESIGN.colors.muted}>{label}</Text>;
        return <Text key={rowIndex} wrap="wrap">{label}</Text>;
      })}
    </Box>
  );
}

function inline(tokens: Token[]): React.ReactNode[] {
  return tokens.map((token, index) => <InlineToken key={`${token.type}-${index}`} token={token} />);
}

function InlineToken({ token }: { token: Token }): React.ReactElement {
  if (token.type === "strong") {
    return <Text bold>{inline((token as Tokens.Strong).tokens)}</Text>;
  }
  if (token.type === "em") {
    return <Text italic>{inline((token as Tokens.Em).tokens)}</Text>;
  }
  if (token.type === "del") {
    return <Text dimColor strikethrough>{inline((token as Tokens.Del).tokens)}</Text>;
  }
  if (token.type === "codespan") {
    return <Text color={TERMINAL_DESIGN.colors.accent}>{(token as Tokens.Codespan).text}</Text>;
  }
  if (token.type === "link") {
    const link = token as Tokens.Link;
    return (
      <Text>
        {inline(link.tokens)}
        {link.href && link.href !== link.text ? <Text color={TERMINAL_DESIGN.colors.muted}> ({link.href})</Text> : null}
      </Text>
    );
  }
  if (token.type === "br") return <Text>{"\n"}</Text>;
  if (token.type === "escape") return <Text>{(token as Tokens.Escape).text}</Text>;
  if (token.type === "image") {
    const image = token as Tokens.Image;
    return <Text color={TERMINAL_DESIGN.colors.muted}>[{image.text || "image"}]</Text>;
  }
  if (token.type === "text") {
    const text = token as Tokens.Text;
    return <Text>{text.tokens ? inline(text.tokens) : text.text}</Text>;
  }
  if ("tokens" in token && Array.isArray(token.tokens)) {
    return <Text>{inline(token.tokens)}</Text>;
  }
  if ("text" in token && typeof token.text === "string") {
    return <Text>{token.text}</Text>;
  }
  return <Text>{token.raw}</Text>;
}

function stripTags(value: string): string {
  return value.replace(/<[^>]+>/g, "");
}
