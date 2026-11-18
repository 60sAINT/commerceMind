/**
 * 与后端 core/skill_selection.py 保持一致的 `/skill_id` 解析。
 * 前端只用它做“去重”和提示，不负责真正的技能选择。
 */

const COMMAND = /^\/([a-z0-9][a-z0-9_-]{0,63})(?=\s|$)/

export interface ParsedSkillCommands {
  ids: string[]
  rest: string
}

/** 解析消息开头连续的 `/skill_id`，返回去重后的 id 与剩余问题。 */
export function parseSkillCommands(message: string): ParsedSkillCommands {
  const ids: string[] = []
  let rest = message.trim()
  let match: RegExpExecArray | null

  while ((match = COMMAND.exec(rest)) !== null) {
    ids.push(match[1])
    rest = rest.slice(match[0].length).trimStart()
  }

  return { ids: [...new Set(ids)], rest }
}

/** 只保留手动命令里没有出现的技能 id，避免同一个技能被发送两次。 */
export function dedupeSkillIds(message: string, selectedIds: string[]): string[] {
  const manual = new Set(parseSkillCommands(message).ids)
  return [...new Set(selectedIds.filter((id) => !manual.has(id)))]
}

/** 消息是否只有技能命令、没有实际问题；用于提前给出友好提示。 */
export function isStandaloneSkillCommand(message: string): boolean {
  const trimmed = message.trim()
  if (!trimmed) return false
  const { ids, rest } = parseSkillCommands(trimmed)
  return ids.length > 0 && rest.length === 0
}
