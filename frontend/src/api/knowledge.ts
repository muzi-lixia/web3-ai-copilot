import { http } from './client.ts'
import type { ApiResponse, KnowledgeDocument } from '../types/api'

/** 列出全部文档元数据，按上传时间倒序；解析进度靠反复调这个接口跟上。 */
export async function fetchDocuments(): Promise<KnowledgeDocument[]> {
  const { data } = await http.get<ApiResponse<KnowledgeDocument[]>>('/knowledge/documents')
  return data.data
}

/**
 * 上传一份原件，返回登记结果。
 *
 * 此时只完成了落盘与登记，正文尚未解析 —— 返回的 `status` 是 `pending`，
 * 不携带分片。体积上限由后端裁决，前端不复制一份判断。
 */
export async function uploadDocument(file: File): Promise<KnowledgeDocument> {
  const body = new FormData()
  body.append('file', file)
  const { data } = await http.post<ApiResponse<KnowledgeDocument>>('/knowledge/documents', body)
  return data.data
}

/** 把处理失败的文档重新排进后台队列；原件仍在，不必重新上传。 */
export async function retryDocument(documentId: string): Promise<KnowledgeDocument> {
  const { data } = await http.post<ApiResponse<KnowledgeDocument>>(
    `/knowledge/documents/${documentId}/retry`,
  )
  return data.data
}

/** 删除文档元数据与原件；已入库的分片由后端随外键一并删除。 */
export async function removeDocument(documentId: string): Promise<void> {
  await http.delete(`/knowledge/documents/${documentId}`)
}
