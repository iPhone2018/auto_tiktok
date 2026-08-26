import { ref } from 'vue'

export const browserStatus = ref(false)
export const loginStatus = ref(false)
// 三态登录态(in=已登录 / out=未登录 / expired=会话已过期),顶栏右上角显示用
export const loginState = ref('out')
export const accountNickname = ref('')
export const friendsList = ref([])
export const hasLoaded = ref(false)
export const homeLoaded = ref(false)

export const setBrowserStatus = (status) => {
  browserStatus.value = status
}

export const setLoginStatus = (status) => {
  loginStatus.value = status
}

export const setLoginState = (state) => {
  loginState.value = state
}

export const setAccountNickname = (nickname) => {
  accountNickname.value = nickname
}

export const setFriendsList = (list) => {
  friendsList.value = list
  hasLoaded.value = true
}

export const setHomeLoaded = () => {
  homeLoaded.value = true
}
