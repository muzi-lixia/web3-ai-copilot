/** 工具调用轨迹：每次调用一条，条目里挂它自己产出的数据（默认收起）。
 *
 * 参数的记录早于校验 —— 参数被拒的调用同样要留痕，那正是要观测的部分。
 */
import { useState } from 'react'

import { SourceFold } from './SourceDetail'
import type { CopilotResult, ToolMeta } from '../types/api'

const STATE_LABEL: Record<string, string> = { failed: '失败', rejected: '参数被拒' }

export default function ToolCalls({ meta, results }: { meta: ToolMeta; results: CopilotResult[] }) {
    const [copied, setCopied] = useState(false)

    const copy = async () => {
        if (!meta.trace_id) return
        try {
            await navigator.clipboard.writeText(meta.trace_id)
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
        } catch {
            // 剪贴板不可用（无权限 / 非安全上下文）时不提示，静默即可。
        }
    }

    if (!meta.calls.length) return null
    const byId = new Map(results.map(result => [result.result_id, result]))

    return (
        <>
            {meta.calls.map((call, index) => {
                // 认领这次调用产出的数据；被合并工具删掉的结果已不在列表里，查不到就跳过。
                const owned = (call.result_ids ?? [])
                    .map(id => byId.get(id))
                    .filter((result): result is CopilotResult => Boolean(result))
                return (
                    <div className="tcall" key={`${call.tool}:${index}`}>
                        <div className="tc-h">
                            <span className="tc-n">{call.tool}</span>
                            {call.status && STATE_LABEL[call.status] && (
                                <span className="tc-m">{STATE_LABEL[call.status]}</span>
                            )}
                            {call.duration_ms != null && <span className="tc-m">{call.duration_ms}ms</span>}
                        </div>
                        {/* 参数行常显：模型未传参数时是空对象，也要显示出来 —— 否则调用看起来像缺了参数。 */}
                        <div className="tc-p">{JSON.stringify(call.args ?? {})}</div>
                        {owned.map(result => <SourceFold key={result.result_id} result={result} />)}
                    </div>
                )
            })}

            {/* 原型把动作放左、说明靠右；说明那句已按用户要求去掉，只留动作。 */}
            <div className="blk-f">
                {meta.trace_id && (
                    <button type="button" className="chipbtn" onClick={() => void copy()}>
                        {copied ? '已复制' : '复制 trace'}
                    </button>
                )}
            </div>
        </>
    )
}
