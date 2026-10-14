/** 一轮回答的结果块：模型正文 + 一个「数据来源与工具调用」折叠区。
 *
 * 折叠区里按工具列出每次调用，数据挂在产出它的那次调用下面（默认收起）。
 * 流式期间整块不出现，由调用方控制 —— 免得用户在看正文时下面已经在跳工具条目。
 */
import type { ReactNode } from 'react'

import { SourceFold } from './SourceDetail'
import ToolCalls from './ToolCalls'
import type { CopilotResult, ToolMeta } from '../types/api'

export default function TurnResult({ say, results, toolMeta }: {
    say?: ReactNode
    results: CopilotResult[]
    toolMeta?: ToolMeta
}) {
    // 认领不到的数据单独列出：缺工具轨迹的历史消息，或轨迹里没有的产出（如合并后被删的）。
    const claimed = new Set((toolMeta?.calls ?? []).flatMap(call => call.result_ids ?? []))
    const orphans = results.filter(result => !claimed.has(result.result_id))

    return (
        <div className="blk">
            <div className="blk-b">{say}</div>
            <details className="fold">
                <summary>
                    数据来源与工具调用
                    <span className="rt">
                        {toolMeta
                            ? `${toolMeta.calls.length} 次调用 · ${toolMeta.total_duration_ms}ms`
                            : `${results.length} 条数据`}
                    </span>
                    <span className="arw" />
                </summary>
                <div className="blk-b">
                    {!!toolMeta?.calls.length && <ToolCalls meta={toolMeta} results={results} />}
                    {orphans.map(result => <SourceFold key={result.result_id} result={result} />)}
                </div>
            </details>
        </div>
    )
}
