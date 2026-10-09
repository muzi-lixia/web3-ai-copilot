import { createBrowserRouter, Navigate } from 'react-router-dom'

import AppLayout from '../components/AppLayout'
import RequireAuth from '../components/RequireAuth'
import Copilot from '../views/Copilot'
import Dashboard from '../views/Dashboard'
import Login from '../views/Login'
import Market from '../views/Market'
import Portfolio from '../views/Portfolio'
import Risk from '../views/Risk'
import Staking from '../views/Staking'

export const router = createBrowserRouter([
  { path: '/login', element: <Login /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: <Dashboard /> },
      { path: 'portfolio', element: <Portfolio /> },
      { path: 'market', element: <Market /> },
      { path: 'risk', element: <Risk /> },
      { path: 'staking', element: <Staking /> },
      { path: 'copilot', element: <Copilot /> },
    ],
  },
])
