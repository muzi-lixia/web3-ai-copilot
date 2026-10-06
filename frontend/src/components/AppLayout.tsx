import { Layout, Menu } from 'antd'
import { Link, Outlet, useLocation } from 'react-router-dom'

const { Content, Header, Sider } = Layout

/** 侧边导航。key 用 pathname，选中态直接由当前路由推导，不用额外 state。 */
const NAV = [
  { key: '/dashboard', label: <Link to="/dashboard">Dashboard</Link> },
  { key: '/portfolio', label: <Link to="/portfolio">Portfolio</Link> },
  { key: '/staking', label: <Link to="/staking">Staking</Link> },
  { key: '/copilot', label: <Link to="/copilot">Copilot</Link> },
  { key: '/knowledge', label: <Link to="/knowledge">Knowledge</Link> },
]

export default function AppLayout() {
  const { pathname } = useLocation()

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={208} style={{ borderRight: '1px solid #f0f0f0' }}>
        <div style={{ padding: '18px 16px', fontWeight: 600, fontSize: 15 }}>Web3 AI Copilot</div>
        <Menu mode="inline" selectedKeys={[pathname]} items={NAV} style={{ borderInlineEnd: 0 }} />
      </Sider>

      <Layout>
        <Header
          style={{
            background: '#fff',
            paddingInline: 24,
            height: 56,
            lineHeight: '56px',
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          {/* 钱包地址输入位（WalletInput，Phase B 接入 walletStore） */}
          <span style={{ color: '#8c8c8c', fontSize: 14 }}>未设置钱包地址</span>
        </Header>

        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
