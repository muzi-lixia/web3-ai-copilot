import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Spin, Table } from 'antd'
import type { CatalogEntry } from '../utils/openapi'
import { getCatalog, type ServiceName } from '../api/catalog'
import { getChains } from '../api/resources'
import { PageHeading, Panel, Stat, Note } from '../components/PageParts'

function ServiceCatalog({ service, label }: { service: ServiceName; label: string }) {
  const query = useQuery({ queryKey: ['service-catalog', service], queryFn: ({ signal }) => getCatalog(service, signal), staleTime: 60000, retry: false })
  const groups = new Map<string, CatalogEntry[]>()
  for (const entry of query.data?.entries ?? []) groups.set(entry.group, [...(groups.get(entry.group) ?? []), entry])
  return <div className="service-section"><div className="catalog-heading"><strong>{label} · {query.data?.title ?? 'OpenAPI'}</strong><Button size="small" loading={query.isFetching} onClick={() => void query.refetch()}>刷新目录</Button></div>
    {query.error && <Alert type="warning" showIcon title={`${label}接口目录读取失败`} description={query.error instanceof Error ? query.error.message : '服务不可用'} action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} style={{ marginBottom: 14 }} />}
    {query.error && query.data && <Note title="旧目录">当前显示上一次成功读取的文档；刷新失败，目录可能已过期。</Note>}
    {query.isPending && query.isFetching && <Spin tip="正在读取服务 OpenAPI"><div style={{ height: 80 }} /></Spin>}
    {query.data && !query.data.entries.length && <Empty description="服务文档未声明任何接口" />}
    {[...groups].map(([title, entries]) => <Panel key={title} title={title} sub={`${entries.length} 个接口 · 服务文档声明`} className="service-section"><Table size="small" pagination={false} rowKey="id" dataSource={entries} columns={[
      { title: '接口', render: (_, row) => <><span className="method">{row.method}</span><code className="code">{row.path}</code></> },
      { title: '用途', dataIndex: 'description' }, { title: '鉴权', dataIndex: 'auth' },
      { title: 'P95', render: () => <span className="muted">—</span> },
      { title: '状态', render: (_, row) => <span className={`proto-tag ${row.deprecated ? 'soon' : ''}`}>{row.deprecated ? '已弃用' : '已注册 · 未探测健康'}</span> },
    ]} /></Panel>)}
  </div>
}
export default function Services() {
  const { data: chains } = useQuery({ queryKey: ['chains'], queryFn: getChains })
  return <><PageHeading title="服务与接口" description="读取两个服务的 OpenAPI，目录随服务注册内容更新" extra={<span className="chip">两个独立服务</span>} />
    <div className="proto-grid four-cols" style={{ marginBottom: 16 }}><Stat value="—" label="接口健康统计（尚未接入）" /><Stat value="—" label="P95（尚未接入）" /><Stat value={chains?.length ?? '—'} label="已接入 EVM 网络" /><Stat value="—" label="越权拦截率（尚未接入）" /></div>
    <ServiceCatalog service="foundation" label="基础服务" /><ServiceCatalog service="agent" label="Agent 服务" />
    <Note title="状态说明">目录表示服务文档声明的接口，不代表逐接口健康检测。侧栏健康探针仅检查服务存活，P95 与错误率尚未采集。鉴权声明由服务提供，实际权限始终由后端执行。</Note></>
}
