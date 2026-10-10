/**
 * 展示层格式化。
 *
 * 全部是**纯字符串操作，不经过 number**。后端把金额序列化成十进制字符串，
 * 就是为了不让链上 bigint 走一遍 float64；这里再转成 number 等于把那份精度
 * 丢在最后一公里，而且丢得毫无必要 —— 加千分位、裁小数位都是字符串能做的事。
 *
 * 这些函数只负责显示裁剪，不参与任何计算。
 */

/**
 * 数量裁剪：保留 6 位有效小数。
 *
 * 再多的位数对"我持有多少"这个判断没有帮助，但也不能直接砍成 0 ——
 * 小于 1e-6 的非零余额显示成 `<0.000001`，否则会被误读成"这个币没有余额"。
 */
export function formatAmount(amount: string | null): string {
  if (amount === null) return "—"
  const [int, frac = ''] = amount.split('.')
  const head = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const tail = frac.slice(0, 6).replace(/0+$/, '')

  if (tail) return `${head}.${tail}`
  // 只有整数部分为零的微量余额才使用阈值提示，1.00000001 不能误显示成小于百万分之一。
  if (/^-?0$/.test(int) && /[1-9]/.test(frac)) {
    return int.startsWith('-') ? '>-0.000001' : '<0.000001'
  }
  return head
}

/**
 * USD 金额展示：到分为止。
 *
 * 有值但不到一分显示 `<$0.01` 而不是 `$0.00` —— 后者读起来像"没有资产"。
 */
export function formatValue(value: string | null, currency = 'USD'): string {
  if (value === null) return '—'
  const [int, frac = ''] = value.split('.')
  const head = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const cents = frac.slice(0, 2).padEnd(2, '0')
  const sign = currency === 'CNY' ? '¥' : '$'

  if (/^-?0$/.test(int) && cents === '00' && /[1-9]/.test(frac)) {
    return int.startsWith('-') ? `>-${sign}0.01` : `<${sign}0.01`
  }
  return `${sign}${head}.${cents}`
}

/**
 * 后端给 0-1 的比例，展示层乘 100 并裁剪。
 *
 * null 原样透传（**不返回 "0%"**）—— 由调用方决定怎么表达"这个值没有算出来"，
 * 它和"算出来是 0"是两回事。
 */
export function formatRatio(value: number | null, digits = 1): string | null {
  return value === null ? null : `${(value * 100).toFixed(digits)}%`
}

/** 0x1234…abcd —— 完整地址放 Tooltip 或 title 里。 */
export function shorten(address: string): string {
  return `${address.slice(0, 6)}…${address.slice(-4)}`
}


/** USD 正文与悬停提示均最多保留 6 位小数，去除尾零。
 * 仅操作十进制字符串，不改变后端金额，也不引入浮点精度损失。
 */
export function formatUsd(value: string | null): string {
  if (value === null) return '—'
  const [int, frac = ''] = value.split('.')
  const head = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const tail = frac.slice(0, 6).replace(/0+$/, '')
  if (/^-?0$/.test(int) && !tail && /[1-9]/.test(frac)) {
    return int.startsWith('-') ? '>-$0.000001' : '<$0.000001'
  }
  return `$${head}${tail ? '.' + tail : ''}`
}
