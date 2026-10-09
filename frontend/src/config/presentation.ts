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
  unknown: { label: '无法评估', color: '#8c8c8c', hint: '持仓为空或数据不完整' },
}

/**
 * 主强调色。
 *
 * 分布条第一段和单段占比条共用它 —— 同一个色值出现在两处时写两遍，
 * 改的时候必然漏一处。
 */
export const ACCENT = '#5B8FF9'

/**
 * 资产配色。
 *
 * 刻意不用 antd 的语义色（success / error 那一套）—— 那些颜色在这套界面上
 * 带着"涨/跌"的含义，用在纯粹的资产区分上会误导。
 *
 * 导出是必要的：分布条和持仓环形图各按一次序取色，同色表才能让同一个币
 * 在两处长得一样。各写各的色表，改一处必然漏另一处。
 */
export const PALETTE = [
  ACCENT,
  '#61DDAA',
  '#F6BD16',
  '#7262FD',
  '#78D3F8',
  '#9661BC',
  '#F6903D',
  '#008685',
]
