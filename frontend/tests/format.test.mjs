// 金额只做字符串格式化，整数主体不能因被裁掉的极小尾数而消失。
import assert from 'node:assert/strict'
import test from 'node:test'
import { formatAmount, formatValue } from '../src/utils/format.ts'

test('tiny fractional tails never turn a nonzero integer balance into a tiny threshold', () => {
  assert.equal(formatAmount('1.00000001'), '1')
  assert.equal(formatAmount('12345678901234567890.000000001'), '12,345,678,901,234,567,890')
  assert.equal(formatAmount('0.00000001'), '<0.000001')
  assert.equal(formatAmount('0.00000000'), '0')
  assert.equal(formatAmount(null), '—')
})

test('small negative earnings remain distinguishable from zero', () => {
  assert.equal(formatAmount('-0.00000001'), '>-0.000001')
  assert.equal(formatAmount('-1.00000001'), '-1')
  assert.equal(formatValue('-0.0001'), '>-$0.01')
  assert.equal(formatValue('0.0001'), '<$0.01')
  assert.equal(formatValue('0'), '$0.00')
})
