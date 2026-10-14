import { Alert, Button, Popconfirm, Space, Table, Tag, Typography, Upload } from 'antd'
import type { TableProps } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fetchDocuments, reindexDocument, removeDocument, retryDocument, uploadDocument } from '../api/knowledge'
import { Note, Panel } from '../components/PageParts'
import { useSessionProbe } from '../hooks/useSessionProbe'
import { useAuthStore } from '../stores/auth'
import type { DocumentStatus, KnowledgeDocument } from '../types/api'

/** 后台处理是异步的，标签要说清现在走到哪一步，而不是只给个颜色。 */
const STATUS: Record<DocumentStatus, { text: string; color?: string }> = {
    pending: { text: '排队中' },
    processing: { text: '处理中', color: 'processing' },
    ready: { text: '可检索', color: 'green' },
    failed: { text: '失败', color: 'red' },
}

/** 失败原因多是解析器或依赖服务的原文，单看它分不出该换文件还是等服务；按阶段补一句人话。 */
const STAGE_HINT: Record<string, string> = {
    load: '文件解析失败',
    clean: '内容清洗失败',
    split: '内容切分失败',
    embed: '向量化服务不可用',
    unknown: '处理未预料地中断',
}

/** 体积只到 MB，够判断文件量级；更细的单位对"要不要传"没有帮助。 */
function humanSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`
    const kb = bytes / 1024
    return kb < 1024 ? `${kb.toFixed(1)} KB` : `${(kb / 1024).toFixed(1)} MB`
}

/**
 * 知识库文档管理。数据源：/knowledge/documents。
 *
 * 离线链路的进度只能从这里看：上传只登记一行，解析、切分、向量化都在后台跑。
 * 所以列表在处理中会重读，而不是上传成功后当它已经入库。
 */
export default function Knowledge() {
    const token = useAuthStore((state) => state.token)
    // 与顶部栏的会话探针共用同一个查询键，读白名单状态不会多发一次 /users/me。
    const me = useSessionProbe(token)
    const isAdmin = me.data?.is_admin === true
    const queryClient = useQueryClient()

    const documents = useQuery({
        queryKey: ['knowledge', 'documents'],
        queryFn: fetchDocuments,
        enabled: Boolean(token),
        // 有文档没到终态就每 2 秒重读；全部跑完就停下来，不让页面空转。
        refetchInterval: (query) =>
            query.state.data?.some((row) => row.status === 'pending' || row.status === 'processing')
                ? 2000
                : false,
    })

    const refreshList = () => void queryClient.invalidateQueries({ queryKey: ['knowledge', 'documents'] })
    const upload = useMutation({ mutationFn: uploadDocument, onSuccess: refreshList })
    const remove = useMutation({ mutationFn: removeDocument, onSuccess: refreshList })
    const retry = useMutation({ mutationFn: retryDocument, onSuccess: refreshList })
    const reindex = useMutation({ mutationFn: reindexDocument, onSuccess: refreshList })

    const rows = documents.data ?? []
    const pending = rows.filter((row) => row.status === 'pending' || row.status === 'processing').length
    const failure = upload.error ?? remove.error ?? retry.error ?? reindex.error ?? documents.error

    const columns: TableProps<KnowledgeDocument>['columns'] = [
        {
            title: '文档',
            key: 'title',
            render: (_, row) => (
                <Space orientation="vertical" size={0}>
                    <Typography.Text strong>{row.title}</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {row.filename}
                    </Typography.Text>
                </Space>
            ),
        },
        {
            title: '体积',
            key: 'size_bytes',
            align: 'right',
            render: (_, row) => humanSize(row.size_bytes),
        },
        {
            title: '状态',
            key: 'status',
            render: (_, row) => {
                const stale = row.status === 'ready' && row.index_compatible === false
                return (
                    <Space orientation="vertical" size={0}>
                        <Tag color={stale ? 'orange' : STATUS[row.status].color}>
                            {stale ? '需要重建索引' : STATUS[row.status].text}
                        </Tag>
                        {row.error !== null && (
                            <Typography.Text type="danger" style={{ fontSize: 12 }}>
                                {row.stage ? `${STAGE_HINT[row.stage] ?? row.stage}：${row.error}` : row.error}
                            </Typography.Text>
                        )}
                    </Space>
                )
            },
        },
        {
            title: '分片',
            key: 'chunk_count',
            align: 'right',
            render: (_, row) => row.chunk_count,
        },
        {
            title: '上传时间',
            key: 'created_at',
            render: (_, row) => new Date(row.created_at).toLocaleString(),
        },
        {
            title: '操作',
            key: 'actions',
            align: 'right',
            render: (_, row) => (
                <Space size={4}>
                    {row.status === 'failed' && (
                        <Button
                            size="small"
                            loading={retry.isPending && retry.variables === row.id}
                            onClick={() => retry.mutate(row.id)}
                        >
                            重试
                        </Button>
                    )}
                    {row.status === 'ready' && (
                        <Popconfirm
                            title="从原件重建索引？"
                            description="重建完成前，这份文档暂不参与检索。"
                            onConfirm={() => reindex.mutate(row.id)}
                            okText="重建"
                            cancelText="取消"
                        >
                            <Button size="small" loading={reindex.isPending && reindex.variables === row.id}>
                                重建索引
                            </Button>
                        </Popconfirm>
                    )}
                    <Popconfirm
                        title="删除这份文档？"
                        description="原件与已入库的分片会一起删除，之后检索不再命中它。"
                        okText="删除"
                        cancelText="取消"
                        onConfirm={() => remove.mutate(row.id)}
                    >
                        <Button size="small" danger loading={remove.isPending && remove.variables === row.id}>
                            删除
                        </Button>
                    </Popconfirm>
                </Space>
            ),
        },
    ]

    return (
        <>
            <div className="kb-hero">
                <span className="proto-tag live">已接入</span>
                <h3>RAG 知识库</h3>
                <p>上传协议文档、操作指南与笔记；后台解析成片段并向量化，供对话检索时引用原文。</p>
            </div>

            {me.data && !isAdmin && (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 16 }}
                    title="当前地址没有知识库管理权限"
                    description="上传与删除只对白名单地址开放；能读到列表，但改不了内容。"
                />
            )}
            {failure && (
                <Alert
                    type="error"
                    showIcon
                    style={{ marginBottom: 16 }}
                    title="操作未完成"
                    description={failure instanceof Error ? failure.message : String(failure)}
                />
            )}

            <div className="filters" style={{ marginBottom: 14 }}>
                <Upload
                    accept=".pdf,.md,.markdown,.txt,.docx"
                    showUploadList={false}
                    beforeUpload={(file) => {
                        upload.reset()
                        upload.mutate(file)
                        // 交给 useMutation 发请求，Upload 自己不要另外发一次。
                        return false
                    }}
                >
                    <Button loading={upload.isPending} disabled={!isAdmin}>
                        上传文档
                    </Button>
                </Upload>
                <span className="chip">PDF / Markdown / TXT / DOCX</span>
                <span className="chip">
                    {pending > 0 ? `${pending} 份处理中` : `共 ${rows.length} 份`}
                </span>
            </div>

            <Panel title="文档列表" sub={documents.isFetching ? '读取中' : `${rows.length} 份`}>
                <Table
                    rowKey="id"
                    size="small"
                    columns={columns}
                    dataSource={rows}
                    pagination={false}
                    loading={documents.isFetching && !documents.data}
                    locale={{ emptyText: '还没有文档，先上传一份' }}
                    scroll={{ x: 'max-content' }}
                />
                <Note title="处理是后台异步的">
                    上传只完成登记与落盘；解析、切分、向量化在基础服务的后台队列里跑，状态从「排队中」走到「可检索」。
                    只有状态到了「可检索」，这篇文档才真正会被检索命中。
                    失败的会写明停在哪一步，可以直接重试，不必重新上传。
                </Note>
            </Panel>
        </>
    )
}
