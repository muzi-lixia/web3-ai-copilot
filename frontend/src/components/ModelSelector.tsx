import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Modal, Select, Radio } from 'antd'
import { useState } from 'react'
import { getModel, selectModel } from '../api/resources'
import { useAuthStore } from '../stores/auth'

/** 切换本人会话的模型；外发确认与运行中互斥仍由服务端强制执行。 */
export default function ModelSelector({ disabled = false, variant = 'select' }: { disabled?: boolean; variant?: 'select' | 'list' }) {
  const address = useAuthStore(s => s.address)
  const cache = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const key = ['agent-model', address]
  const { data, isLoading } = useQuery({ queryKey: key, queryFn: getModel, enabled: !!address })
  async function change(provider: string, accepted: boolean) {
    setBusy(true); setError(null)
    try { await selectModel(provider, accepted); await cache.invalidateQueries({ queryKey: key }) }
    catch (e) { setError(e instanceof Error ? e.message : '切换失败') }
    finally { setBusy(false) }
  }
  function choose(provider: string) {
    if (provider === 'ollama') void change(provider, false)
    else Modal.confirm({ title: '确认使用在线模型', content: '安全化后的问题和对话元数据将发送给模型提供方。真实余额由基础服务展示，不作为模型输入。',
      okText: '同意并切换', cancelText: '继续用当前模型', onOk: () => change(provider, true) })
  }
  return <div>{variant === 'list' ? <Radio.Group className="model-options" value={data?.provider} disabled={!address || disabled || busy} onChange={e => choose(e.target.value)}>
    {(data?.available ?? [{ provider: 'ollama', model: null, enabled: false, external: false }, { provider: 'deepseek', model: null, enabled: false, external: true }, { provider: 'qwen', model: null, enabled: false, external: true }]).map(m => <div className="model-option" key={m.provider}><Radio value={m.provider} disabled={!m.enabled}>{m.external ? '在线' : '本地'} {m.provider}{m.model && ` · ${m.model}`}</Radio><small>{!address ? '登录后查看配置' : !m.enabled ? '管理员未启用' : m.external ? '对话元数据外发' : '不调用在线提供方'}</small></div>)}
  </Radio.Group> : <Select aria-label="对话模型" style={{ width: 184 }} value={data?.provider} placeholder={address ? '读取模型配置' : '登录后选择模型'} disabled={!address || disabled || busy}
    loading={busy || isLoading} options={data?.available.map(m => ({ value: m.provider, label: m.enabled ? `${m.provider} · ${m.model}` : `${m.provider} · 未配置`, disabled: !m.enabled }))} onChange={choose} />}
    {error && <Alert className={variant === 'select' ? 'model-error' : undefined} type="error" title={error} closable onClose={() => setError(null)} />}</div>
}
