import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getLicenseStatus } from '../api/douyin'

// 授权状态: none=未激活 active=已激活 expired=已过期 rollback=时钟回拨 locked=已锁定
// 后端 dev 模式(status='dev',未配置公钥)按 active 处理,方便开发调试
export const useLicenseStore = defineStore('license', () => {
  const status = ref('none')
  const machineCode = ref('')
  const activatedAt = ref('')
  const expiresAt = ref('')
  const daysLeft = ref(0)
  const expiresInSeconds = ref(null)
  const loaded = ref(false)
  const loading = ref(false)

  const isActive = computed(() => status.value === 'active')

  async function fetchStatus() {
    loading.value = true
    try {
      const res = await getLicenseStatus()
      const d = res.data || {}
      status.value = d.status || 'none'
      machineCode.value = d.machine_code || ''
      activatedAt.value = d.activated_at || ''
      expiresAt.value = d.expires_at || ''
      daysLeft.value = d.days_left ?? 0
      expiresInSeconds.value = d.expires_in_seconds ?? null
    } catch (e) {
      // 后端不可达:保持原状态,由页面提示
    } finally {
      loaded.value = true
      loading.value = false
    }
  }

  function reset() {
    status.value = 'none'
    expiresAt.value = ''
    daysLeft.value = 0
  }

  return { status, machineCode, activatedAt, expiresAt, daysLeft, expiresInSeconds, loaded, loading, isActive, fetchStatus, reset }
})
