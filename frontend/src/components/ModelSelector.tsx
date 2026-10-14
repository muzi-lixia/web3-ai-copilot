import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Modal, Select, Radio } from 'antd'
import { useState } from 'react'

import { getModel, selectModel, type ModelOption } from '../api/resources'
import { useAuthStore } from '../stores/auth'

/** 服务端还没返回可用模型时先占位，保证列表形状稳定。 */
const PLACEHOLDER_OPTIONS: ModelOption[] = [
    { provider: 'ollama', model: null, enabled: false, external: false },
    { provider: 'deepseek', model: null, enabled: false, external: true },
    { provider: 'qwen', model: null, enabled: false, external: true },
]

/** 切换本人会话的模型；外发确认与运行中互斥仍由服务端强制执行。 */
export default function ModelSelector({ disabled = false, variant = 'select' }: {
    disabled?: boolean
    variant?: 'select' | 'list'
}) {
    const address = useAuthStore(s => s.address)
    const cache = useQueryClient()
    const [error, setError] = useState<string | null>(null)
    const [busy, setBusy] = useState(false)
    const key = ['agent-model', address]
    const { data, isLoading } = useQuery({ queryKey: key, queryFn: getModel, enabled: !!address })

    async function change(provider: string, accepted: boolean) {
        setBusy(true)
        setError(null)
        try {
            await selectModel(provider, accepted)
            await cache.invalidateQueries({ queryKey: key })
        } catch (e) {
            setError(e instanceof Error ? e.message : '切换失败')
        } finally {
            setBusy(false)
        }
    }

    function choose(provider: string) {
        // 本地模型不出网，直接用；在线模型要先让用户确认数据外发。
        if (provider === 'ollama') {
            void change(provider, false)
            return
        }
        Modal.confirm({
            title: '确认使用在线模型',
            content: '脱敏后的问题、必要的对话历史、本次查询的持仓币种、数量、估值及知识库引用片段将发送给所选模型提供方，用于生成回答。模型消息会保存在服务端，可通过清空对话删除。请勿输入私钥或助记词。',
            okText: '同意并切换',
            cancelText: '继续用当前模型',
            onOk: () => change(provider, true),
        })
    }

    const options = data?.available ?? PLACEHOLDER_OPTIONS

    return (
        <div>
            {variant === 'list' ? (
                <Radio.Group
                    className="model-options"
                    value={data?.provider}
                    disabled={!address || disabled || busy}
                    onChange={e => choose(e.target.value)}
                >
                    {options.map(m => (
                        <div className="model-option" key={m.provider}>
                            <Radio value={m.provider} disabled={!m.enabled}>
                                {m.external ? '在线' : '本地'} {m.provider}{m.model && ` · ${m.model}`}
                            </Radio>
                            <small>
                                {!address
                                    ? '登录后查看配置'
                                    : !m.enabled
                                        ? '管理员未启用'
                                        : m.external ? '对话与持仓数据外发' : '不调用在线提供方'}
                            </small>
                        </div>
                    ))}
                </Radio.Group>
            ) : (
                <Select
                    aria-label="对话模型"
                    style={{ width: 184 }}
                    value={data?.provider}
                    placeholder={address ? '读取模型配置' : '登录后选择模型'}
                    disabled={!address || disabled || busy}
                    loading={busy || isLoading}
                    options={data?.available.map(m => ({
                        value: m.provider,
                        label: m.enabled ? `${m.provider} · ${m.model}` : `${m.provider} · 未配置`,
                        disabled: !m.enabled,
                    }))}
                    onChange={choose}
                />
            )}

            {error && (
                <Alert
                    className={variant === 'select' ? 'model-error' : undefined}
                    type="error"
                    title={error}
                    closable
                    onClose={() => setError(null)}
                />
            )}
        </div>
    )
}
