import { useCallback, useLayoutEffect, useRef, useState } from 'react'

/** 距离底部多少像素以内仍视为“跟随”，避免用户轻微上滑就被打断。 */
const FOLLOW_THRESHOLD = 96

/**
 * 智能滚动：
 * - 用户停留在底部附近时，新内容（含流式增量）自动跟随；
 * - 用户主动上滑阅读后不再抢滚动；
 * - resetKey 变化（切换会话）时强制回到底部。
 */
export function useSmartScroll(resetKey: string, follow: unknown) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const [atBottom, setAtBottom] = useState(true)

  const scrollToBottom = useCallback((smooth = false) => {
    const element = scrollRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  const handleScroll = useCallback(() => {
    const element = scrollRef.current
    if (!element) return
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight
    const stick = distance <= FOLLOW_THRESHOLD
    stickRef.current = stick
    setAtBottom(stick)
  }, [])

  useLayoutEffect(() => {
    stickRef.current = true
    scrollToBottom()
  }, [resetKey, scrollToBottom])

  useLayoutEffect(() => {
    if (stickRef.current) scrollToBottom()
  }, [follow, scrollToBottom])

  return { scrollRef, atBottom, handleScroll, scrollToBottom }
}
