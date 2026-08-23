import { createRouter, createWebHistory } from 'vue-router'
import { localBootstrap } from '../api/douyin'
import { useLicenseStore } from '../stores/license'

// 路由配置:无登录页/激活页,直接进入首页
const routes = [
  {
    path: '/',
    component: () => import('../views/Layout.vue'),
    redirect: '/home',
    children: [
      {
        path: 'home',
        name: 'Home',
        component: () => import('../views/Home.vue'),
        meta: { title: '首页', icon: 'House' }
      },
      {
        path: 'friends',
        name: 'Friends',
        component: () => import('../views/Friends.vue'),
        meta: { title: '好友列表', icon: 'User' }
      },
      {
        path: 'tasks',
        name: 'Tasks',
        component: () => import('../views/Tasks.vue'),
        meta: { title: '定时任务', icon: 'Clock' }
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('../views/Settings.vue'),
        meta: { title: '设置', icon: 'Setting' }
      }
    ]
  },
  {
    path: '/login',
    redirect: '/home'  // 兼容旧链接
  },
  {
    path: '/activate',
    redirect: '/home'  // 兼容旧链接
  }
]

// 创建路由实例
const router = createRouter({
  history: createWebHistory(),
  routes
})

// 守卫:静默获取本机 token + 刷新授权状态(决定发送/建任务按钮可用性)
router.beforeEach(async () => {
  const licenseStore = useLicenseStore()
  await licenseStore.fetchStatus().catch(() => {})

  const hasToken = !!(localStorage.getItem('token') || localStorage.getItem('douyin_token'))
  if (hasToken) return true
  try {
    const res = await localBootstrap()
    if (res && res.code === 200) {
      localStorage.setItem('token', res.data)
    }
  } catch (e) {
    // 后端不可达也放行,页面自行提示
  }
  return true
})

export default router
