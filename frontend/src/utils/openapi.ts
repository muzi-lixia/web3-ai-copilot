/** 仅解析文档声明，不调用目录中的业务接口；注册信息与健康状态严格区分。 */
export interface CatalogEntry {
    id: string
    path: string
    method: string
    group: string
    description: string
    auth: string
    deprecated: boolean
}

type ObjectValue = Record<string, unknown>

const object = (v: unknown): v is ObjectValue => typeof v === 'object' && v !== null && !Array.isArray(v)

const METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace'])

/** 一段 operation 的鉴权描述：优先用扩展字段，其次读 security 声明。 */
function authentication(operation: ObjectValue, document: ObjectValue): string {
    if (typeof operation['x-auth-description'] === 'string') return operation['x-auth-description']
    const security = operation.security ?? document.security
    if (security === undefined || (Array.isArray(security) && security.length === 0)) return '公开访问'
    if (!Array.isArray(security)) return '鉴权声明无法识别'

    const components = object(document.components) ? document.components : {}
    const schemes = object(components.securitySchemes) ? components.securitySchemes : {}
    // security 数组项是 OR，单项多个 scheme 是 AND；空对象意味着支持匿名访问。
    return security
        .map((requirement) => {
            if (!object(requirement)) return '未知鉴权'
            const names = Object.keys(requirement)
            if (!names.length) return '匿名访问'
            return names
                .map((name) => {
                    const scheme = schemes[name]
                    if (!object(scheme)) return name
                    if (scheme.type === 'http' && String(scheme.scheme).toLowerCase() === 'bearer') {
                        return 'Bearer 凭证'
                    }
                    if (scheme.type === 'apiKey') return `API Key（${String(scheme.name ?? name)}）`
                    return name
                })
                .join(' + ')
        })
        .join(' 或 ')
}

/** 把 OpenAPI 3 文档压成一份接口清单；不是 3.x 或结构缺失时直接报错。 */
export function parseCatalog(value: unknown): { title: string; entries: CatalogEntry[] } {
    const document = object(value) ? value : null
    if (!document || typeof document.openapi !== 'string' || !document.openapi.startsWith('3.')) {
        throw new Error('服务未返回有效的 OpenAPI 3 文档')
    }
    const paths = document.paths
    if (!object(paths)) throw new Error('服务未返回有效的 OpenAPI 3 文档')

    const entries: CatalogEntry[] = []
    for (const [path, pathItem] of Object.entries(paths)) {
        if (!object(pathItem)) continue
        for (const [method, operation] of Object.entries(pathItem)) {
            if (!METHODS.has(method) || !object(operation)) continue
            const tags = Array.isArray(operation.tags)
                ? operation.tags.filter((v): v is string => typeof v === 'string')
                : []
            entries.push({
                id: `${method}:${path}`,
                path,
                method: method.toUpperCase(),
                group: tags[0] ?? '其他接口',
                description: describe(operation),
                auth: authentication(operation, document),
                deprecated: operation.deprecated === true,
            })
        }
    }
    entries.sort(
        (a, b) =>
            a.group.localeCompare(b.group, 'zh-CN') ||
            a.path.localeCompare(b.path) ||
            a.method.localeCompare(b.method),
    )
    const title = object(document.info) && typeof document.info.title === 'string'
        ? document.info.title
        : '服务接口'
    return { title, entries }
}

/** summary 优先，其次取 description 首行；都没有则给一句占位。 */
function describe(operation: ObjectValue): string {
    if (typeof operation.summary === 'string') return operation.summary
    if (typeof operation.description === 'string') return operation.description.split('\n')[0]
    return '服务未提供说明'
}
