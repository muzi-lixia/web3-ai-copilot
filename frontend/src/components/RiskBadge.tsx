import type { RiskLevel } from '../types/api'

import { LEVEL_META } from '../config/presentation'

/**
 * 风险等级色块。
 *
 * `unknown` 也走同一个组件 —— 它是**输入不足**而不是"第三种风险"，
 * 用灰色显示既表达了这一点，又不会让人误读成"低风险"。
 */
export function RiskBadge({ level }: { level: RiskLevel }) {
  const meta = LEVEL_META[level]

  return (
    <span
      style={{
        background: meta.color,
        color: '#fff',
        borderRadius: 4,
        padding: '2px 12px',
        fontSize: 16,
        fontWeight: 600,
      }}
    >
      {meta.label}
    </span>
  )
}
