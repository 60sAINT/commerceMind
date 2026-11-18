import { ArrowUpRight, MessageSquarePlus, Sparkles } from 'lucide-react'

const QUICK_QUESTIONS = [
  { label: '退款一般多久到账？', hint: '售后与退款时效' },
  { label: '订单显示签收但我没收到货', hint: '物流异常处理' },
  { label: '如何修改收货地址？', hint: '订单信息变更' },
  { label: '帮我对比这两款商品', hint: '选购建议' },
]

interface EmptyStateProps {
  onAsk: (question: string) => void
  disabled?: boolean
}

export function EmptyState({ onAsk, disabled }: EmptyStateProps) {
  return (
    <div className="m-auto w-full max-w-xl py-10 text-center md:py-16">
      <div className="mx-auto grid size-12 place-items-center rounded-xl border border-line bg-elevated shadow-soft">
        <MessageSquarePlus className="size-5 text-muted" />
      </div>
      <h2 className="mt-5 text-xl font-semibold tracking-tight md:text-2xl">有什么可以帮你？</h2>
      <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-muted">
        描述订单、商品或售后问题，CommerceMind 会结合会话上下文给出协助。
      </p>

      <div className="mt-7 grid gap-2 text-left sm:grid-cols-2">
        {QUICK_QUESTIONS.map((question) => (
          <button
            key={question.label}
            type="button"
            disabled={disabled}
            onClick={() => onAsk(question.label)}
            className="group flex items-start justify-between gap-3 rounded-lg border border-line bg-elevated px-3.5 py-3 text-left transition-colors hover:border-line-strong hover:bg-soft disabled:opacity-50"
          >
            <span className="min-w-0">
              <span className="block text-xs font-medium leading-5">{question.label}</span>
              <span className="mt-0.5 block text-[11px] text-faint">{question.hint}</span>
            </span>
            <ArrowUpRight className="mt-0.5 size-3.5 shrink-0 text-faint transition-transform group-hover:-translate-y-0.5 group-hover:text-ink" />
          </button>
        ))}
      </div>

      <p className="mt-6 inline-flex items-center gap-1.5 text-[11px] text-faint">
        <Sparkles className="size-3" />
        支持 Markdown、代码块与多轮追问
      </p>
    </div>
  )
}
