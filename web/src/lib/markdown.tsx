import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface MarkdownProps {
  children: string;
  className?: string;
}

export function Markdown({ children, className = "" }: MarkdownProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      className={className}
      components={{
        p: ({ children }) => (
          <p className="mb-2 last:mb-0 text-[12.5px] text-text-muted leading-relaxed">{children}</p>
        ),
        strong: ({ children }) => (
          <strong className="font-semibold text-text">{children}</strong>
        ),
        em: ({ children }) => <em className="italic">{children}</em>,
        h1: ({ children }) => (
          <h1 className="text-[14px] font-bold text-text-bright mb-2 mt-3">{children}</h1>
        ),
        h2: ({ children }) => (
          <h2 className="text-[13px] font-semibold text-text-bright mb-2 mt-3">{children}</h2>
        ),
        h3: ({ children }) => (
          <h3 className="text-[12px] font-semibold text-text mb-1.5 mt-2">{children}</h3>
        ),
        ul: ({ children }) => (
          <ul className="list-disc list-inside space-y-1 mb-2 text-[12.5px] text-text-muted">{children}</ul>
        ),
        ol: ({ children }) => (
          <ol className="list-decimal list-inside space-y-1 mb-2 text-[12.5px] text-text-muted">{children}</ol>
        ),
        li: ({ children }) => <li className="leading-relaxed">{children}</li>,
        code: ({ inline, children }: { inline?: boolean; children?: React.ReactNode }) =>
          inline ? (
            <code className="px-1 py-0.5 bg-white/[0.06] border border-white/[0.08] rounded text-[11.5px] font-mono text-accent">
              {children}
            </code>
          ) : (
            <pre className="mt-2 mb-2 p-3 bg-[#06060A] border border-white/[0.07] rounded-xl overflow-x-auto">
              <code className="text-[11.5px] font-mono text-text-muted whitespace-pre">{children}</code>
            </pre>
          ),
        blockquote: ({ children }) => (
          <blockquote className="border-l-2 border-accent/40 pl-3 my-2 text-text-dim italic">
            {children}
          </blockquote>
        ),
        hr: () => <hr className="border-white/[0.06] my-3" />,
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
