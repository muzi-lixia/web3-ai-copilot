import { Table, Button } from 'antd'
import { Panel, Note } from '../components/PageParts'
export default function Knowledge() {
  return <><div className="kb-hero"><span className="proto-tag soon">规划中 · 后续扩展</span><h3>RAG 知识库</h3><p>知识库管理与对话引用预留在这里，后续通过新增工具接入现有对话通道。</p></div>
    <div className="proto-grid three-cols" style={{ marginBottom: 16 }}>{[
      ['文档管理', '上传 / 归档 / 切分 / 版本。支持协议文档、操作指南与个人笔记。'],
      ['向量检索', '切片 → 向量化 → 检索 → 重排。检索结果携带出处。'],
      ['对话引用', '回答附带引用来源，用户可打开原文核对。'],
    ].map(([title, text]) => <div className="kb-card" key={title}><h3>{title}</h3><p>{text}</p><small>规划中 · 尚未启用</small></div>)}</div>
    <div className="proto-grid two-cols"><Panel title="知识库列表" sub="未接入"><Table size="small" pagination={false} dataSource={[]} columns={[{ title: '名称', dataIndex: 'name' }, { title: '文档数', dataIndex: 'count' }, { title: '状态', dataIndex: 'status' }]} locale={{ emptyText: '尚未创建知识库' }} /><div className="legend"><Button size="small" disabled>上传文档（未开放）</Button><span>当前不执行文档解析、训练或检索</span></div></Panel>
    <Panel title="对话中的引用会长这样" sub="静态样式预览，不是检索结果"><div className="panel-body"><div className="bub">回答将附带可核对的原文出处。</div><div className="cite"><strong>▤ 文档名称 · 章节</strong><p>检索命中片段将在这里展示；当前没有知识库数据。</p></div><Note title="接线方式">未来增加 search_knowledge 工具，复用现有工具调度与权限控制。</Note></div></Panel></div></>
}
