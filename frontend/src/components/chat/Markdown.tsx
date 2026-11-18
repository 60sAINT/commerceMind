import { isValidElement, memo, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'

const REMARK_PLUGINS = [remarkGfm]
const REHYPE_PLUGINS = [rehypeHighlight]

/** 递归提取节点文本，用于代码块复制（高亮后文本被拆成多个 span）。 */
function nodeText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(nodeText).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children)
  return ''
}

function CodeBlock({
  code,
  language,
  className,
  children,
}: {
  code: string
  language?: string
  className?: string
  children: ReactNode
}) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    },
    [],
  )

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(() => setCopied(false), 1600)
    } catch {
      // 剪贴板不可用时保持原样，不打断阅读。
    }
  }, [code])

  return (
    <div className="cm-code my-3">
      <div className="flex items-center justify-between gap-3 border-b border-line px-3 py-1.5">
        <span className="truncate font-mono text-[11px] text-faint">{language || 'text'}</span>
        <button
          type="button"
          onClick={copy}
          aria-label={copied ? '代码已复制' : '复制代码'}
          className="inline-flex items-center gap-1.5 rounded px-1.5 py-1 text-[11px] text-muted transition-colors hover:bg-hover hover:text-ink"
        >
          {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre>
        <code className={className}>{children}</code>
      </pre>
    </div>
  )
}

const COMPONENTS: Components = {
  pre({ children }) {
    const child = Array.isArray(children) ? children[0] : children
    if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) {
      return <pre>{children}</pre>
    }
    const className = child.props.className
    const language = /language-([\w-]+)/.exec(className ?? '')?.[1]
    return (
      <CodeBlock code={nodeText(child.props.children)} language={language} className={className}>
        {child.props.children}
      </CodeBlock>
    )
  },
  // 链接一律新窗口打开，并阻断 opener / referrer。
  a({ href, title, children }) {
    return (
      <a href={href} title={title} target="_blank" rel="noopener noreferrer nofollow">
        {children}
      </a>
    )
  },
  table({ children }) {
    return (
      <div className="cm-table-wrap">
        <table>{children}</table>
      </div>
    )
  },
}

interface MarkdownProps {
  content: string
}

/** 助手消息 Markdown；memo 避免未变化的消息重复解析。 */
export const Markdown = memo(function Markdown({ content }: MarkdownProps) {
  return (
    <div className="cm-markdown">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={COMPONENTS}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
})
