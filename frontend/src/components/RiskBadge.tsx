import type { RiskLevel } from '../types/api'

/**
 * 风险档的展示元信息。
 *
 * 这里的红/橙/绿是**通用危险度语义**，与项目里金融数字的「涨红跌绿」约定无关 ——
 * 后者用于价格涨跌，两者不该互相套用。
 */
export const LEVEL_META: Record<RiskLevel, { label: string; color: string; hint: string }> = {
  low: { label: '低', color: '#52c41a', hint: '三项判据均未触发' },
  medium: { label: '中', color: '#faad14', hint: '触发 1 项判据' },
  high: { label: '高', color: '#ff4d4f', hint: '触发 2 项及以上判据' },
  unknown: { label: '无法评估', color: '#8c8c8c', hint: '没有可估值的持仓' },
}

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
