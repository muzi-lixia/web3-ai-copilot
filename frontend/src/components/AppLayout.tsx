import { Button, Layout, Menu, Space, Tag, Tooltip, Typography } from 'antd'
import { Link, Outlet, useLocation } from 'react-router-dom'

import { useWalletLogin } from '../hooks/useWalletLogin'
import { useAuthStore } from '../stores/auth'
import { shorten } from '../utils/format'

const { Content, Header, Sider } = Layout

/** 侧边导航。key 用 pathname，选中态直接由当前路由推导，不用额外 state。 */
const NAV = [
  { key: '/dashboard', label: <Link to="/dashboard">Dashboard</Link> },
  { key: '/portfolio', label: <Link to="/portfolio">Portfolio</Link> },
  { key: '/market', label: <Link to="/market">Market</Link> },
  { key: '/risk', label: <Link to="/risk">Risk</Link> },
  { key: '/staking', label: <Link to="/staking">Staking</Link> },
  { key: '/copilot', label: <Link to="/copilot">Copilot</Link> },
]

export default function AppLayout() {
  const { pathname } = useLocation()
  const address = useAuthStore((state) => state.address)
  const { logout } = useWalletLogin()

  return (
    // 外层锁死视口高度并把溢出裁掉 —— 整页不再产生滚动条，滚动全部交给下面的 <main>。
    // 注意是 `height` 而不是 `minHeight`：后者允许被内容撑高，撑高之后 body 就滚起来了。
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      <Sider
        theme="light"
        width={208}
        // 菜单超出时自己滚，不把外层撑高。
        style={{ borderRight: '1px solid #f0f0f0', overflow: 'auto' }}
      >
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
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <Tag color="green">Berachain</Tag>

          <Space size={12}>
            {address ? (
              <>
                <Tooltip title={address}>
                  <Typography.Text copyable={{ text: address }} style={{ fontSize: 14 }}>
                    {shorten(address)}
                  </Typography.Text>
                </Tooltip>
                <Button size="small" onClick={logout}>
                  退出
                </Button>
              </>
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: 14 }}>
                未登录
              </Typography.Text>
            )}
          </Space>
        </Header>

        {/*
          全站唯一的滚动容器。antd 的 Content 渲染出来就是 <main>。

          antd 的 .ant-layout-content 本身已经是 flex:auto + min-height:0，这里仍显式
          写出来：这两个值是"内滚"成立的前提（flex 子项的 min-height 初始值是 auto，
          不为 0 的话内容会把盒子撑高、溢出到 body 上，又退回整页滚动），
          不能依赖某个组件库版本恰好给对了。
        */}
        <Content style={{ padding: 24, flex: 1, minHeight: 0, overflow: 'auto' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
