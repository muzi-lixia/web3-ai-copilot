import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { RouterProvider } from 'react-router-dom'

import { router } from './router'

const appTheme = {
    algorithm: theme.darkAlgorithm,
    token: {
        colorPrimary: '#3FD68C',
        colorBgBase: '#0A0D12',
        colorBgContainer: '#11151C',
        colorText: '#E8EEF5',
        colorTextSecondary: '#98A7B8',
        colorBorder: '#2E3846',
        borderRadius: 10,
    },
}

export default function App() {
    return (
        <ConfigProvider locale={zhCN} theme={appTheme}>
            <RouterProvider router={router} />
        </ConfigProvider>
    )
}
