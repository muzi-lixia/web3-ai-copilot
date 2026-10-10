import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { RouterProvider } from 'react-router-dom'

import { router } from './router'

export default function App() {
  return (
    <ConfigProvider locale={zhCN} theme={{ algorithm: theme.darkAlgorithm, token: {
      colorPrimary: '#6E7BFF', colorBgBase: '#0A0D12', colorBgContainer: '#11151C',
      colorText: '#E8EEF5', colorTextSecondary: '#98A7B8', colorBorder: '#2E3846', borderRadius: 10,
    } }}>
      <RouterProvider router={router} />
    </ConfigProvider>
  )
}
