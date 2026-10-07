import { Tooltip } from 'antd'

import type { Asset } from '../types/api'

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

/**
 * 资产分布条：按估值占比横向堆叠。
 *
 * 占比为 null 的资产（无行情，算不出占比）不出现 —— 它们不进分母也不占宽度，
 * 因为"不知道该占多少"和"占 0%"是两回事。这些币由调用方单独提示。
 */
export function DistributionBar({ assets, height = 10 }: { assets: Asset[]; height?: number }) {
  const shares = assets.filter((asset) => (asset.percentage ?? 0) > 0)
  if (shares.length === 0) return null

  return (
    <div
      style={{
        display: 'flex',
        height,
        borderRadius: height / 2,
        overflow: 'hidden',
        background: '#f0f0f0',
      }}
    >
      {shares.map((asset, index) => (
        <Tooltip key={asset.symbol} title={`${asset.symbol} ${asset.percentage!.toFixed(2)}%`}>
          <div
            style={{
              width: `${asset.percentage}%`,
              background: PALETTE[index % PALETTE.length],
            }}
          />
        </Tooltip>
      ))}
    </div>
  )
}
