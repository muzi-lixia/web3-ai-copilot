/** 模型正文的 Markdown 渲染：只读展示，不渲染原始 HTML（react-markdown 默认即转义），
 * 链接一律新窗口打开。
 *
 * 正文里不挂光标：生成中的提示由外层的「三点流动」负责，它渲染在正文块之外，
 * 不会跟半截 Markdown 语法混在一起。
 */
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export default function Markdown({ text }: { text: string }) {
    return (
        <div className="md">
            <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                    // 模型给的链接不接管当前页；react-markdown 会把 node 塞进 props，这里剔除。
                    a: ({ node, ...rest }) => <a {...rest} target="_blank" rel="noreferrer noopener" />,
                }}
            >
                {text}
            </ReactMarkdown>
        </div>
    )
}
