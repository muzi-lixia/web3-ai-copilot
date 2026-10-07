import { Empty, Typography } from 'antd'

import type { Asset } from '../types/api'
import { formatValue } from '../utils/format'
import { PALETTE } from './DistributionBar'

/** 环的直径与环宽。宽度定了内圈就有 116px，够放"总资产 + 金额"两行。 */
const SIZE = 168
const STROKE = 26

/** 相邻两段之间留的缝。不留缝时两个相近的颜色会连成一片，看不出分界。 */
const GAP = 1.5

/** 「其他」那一段用的灰色：它不是一个币，不该占用资产配色里的任何一色。 */
const REST_COLOR = '#d9d9d9'

interface Slice {
  key: string
  symbol: string
  /** 0-100 */
  share: number
  /** null 表示不展示金额（聚合出来的"其他"没有单一金额） */
  value: string | null
  color: string
}

/**
 * 持仓环形图 + 图例。
 *
 * 用 SVG 的 stroke-dasharray 画弧，没有引入图表库 —— 这里要做的事只有
 * "一眼看出构成"，为它拉进一个通用图表引擎，体积、主题、响应式都得重新对齐，
 * 收益却只是同一个圆。色序取自 DistributionBar 的 PALETTE，两处的同色即同币。
 *
 * 占比之和不足 100%（持仓多于 topCount）时补一段「其他」——
 * 否则环上会缺一块，看起来像少画了。金额无法合并显示：那是后端 Decimal
 * 字符串之和，前端不做这个运算（见 utils/format.ts 的原则）。
 */
export function HoldingsDonut({
  assets,
  totalValueUsd,
  topCount = 5,
}: {
  /** 有估值的持仓（percentage 非 null），顺序不限 */
  assets: Asset[]
  totalValueUsd: string
  topCount?: number
}) {
  const sorted = [...assets].sort((a, b) => (b.percentage ?? 0) - (a.percentage ?? 0))
  const top = sorted.slice(0, topCount)
  const rest = sorted.slice(topCount)
  const restShare = rest.reduce((sum, asset) => sum + (asset.percentage ?? 0), 0)

  const slices: Slice[] = top.map((asset, index) => ({
    key: asset.contract ?? 'native',
    symbol: asset.symbol,
    share: asset.percentage!,
    value: asset.value_usd,
    color: PALETTE[index % PALETTE.length],
  }))

  if (rest.length > 0) {
    slices.push({
      key: 'rest',
      symbol: `其他 ${rest.length} 项`,
      share: restShare,
      value: null,
      color: REST_COLOR,
    })
  }

  if (slices.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可估值的持仓" />
  }

  const radius = (SIZE - STROKE) / 2
  const circumference = 2 * Math.PI * radius
  const center = SIZE / 2

  // 每段的起点 = 它前面所有段的占比之和。段数就这么几段，不值得为此维护一个
  // 在渲染过程中被改写的游标 —— 那种写法 React 编译器不认，会跳过对组件的优化。
  const arcs = slices.map((slice, index) => {
    const start = slices.slice(0, index).reduce((sum, item) => sum + item.share, 0)
    const length = (slice.share / 100) * circumference
    return {
      slice,
      // 极小的持仓（0.01%）扣掉缝就没了，留一条细线表示"这里确实还有东西"。
      dash: Math.max(length - GAP, 0.6),
      // 取负值让每段接在前一段末尾，而不是各自从头绕。
      offset: -((start / 100) * circumference),
    }
  })

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 32 }}>
      <div style={{ position: 'relative', width: SIZE, height: SIZE, flexShrink: 0 }}>
        <svg width={SIZE} height={SIZE} role="img" aria-label="持仓构成">
          {/* 底层灰环：数据里的占比不足 100% 时，露出来的是它而不是空白 */}
          <circle cx={center} cy={center} r={radius} fill="none" stroke="#f0f0f0" strokeWidth={STROKE} />
          {arcs.map(({ slice, dash, offset }) => (
            <circle
              key={slice.key}
              cx={center}
              cy={center}
              r={radius}
              fill="none"
              stroke={slice.color}
              strokeWidth={STROKE}
              strokeDasharray={`${dash} ${circumference - dash}`}
              strokeDashoffset={offset}
              transform={`rotate(-90 ${center} ${center})`}
            >
              {/* 原生 title：悬浮出提示，不为它引一个 tooltip 层 */}
              <title>{`${slice.symbol} ${slice.share.toFixed(2)}%`}</title>
            </circle>
          ))}
        </svg>
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            pointerEvents: 'none',
          }}
        >
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            总资产
          </Typography.Text>
          <Typography.Text strong style={{ fontSize: 18 }}>
            {formatValue(totalValueUsd)}
          </Typography.Text>
        </div>
      </div>

      <div style={{ flex: '1 1 300px', minWidth: 260 }}>
        {slices.map((slice) => (
          <div
            key={slice.key}
            style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '5px 0' }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: 4,
                background: slice.color,
                flexShrink: 0,
              }}
            />
            <Typography.Text strong style={{ flex: 1 }}>
              {slice.symbol}
            </Typography.Text>
            <Typography.Text style={{ width: 68, textAlign: 'right', fontSize: 13 }}>
              {slice.share.toFixed(2)}%
            </Typography.Text>
            <Typography.Text strong style={{ width: 96, textAlign: 'right' }}>
              {slice.value === null ? '' : formatValue(slice.value)}
            </Typography.Text>
          </div>
        ))}
      </div>
    </div>
  )
}
