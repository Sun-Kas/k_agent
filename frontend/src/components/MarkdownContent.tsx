import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

interface MarkdownContentProps {
  content: string;
}

/** Normalize common LLM math fences so remark-math can see them. */
function normalizeMathDelimiters(content: string): string {
  return content
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `$$${body}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => `$${body}$`);
}

export function MarkdownContent({ content }: MarkdownContentProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
    >
      {normalizeMathDelimiters(content)}
    </ReactMarkdown>
  );
}
