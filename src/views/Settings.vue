<template>
  <div class="settings-container">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>账户配置</span>
        </div>
      </template>

      <div class="account-section">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="登录状态">
            <el-tag :type="displayLogin ? 'success' : (loginState === 'expired' ? 'warning' : 'danger')">
              {{ displayLogin ? (username ? '已登录: ' + username : '已登录') : (loginState === 'expired' ? '会话已过期,请重新登录' : '未登录') }}
            </el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <div class="login-action">
          <el-button type="primary" :icon="Key" @click="handleLogin" :loading="loginLoading" :disabled="loginStatus">
            扫码登录
          </el-button>
          <el-button type="primary" :icon="Message" @click="phoneDialogVisible = true" :disabled="loginStatus">
            验证码登录
          </el-button>
          <el-button :icon="Refresh" @click="handleRefreshStatus" :loading="refreshStatusLoading">
            刷新状态
          </el-button>
          <el-button :icon="SwitchButton" type="danger" @click="handleDieLogin" :disabled="!displayLogin">
            强制退出登录
          </el-button>
        </div>
      </div>
    </el-card>

    <!-- 授权信息 -->
    <el-card style="margin-top: 20px">
      <template #header>
        <div class="card-header">
          <span>授权信息</span>
        </div>
      </template>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="授权状态">
          <el-tag :type="license.status === 'active' && douyinId ? 'success' : 'danger'">
            {{ statusText }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="抖音标识">
          <div class="machine-row">
            <span class="machine-code">{{ douyinId || '未登录(请先登录抖音)' }}</span>
            <el-button v-if="douyinId" size="small" @click="copyDouyinId">复制</el-button>
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="本机机器码">
          <div class="machine-row">
            <span class="machine-code">{{ license.machineCode || '加载中...' }}</span>
            <el-button size="small" @click="copyMachineCode">复制</el-button>
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="到期时间">{{ formatExpire(license.expiresAt) }}</el-descriptions-item>
        <el-descriptions-item label="剩余时长">{{ formatRemaining() }}</el-descriptions-item>
      </el-descriptions>
      <div class="login-action" style="margin-top: 14px">
        <el-button type="primary" @click="renewDialogVisible = true">续费激活</el-button>
      </div>
    </el-card>

    <!-- 续费激活弹窗 -->
    <el-dialog v-model="renewDialogVisible" title="续费激活" width="500px" destroy-on-close>
      <el-form label-width="80px">
        <el-form-item label="抖音标识">
          <div class="machine-row">
            <span class="machine-code">{{ douyinId || '未登录(请先登录抖音)' }}</span>
            <el-button v-if="douyinId" size="small" @click="copyDouyinId">复制</el-button>
          </div>
        </el-form-item>
        <el-form-item label="机器码">
          <div class="machine-row">
            <span class="machine-code">{{ license.machineCode }}</span>
            <el-button size="small" @click="copyMachineCode">复制</el-button>
          </div>
        </el-form-item>
        <el-form-item label="卡密">
          <el-input v-model="renewCard" type="textarea" :rows="4" placeholder="粘贴新卡密(形如 DY-XXXX-...)"/>
        </el-form-item>
      </el-form>
      <p v-if="douyinId" style="margin: 0 0 6px; font-size: 12px; color: #909399;">
        卡密与当前抖音账号({{ douyinId }})绑定,激活后更换抖音账号将导致授权失效
      </p>
      <template #footer>
        <el-button @click="renewDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleRenewActivate" :loading="renewLoading">激活</el-button>
      </template>
    </el-dialog>

    <!-- 验证码登录弹窗 -->
    <el-dialog v-model="phoneDialogVisible" title="验证码登录" width="400px" destroy-on-close>
      <el-form :model="phoneForm" label-width="80px">
        <el-form-item label="手机号">
          <el-input
            v-model="phoneForm.phone"
            placeholder="请输入手机号"
            @keyup.enter="handleSendCode"
          />
        </el-form-item>
        <el-form-item label="验证码">
          <div style="display: flex; gap: 10px;">
            <el-input
              v-model="phoneForm.code"
              placeholder="请输入验证码"
              style="flex: 1"
              @keyup.enter="handlePhoneLogin"
            />
            <el-button @click="handleSendCode" :disabled="codeCountdown > 0" :loading="codeLoading">
              {{ codeCountdown > 0 ? `${codeCountdown}s` : '发送验证码' }}
            </el-button>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="phoneDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handlePhoneLogin" :loading="phoneLoading">
          登录
        </el-button>
      </template>
    </el-dialog>

    <!-- 二维码弹窗 -->
    <el-dialog v-model="qrDialogVisible" title="抖音扫码登录" width="350px" destroy-on-close>
      <div class="qrcode-container">
        <div v-if="qrcodeUrl" class="qrcode-wrapper">
          <img :src="qrcodeUrl" alt="登录二维码" class="qrcode-img" />
          <p class="qrcode-hint">请使用抖音App扫码登录</p>
        </div>
        <div v-else-if="loading" class="loading-wrapper">
          <el-icon class="is-loading"><Loading /></el-icon>
          <p>正在加载二维码...</p>
        </div>
        <div v-else class="error-wrapper">
          <p>获取二维码失败，请重试</p>
        </div>
      </div>
      <div class="qrcode-actions">
        <el-button :icon="Refresh" @click="handleRefreshCode" :loading="refreshLoading" size="small">
          刷新验证码
        </el-button>
        <el-button :icon="View" @click="handleCheckLogin" :loading="checkLoading" size="small">
          获取登录状态
        </el-button>
      </div>
    </el-dialog>
  </div>

  <!-- 调试区域卡片 -->
  <el-card style="margin-top: 20px">
    <template #header>
      <div class="card-header">
        <span>调试功能</span>
      </div>
    </template>
    <div class="debug-row">
      <el-button type="primary" :icon="Picture" @click="handleGetScreenshot" :loading="screenshotLoading">
        获取浏览器页面截图
      </el-button>
    </div>
    <div v-if="screenshotUrl" class="screenshot-wrapper">
      <img :src="screenshotUrl" alt="浏览器截图" class="screenshot-img" />
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted, onActivated, watch, onUnmounted } from 'vue'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Key, Refresh, View, Loading, SwitchButton, Picture, Message } from '@element-plus/icons-vue'
import { getInitStatus, getLoginStatus, initBrowser, getLoginPng, getUsername, getDouyinUserInfo, getFriendsList, logout, pnglogin, getScrlk, dieLogin, sendVerifyCode, submitVerifyCode, activateLicense } from '../api/douyin'
import { loginStatus, hasLoaded, setLoginStatus, setFriendsList } from '../stores/browser'
import { useLicenseStore } from '../stores/license'

