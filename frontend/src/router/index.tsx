import { createBrowserRouter, Navigate } from 'react-router-dom'

import AppLayout from '../components/AppLayout'
import RequireAuth from '../components/RequireAuth'
import Copilot from '../views/Copilot'
import Login from '../views/Login'
import Market from '../views/Market'
import Assets from '../views/Assets'
import Services from '../views/Services'
import Settings from '../views/Settings'
import Knowledge from '../views/Knowledge'

export const router = createBrowserRouter([
    {
        path: '/',
        element: <AppLayout />,
        children: [
            { path: 'login', element: <Login /> },
            { path: 'services', element: <Services /> },
            { path: 'settings', element: <Settings /> },
            { path: 'knowledge', element: <RequireAuth><Knowledge /></RequireAuth> },
            { index: true, element: <Navigate to="/copilot" replace /> },
            { path: 'portfolio', element: <RequireAuth><Assets /></RequireAuth> },
            { path: 'market', element: <Market /> },
            { path: 'copilot', element: <RequireAuth><Copilot /></RequireAuth> },
        ],
    },
])
