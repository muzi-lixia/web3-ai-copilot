import { useQuery } from '@tanstack/react-query'
import { Button } from 'antd'
import ModelSelector from '../components/ModelSelector'
import { Panel, Note, Stat, PageHeading } from '../components/PageParts'
import { getModel } from '../api/resources'
import { useAuthStore } from '../stores/auth'
import { useWalletLogin } from '../hooks/useWalletLogin'
const date = (value: string | null) => value ? new Date(value).toLocaleString() : '未读取'
function Row({ label, value }: { label: string; value: string }) { return <div className="set-row"><span className="k">{label}</span><span className="v">{value}</span></div> }
export default function Settings() {
  const auth = useAuthStore()
  const { logout } = useWalletLogin()
  const { data } = useQuery({ queryKey: ['agent-model', auth.address], queryFn: getModel, enabled: !!auth.address })
  return <><PageHeading title="会话与设置" description="凭证生命周期、模型 provider、数据外发说明" />
    <div className="proto-grid two-cols"><Panel title="登录会话"><Row label="绑定地址" value={auth.address ?? '未连接钱包'} /><Row label="地址来源" value="钱包签名验证 · 不可编辑" />
      <Row label="Session 到期" value={date(auth.sessionExpiresAt)} /><Row label="Access Token 到期" value={date(auth.expiresAt)} /><Row label="续期机制" value="到期自动续期，登录会话失效后重新签名" />
      <div className="panel-body"><Button block danger disabled={!auth.token} onClick={logout}>退出登录并销毁会话</Button></div></Panel>
    <Panel title="模型 provider"><div className="panel-body"><ModelSelector variant="list" /><div style={{ marginTop: 12 }}><Note title="外发边界" warning>在线模型接收安全化后的对话文本与元数据。钱包地址和资产明细不进入模型输入。本地模型不可用时，不自动切换在线模型。</Note></div></div></Panel>
    <Panel title="上下文与准确率"><Row label="上下文窗口" value={data?.context_window ? `${data.context_window} tokens（部署配置）` : '未读取'} /><Row label="上下文保留" value={data ? `${data.context_ttl_hours} 小时` : '未读取'} /><Row label="当前用量" value="暂未提供统计接口" /><Row label="模型准确率" value="尚未接入评测结果" /><div className="panel-body"><Note title="配置">窗口值来自部署配置，不表示已测得的实际用量或准确率。对话页可手动清空上下文与关联结果。</Note></div></Panel>
    <Panel title="数据与隐私"><Row label="可查询范围" value="仅当前凭证绑定的本人钱包" /><Row label="资产明细是否进模型" value="否 · 通过展示通道读取" /><Row label="请求追踪" value="结构化日志 + trace_id" /><Row label="日志脱敏" value="不记录钱包地址、余额、签名与凭证" /><div className="panel-body"><Note title="只读">当前服务不持有私钥，不构造或广播交易。</Note></div></Panel></div>
    <Panel title="运维指标" sub="受限区域 · 尚未开放运维接口" className="ops-panel"><div className="proto-grid three-cols ops-grid"><Stat value="—" label="越权拦截率" /><Stat value="—" label="实际生效上下文" /><Stat value="—" label="SSE 活跃连接数" /></div><div className="panel-body"><Note title="权限边界" warning>这里不请求或下发全站聚合指标。后续运维接口必须在服务端独立校验角色，前端隐藏不构成权限控制。</Note></div></Panel></>
}