const loginLoading = ref(false)
const refreshLoading = ref(false)
const checkLoading = ref(false)
const refreshStatusLoading = ref(false)
const qrDialogVisible = ref(false)
const qrcodeUrl = ref('')
const loading = ref(false)
const username = ref(localStorage.getItem('douyin_username') || '')
const usernameLoaded = ref(localStorage.getItem('douyin_username_loaded') === '1')
const settingsLoaded = ref(localStorage.getItem('douyin_settings_loaded') === '1')
// 授权信息
const license = useLicenseStore()
const renewDialogVisible = ref(false)
const renewLoading = ref(false)
const renewCard = ref('')
// 浏览器初始化状态(未初始化时登录状态一律显示未登录)
const browserStatus = ref(false)
const fetchBrowserStatus = async () => {
  try {
    const res = await getInitStatus()
    browserStatus.value = res.data === 'Yes'
  } catch (e) {
    browserStatus.value = false
  }
}
const displayLogin = computed(() => browserStatus.value && loginStatus.value)
// 抖音账号唯一标识(登录后从页面 SSR 数据提取)
const douyinId = ref('')
const douyinNickname = ref('')

const fetchDouyinInfo = async () => {
  try {
    const res = await getDouyinUserInfo()
    if (res.code === 200) {
      douyinId.value = res.data.douyin_id || ''
      douyinNickname.value = res.data.nickname || ''
    }
  } catch (e) {
    douyinId.value = ''
  }
}

