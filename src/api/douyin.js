import axios from 'axios'
import { ElMessage } from 'element-plus'

// 创建 axios 实例
const api = axios.create({
  baseURL: '/api',  // 通过 vite 代理到后端
  // /Api/Init 首次启动 Chrome + 加载抖音页需要 15-60 秒,10 秒超时会导致前端误报"无法连接"
  timeout: 120000
})

// 请求拦截器
api.interceptors.request.use(
  config => {
    const token = localStorage.getItem('token') || localStorage.getItem('douyin_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  error => Promise.reject(error)
)

// 响应拦截器
api.interceptors.response.use(
  response => {
    const contentType = response.headers['content-type']
    if (contentType && contentType.includes('application/json')) {
      const res = response.data
      if (res && res.code !== undefined && res.code == 401) {
        ElMessage.error('登录已过期，请重新登录')
        localStorage.removeItem('token')
        localStorage.removeItem('douyin_token')
        setTimeout(() => {
          window.location.replace('/')
        }, 1500)
        return Promise.reject(new Error(res.data || '未授权'))
      }
      if (res && res.code === 403 && typeof res.data === 'string' && res.data.startsWith('license_')) {
        ElMessage.error('授权已失效，请在设置页重新激活')
        return Promise.reject(new Error(res.data))
      }
      if (res && res.code !== undefined && res.code != 200 && res.code != '200') {
        ElMessage.error(res.data || res.msg || res.message || '请求失败')
        return Promise.reject(new Error(res.data || '请求失败'))
      }
      return res
    }
    return response
  },
  error => {
    // 处理网络错误或服务器错误
    if (error.response) {
      // 服务器返回了错误状态码
      const status = error.response.status
      const data = error.response.data

      if (status === 401) {
        ElMessage.error('登录已过期，请重新登录')
        localStorage.removeItem('token')
        localStorage.removeItem('douyin_token')
        setTimeout(() => {
          window.location.replace('/login')
        }, 1500)
        return
      } else if (status === 500) {
        ElMessage.error('服务器内部错误')
      } else if (status === 404) {
        ElMessage.error('请求的资源不存在')
      } else {
        // 尝试从响应中提取错误信息
        const msg = data?.data || data?.msg || data?.message || error.message || `请求失败 (${status})`
        ElMessage.error(msg)
      }
    } else if (error.request) {
      // 请求已发送但没有收到响应
      ElMessage.error('无法连接到服务器，请检查后端服务是否启动')
    } else {
      ElMessage.error(error.message || '网络错误')
    }
    return Promise.reject(error)
  }
)

// 初始化浏览器
export const initBrowser = () => api.get('/Api/Init')

// 获取初始化状态
export const getInitStatus = () => api.get('/Api/GetInit')

// 获取登录状态
export const getLoginStatus = () => api.get('/Api/GetLogin')

// 扫码登录确认
export const pnglogin = () => api.get('/Api/Pnglogin')

// 获取浏览器页面截图
export const getScrlk = () => api.get('/Api/GetScrlk')

// 获取二维码
export const getLoginPng = () => api.get('/Api/login/Init/GetLoginPng')

// 强制退出登录
export const dieLogin = () => api.get('/Api/DieLogin')

// 发送验证码
export const sendVerifyCode = (phone) => api.get('/Api/LoginPhone', { params: { phone } })

// 提交验证码
export const submitVerifyCode = (code) => api.get('/Api/LoginPhoneInput', { params: { code } })

// 退出登录
export const logout = () => api.get('/Api/logout')

// 获取好友列表
export const getFriendsList = () => api.get('/Api/GetFriendsList')

// 发送消息
export const sendMessage = (name, text) => api.get('/Api/Send', { params: { name, text } })

// 添加定时任务
export const addTask = (time, name, text) => api.get('/Time/add', { params: { time, name, text } })

// 删除定时任务
export const delTask = (task_id) => api.get('/Time/del', { params: { task_id } })

// 修改定时任务
export const editTask = (name, new_time) => api.get('/Time/edit', { params: { name, new_time } })

// 获取任务列表
export const getTaskList = () => api.get('/Time/getlist')

// 获取用户名
export const getUsername = () => api.get('/Api/GetUsername')

// 抖音账号唯一标识(设置页授权信息卡展示)
export const getDouyinUserInfo = () => api.get('/Api/GetDouyinUserInfo')

// 修改密码
export const changePassword = (old_password, new_password) => api.get('/Api/ChangePassword', { params: { old_password, new_password } })

// 获取上次登录IP
export const getLastLoginIP = () => api.get('/Api/GetLastLoginIP')

// 获取项目启动时间
export const getHome = () => api.get('/Home')

// ===== 授权与系统 =====

// 获取本机机器码(激活页展示)
export const getMachineCode = () => api.get('/Api/GetMachineCode')

// 激活卡密
export const activateLicense = (card) => api.post('/Api/Activate', { card })

// 授权状态(激活页/锁屏页轮询)
export const getLicenseStatus = () => api.get('/Api/LicenseStatus')

// 本机免密引导:签发本地 token
export const localBootstrap = () => api.get('/Api/LocalBootstrap')

// 聚合状态(登录/心跳/暂停/授权)
export const getStatus = () => api.get('/Api/Status')

// 退出程序(桌面版)
export const shutdownApp = () => api.post('/Api/Shutdown')

// 开机自启开关
export const setAutoStart = (enable) => api.get('/Api/SetAutoStart', { params: { enable: enable ? '1' : '0' } })

export default api
