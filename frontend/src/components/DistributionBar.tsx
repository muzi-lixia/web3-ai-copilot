import { Tooltip } from 'antd'

import type { Asset } from '../types/api'

import { PALETTE } from '../config/presentation'

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