const statusText = computed(() => {
  if (license.status === 'active') return douyinId.value ? '已激活' : '未激活(未登录抖音)'
  if (license.status === 'expired') return '已过期'
  if (license.status === 'rollback') return '时间异常'
  if (license.status === 'locked') return '已锁定'
  if (license.status === 'account_mismatch') return '账号不匹配'
  if (license.status === 'config_missing') return '未配置授权'
  return '未激活'
})

const copyDouyinId = async () => {
  try {
    await navigator.clipboard.writeText(douyinId.value)
    ElMessage.success('抖音标识已复制')
  } catch (e) {
    ElMessage.error('复制失败,请手动选中复制')
  }
}
const screenshotLoading = ref(false)
const screenshotUrl = ref('')
const phoneDialogVisible = ref(false)
const phoneLoading = ref(false)
const codeLoading = ref(false)
const codeCountdown = ref(0)
const phoneForm = ref({
  phone: '',
  code: ''
})

const copyMachineCode = async () => {
  try {
    await navigator.clipboard.writeText(license.machineCode || '')
    ElMessage.success('机器码已复制')
  } catch (e) {
    ElMessage.error('复制失败,请手动选中复制')
  }
}

const formatExpire = (iso) => {
  if (!iso) return '-'
  const d = new Date(iso)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

const formatRemaining = () => license.remainingText

const handleRenewActivate = async () => {
  if (!renewCard.value.trim()) {
    ElMessage.warning('请粘贴卡密')
    return
  }
  renewLoading.value = true
  try {
    const res = await activateLicense(renewCard.value.trim())
    if (res.code === 200) {
      ElMessage.success('激活成功')
      renewDialogVisible.value = false
      renewCard.value = ''
      await license.fetchStatus()
    }
  } catch (error) {
    if (error && error.response) {
      ElMessage.error(error.response.data?.data || '激活失败')
    }
  } finally {
    renewLoading.value = false
  }
}


const fetchUsername = async () => {
  try {
    const res = await getUsername()
    if (res.code == 200 || res.code == '200') {
      username.value = res.data
      usernameLoaded.value = true
      localStorage.setItem('douyin_username', res.data)
      localStorage.setItem('douyin_username_loaded', '1')
    }
  } catch (error) {
    // 获取失败不提示，静默处理
  }
}

const loginState = ref('out')
const checkLoginStatus = async () => {
  try {
    const res = await getLoginStatus()
    loginStatus.value = res.data === 'Yes'
    setLoginStatus(loginStatus.value)
    loginState.value = res.login_state || (loginStatus.value ? 'in' : 'out')
    if (loginStatus.value && !usernameLoaded.value) {
      await fetchUsername()
    }
  } catch (error) {
    loginStatus.value = false
    setLoginStatus(false)
    loginState.value = 'out'
  }
}

const handleRefreshStatus = async () => {
  refreshStatusLoading.value = true
  try {
    usernameLoaded.value = false
    localStorage.removeItem('douyin_username')
    localStorage.removeItem('douyin_username_loaded')
    await fetchBrowserStatus()
    await checkLoginStatus()
    ElMessage.success(displayLogin.value ? '已登录' : '未登录')
  } finally {
    refreshStatusLoading.value = false
  }
}

const handleCheckLogin = async () => {
  checkLoading.value = true
  try {
    const res = await pnglogin()
    loginStatus.value = res.code == 200
    setLoginStatus(loginStatus.value)
    if (loginStatus.value) {
      ElMessage.success('登录成功，扫码登录窗口将关闭')
      qrDialogVisible.value = false
      username.value = ''
      usernameLoaded.value = false
      localStorage.removeItem('douyin_username')
      localStorage.removeItem('douyin_username_loaded')
      await fetchUsername()
      // 登录成功后请求好友列表
      await fetchFriendsList()
    } else {
      ElMessage.warning('未登录，请继续扫码')
    }
  } catch (error) {
    ElMessage.error('扫码登录失败，请重试')
  } finally {
    checkLoading.value = false
  }
}

const fetchFriendsList = async () => {
  try {
    const res = await getFriendsList()
    if (res.code === 200) {
      const list = res.data.list || {}
      const formattedList = Object.entries(list).map(([name, [avatar, fire]]) => ({
        name,
        avatar,
        fire
      }))
      setFriendsList(formattedList)
    }
  } catch (error) {
    // 获取失败静默处理
  }
}

// 扫码登录自动轮询：每5秒检测一次登录状态，登录成功后自动关闭弹窗并刷新数据
let qrPollTimer = null
const stopQrPoll = () => {
  if (qrPollTimer) {
    clearInterval(qrPollTimer)
    qrPollTimer = null
  }
}
const startQrPoll = () => {
  stopQrPoll()
  qrPollTimer = setInterval(async () => {
    if (!qrDialogVisible.value) {
      stopQrPoll()
      return
    }
    try {
      // 用原生 axios 绕过拦截器，避免轮询时反复弹出错误提示
      const res = await axios.get('/api/Api/Pnglogin', {
        headers: { Authorization: `Bearer ${localStorage.getItem('token') || localStorage.getItem('douyin_token')}` }
      })
      if (res.data && (res.data.code == 200 || res.data.code == '200')) {
        stopQrPoll()
        loginStatus.value = true
        loginState.value = 'in'
        setLoginStatus(true)
        qrDialogVisible.value = false
        ElMessage.success('登录成功，扫码登录窗口将关闭')
        username.value = ''
        usernameLoaded.value = false
        localStorage.removeItem('douyin_username')
        localStorage.removeItem('douyin_username_loaded')
        await fetchUsername()
        await fetchFriendsList()
      }
    } catch (e) {
      // 未登录/网络波动时继续轮询
    }
  }, 5000)
}

const handleRefreshCode = async () => {
  refreshLoading.value = true
  try {
    // 先初始化浏览器
    await initBrowser()
    await fetchBrowserStatus()
    // 获取新二维码
    const res = await getLoginPng()
    if (res.data === 'already_logged_in') {
      qrDialogVisible.value = false
      setLoginStatus(true)
      loginState.value = 'in'
      await fetchUsername()
      await fetchFriendsList()
      ElMessage.success('检测到抖音已登录,无需重复扫码')
    } else if (res.data) {
      qrcodeUrl.value = res.data
      qrDialogVisible.value = true
      startQrPoll()
      ElMessage.success('刷新成功')
    } else {
      ElMessage.error('获取二维码失败')
    }
  } catch (error) {
    ElMessage.error('刷新失败，请确保浏览器已初始化')
  } finally {
    refreshLoading.value = false
  }
}

const handleSendCode = async () => {
  if (!phoneForm.value.phone) {
    ElMessage.warning('请输入手机号')
    return
  }
  codeLoading.value = true
  try {
    const res = await sendVerifyCode(phoneForm.value.phone)
    if (res.code == 200) {
      ElMessage.success('验证码发送成功')
      codeCountdown.value = 60
      const timer = setInterval(() => {
        codeCountdown.value--
        if (codeCountdown.value <= 0) {
          clearInterval(timer)
        }
      }, 1000)
    } else {
      ElMessage.error(res.data || '验证码发送失败')
    }
  } catch (error) {
    ElMessage.error('验证码发送失败，请确保浏览器已初始化')
  } finally {
    codeLoading.value = false
  }
}

const handlePhoneLogin = async () => {
  if (!phoneForm.value.phone) {
    ElMessage.warning('请输入手机号')
    return
  }
  if (!phoneForm.value.code) {
    ElMessage.warning('请输入验证码')
    return
  }
  phoneLoading.value = true
  try {
    const res = await submitVerifyCode(phoneForm.value.code)
    if (res.code == 200) {
      ElMessage.success('登录成功')
      phoneDialogVisible.value = false
      setLoginStatus(true)
      loginState.value = 'in'
      username.value = ''
      usernameLoaded.value = false
      localStorage.removeItem('douyin_username')
      localStorage.removeItem('douyin_username_loaded')
      await fetchUsername()
    } else {
      ElMessage.error(res.data || '登录失败')
    }
  } catch (error) {
    ElMessage.error('登录失败，请重试')
  } finally {
    phoneLoading.value = false
  }
}

const handleGetScreenshot = async () => {
  screenshotLoading.value = true
  screenshotUrl.value = ''
  try {
    const res = await getScrlk()
    if (res.code == 200) {
      screenshotUrl.value = 'data:image/png;base64,' + res.data
    } else {
      ElMessage.error(res.data || '获取截图失败')
    }
  } catch (error) {
    ElMessage.error('获取截图失败，请确保已登录')
  } finally {
    screenshotLoading.value = false
  }
}

const handleDieLogin = async () => {
  try {
    await dieLogin()
    setLoginStatus(false)
    loginState.value = 'out'
    localStorage.removeItem('douyin_token')
    localStorage.removeItem('douyin_username')
    localStorage.removeItem('douyin_username_loaded')
    ElMessage.success('已彻底退出登录,页面即将刷新')
    // 刷新整页:登录态/好友/授权信息全部重拉,保证可以正常重新扫码换号
    setTimeout(() => { window.location.reload() }, 800)
  } catch (error) {
    ElMessage.error('强制退出失败')
  }
}

const handleLogin = async () => {
  loginLoading.value = true
  loading.value = true
  qrcodeUrl.value = ''
  qrDialogVisible.value = true

  try {
    // 先初始化浏览器
    await initBrowser()
    await fetchBrowserStatus()
    // 获取二维码
    const res = await getLoginPng()
    if (res.data === 'already_logged_in') {
      // 后端已登录:直接提示,采集已有 cookie,无需扫码
      qrDialogVisible.value = false
      setLoginStatus(true)
      loginState.value = 'in'
      await fetchUsername()
      await fetchFriendsList()
      ElMessage.success('检测到抖音已登录,无需重复扫码')
    } else if (res.data) {
      qrcodeUrl.value = res.data
      startQrPoll()
      ElMessage.success('请使用抖音App扫码登录')
    } else {
      ElMessage.error('获取二维码失败')
      qrDialogVisible.value = false
    }
  } catch (error) {
    ElMessage.error('登录初始化失败，请确保浏览器已启动')
    qrDialogVisible.value = false
  } finally {
    loginLoading.value = false
    loading.value = false
  }
}

onMounted(async () => {
  // 首次加载
  await fetchBrowserStatus()
  if (!settingsLoaded.value) {
    await checkLoginStatus()
    localStorage.setItem('douyin_settings_loaded', '1')
  }
  await license.fetchStatus()
  await fetchDouyinInfo()
})

onActivated(() => {
  fetchBrowserStatus()
  license.fetchStatus()
  fetchDouyinInfo()
})

watch(loginStatus, (v) => {
  if (v) fetchDouyinInfo()
})

watch(qrDialogVisible, (v) => {
  // 二维码弹窗关闭时停止轮询
  if (!v) stopQrPoll()
})

onUnmounted(() => {
  stopQrPoll()
})
</script>

<style scoped>
.settings-container {
  width: 100%;
}

.machine-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.machine-code {
  font-family: monospace;
  letter-spacing: 1px;
  word-break: break-all;
}

.card-header {
  font-weight: 600;
  font-size: 16px;
}

.account-section {
  padding: 10px 0;
}

.login-action {
  margin-top: 30px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.qrcode-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 300px;
}

.qrcode-wrapper {
  text-align: center;
}

.qrcode-img {
  width: 250px;
  height: 250px;
  border: 1px solid #ddd;
  border-radius: 8px;
}

.qrcode-hint {
  margin-top: 15px;
  color: #666;
  font-size: 14px;
}

.qrcode-actions {
  display: flex;
  justify-content: center;
  gap: 12px;
  margin-top: 20px;
  padding-top: 15px;
  border-top: 1px solid #eee;
}

.loading-wrapper,
.error-wrapper {
  text-align: center;
  color: #999;
}

.loading-wrapper .el-icon {
  font-size: 48px;
  margin-bottom: 10px;
}

.config-row {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

.config-item {
  display: flex;
  align-items: center;
  padding: 8px 16px;
  background: #f5f7fa;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

.config-label {
  color: #909399;
  font-size: 14px;
}

.config-value {
  color: #303133;
  font-size: 14px;
  font-weight: 500;
}

.debug-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.screenshot-wrapper {
  margin-top: 16px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  overflow: hidden;
}

.screenshot-img {
  width: 100%;
  max-width: 800px;
  display: block;
}
</style>
