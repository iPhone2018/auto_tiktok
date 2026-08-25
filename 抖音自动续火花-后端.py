# -*- coding: utf-8 -*-
"""
抖音自动续火花 - 后端服务
- 直接使用本机安装的 Google Chrome(有头模式,不再区分无头/容器)
- chromedriver 由 Selenium Manager 自动匹配本机 Chrome 版本(也可用环境变量 CHROMEDRIVER_BIN 指定)
- 浏览器配置持久化(chrome_profile 目录),服务重启后保持登录状态,无需重新扫码
"""
import os
import re
import gzip
import shutil
import json
import base64
import sqlite3
import subprocess
import sys
import time
import tempfile
import threading
import hashlib
import secrets
import functools
import math
import urllib.parse
import webbrowser
from datetime import datetime, timezone, timedelta

import requests
import schedule
import uvicorn
from selenium import webdriver
from selenium.webdriver import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, WebDriverException, SessionNotCreatedException
from fastapi import FastAPI, Header, Request, Query, Body
from fastapi.middleware.cors import CORSMiddleware

DOUYIN_HOME = 'https://www.douyin.com/'
DOUYIN_CHAT = 'https://www.douyin.com/chat?isPopup=1'
LOGIN_PANEL_XPATH = '//*[@id="douyin_login_comp_flat_panel"]'


# ========== 浏览器配置:直接使用本机 Google Chrome ==========

def _find_chrome_binary():
    """查找本机 Chrome 可执行文件(Windows 注册表+标准路径 / macOS 默认路径)"""
    candidates = [os.environ.get('CHROME_BIN', '')]
    if sys.platform == 'win32':
        try:
            import winreg
            for key_path in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
                             r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                        candidates.append(winreg.QueryValueEx(k, '')[0])
                except OSError:
                    continue
        except Exception:
            pass
        pf = os.environ.get('PROGRAMFILES', r'C:\Program Files')
        pf86 = os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')
        local = os.environ.get('LOCALAPPDATA', '')
        candidates += [
            os.path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(pf86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        ]
    else:
        candidates += [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            shutil.which('google-chrome') or '',
            shutil.which('chrome') or '',
            '/Applications/Chromium.app/Contents/MacOS/Chromium',
            shutil.which('chromium') or '',
        ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None

# ====== 打包态检测 ======
# 注意:Nuitka 故意不设置 sys.frozen(官方要求用 __compiled__ 检测),只有
# PyInstaller/cx_Freeze 才设 sys.frozen。只判 sys.frozen 会让 Nuitka 打的 exe
# 全程走进 dev 分支 —— 数据写进 onefile 临时解包目录,退出即被删除。
IS_FROZEN = bool(getattr(sys, 'frozen', False)) or ('__compiled__' in globals())

def _exe_dir() -> str:
    """打包版 exe 所在目录。onefile 下 sys.executable 指向临时解包出来的载荷,
    Nuitka 通过 NUITKA_ONEFILE_BINARY 暴露用户双击的那个原始 exe 路径。"""
    original = os.environ.get('NUITKA_ONEFILE_BINARY')
    if original and os.path.exists(original):
        return os.path.dirname(os.path.abspath(original))
    return os.path.dirname(os.path.abspath(sys.executable))

# ====== 数据目录:开发模式默认项目根(兼容现有 chrome_profile);
# 打包后(Nuitka onefile)必须用系统应用数据目录,__file__ 指向临时解包目录 ======
def _default_data_dir() -> str:
    if IS_FROZEN:
        if sys.platform == 'win32':
            base = os.environ.get('APPDATA') or os.path.expanduser('~')
            return os.path.join(base, 'DouyinSpark')
        return os.path.expanduser('~/Library/Application Support/DouyinSpark')
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = os.environ.get('SPARK_DATA_DIR') or _default_data_dir()
os.makedirs(APP_DIR, exist_ok=True)
# 持久化浏览器配置目录:重启服务后保持登录状态
PROFILE_DIR = os.path.join(APP_DIR, 'chrome_profile')
os.makedirs(PROFILE_DIR, exist_ok=True)

# ====== 启动日志:打包版控制台被隐藏(--windows-console-mode=hide),
# stdout 可能不可写,启动崩溃会完全静默。统一落盘到 APP_DIR/startup.log ======
LOG_PATH = os.path.join(APP_DIR, 'startup.log')
_log_teed = False

class _Tee:
    """同时写控制台(若可用)与日志文件;控制台写失败后自动降级为只写文件"""
    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, stream, fp):
        self._stream = stream
        self._fp = fp

    def write(self, s):
        if self._stream is not None:
            try:
                self._stream.write(s)
            except Exception:
                self._stream = None  # 控制台已隐藏/句柄失效,后续只写文件
        try:
            self._fp.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        for t in (self._stream, self._fp):
            try:
                if t is not None:
                    t.flush()
            except Exception:
                pass

    def isatty(self):
        return False  # 关掉 uvicorn/click 的 ANSI 着色,日志里不留控制字符

def _setup_startup_log():
    """打包版(或 SPARK_LOG_FILE=1)把 stdout/stderr 接到日志文件"""
    global _log_teed
    # Windows 控制台/输出重定向到文件时默认走 GBK,日志里的 emoji 会抛 UnicodeEncodeError
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if not IS_FROZEN and os.environ.get('SPARK_LOG_FILE') != '1':
        return
    try:
        fp = open(LOG_PATH, 'a', encoding='utf-8', errors='replace', buffering=1)
    except Exception:
        return
    sys.stdout = _Tee(sys.stdout, fp)
    sys.stderr = _Tee(sys.stderr, fp)
    _log_teed = True
    print(f'\n===== 启动 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} '
          f'(frozen={IS_FROZEN}, pid={os.getpid()}) =====')

_setup_startup_log()

def _platform_user_agent() -> str:
    if sys.platform == 'win32':
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    return 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

def build_options():
    options = webdriver.ChromeOptions()
    chrome_bin = _find_chrome_binary()
    if chrome_bin:
        options.binary_location = chrome_bin
    options.add_argument(f'--user-data-dir={PROFILE_DIR}')
    options.add_argument('--headless=new')  # 无头模式:用户看不到浏览器窗口,扫码二维码显示在 Web UI
    options.add_argument('--window-size=1280,900')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-infobars')
    options.add_argument('--disable-notifications')
    options.add_argument('--disable-popup-blocking')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('log-level=3')
    options.add_argument(f'user-agent={_platform_user_agent()}')
    options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging', 'useAutomationExtension'])
    return options

def create_driver():
    """创建浏览器实例:
    1) CHROMEDRIVER_BIN 指定驱动 → 直接使用(打包版同目录兜底)
    2) 否则 Selenium Manager 自动匹配,但先离线(用缓存驱动,国内网络下 SM 联网解析可能卡死),失败再联网下载"""
    options = build_options()
    driver_bin = os.environ.get('CHROMEDRIVER_BIN', '')
    if driver_bin:
        return webdriver.Chrome(service=ChromeService(executable_path=driver_bin), options=options)
    os.environ['SE_OFFLINE'] = 'true'
    try:
        return webdriver.Chrome(options=options)
    except Exception:
        # 缓存无驱动(首次使用)→ 允许联网下载
        os.environ['SE_OFFLINE'] = 'false'
        return webdriver.Chrome(options=options)


# ====== 机器码 & 单实例 ======

_machine_code_cache = None

def get_machine_code() -> str:
    """本机唯一机器码:Windows 取注册表 MachineGuid,macOS 取 IOPlatformUUID,sha256 归一化为 32 位。
    注意:重装系统后机器码会变化,需卖家重新发卡(换绑)。"""
    global _machine_code_cache
    if _machine_code_cache:
        return _machine_code_cache
    raw = None
    if sys.platform == 'win32':
        try:
            import winreg
            for key_path in (r'SOFTWARE\Microsoft\Cryptography',
                             r'SYSTEM\CurrentControlSet\Control\SystemInformation'):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                        if key_path.endswith('Cryptography'):
                            raw, _ = winreg.QueryValueEx(k, 'MachineGuid')
                        else:
                            for name in ('ComputerHardwareId', 'BIOSUUID'):
                                try:
                                    raw, _ = winreg.QueryValueEx(k, name)
                                    break
                                except OSError:
                                    continue
                        break
                except OSError:
                    continue
        except Exception:
            raw = None
    elif sys.platform == 'darwin':
        try:
            out = subprocess.run(['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                                 capture_output=True, text=True, timeout=10).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            raw = m.group(1) if m else None
        except Exception:
            raw = None
    if not raw:
        # 兜底(保证非空):用户名+主机名组合,稳定性差,仅用于异常环境
        raw = f"{os.environ.get('USER', '')}@{os.environ.get('COMPUTERNAME', '')}@{platform_node()}"
    _machine_code_cache = hashlib.sha256(raw.lower().encode()).hexdigest().upper()[:32]
    return _machine_code_cache

def platform_node() -> str:
    import socket
    return socket.gethostname()

_single_instance_handle = None  # Windows 互斥句柄,必须持引用防 GC 释放
_single_lock_file = None        # macOS flock 文件句柄

def acquire_single_instance() -> bool:
    """单实例锁:Windows 命名互斥体;macOS/Linux flock 锁文件。返回 False 表示已有实例在运行。"""
    global _single_instance_handle, _single_lock_file
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, 'Local\\DouyinSpark_SparkApp')
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                try:
                    ctypes.windll.user32.MessageBoxW(None, '程序已在运行,请勿重复打开。', '抖音火花助手', 0x30)
                except Exception:
                    print('程序已在运行,请勿重复打开。')
                return False
            _single_instance_handle = handle
            return True
        except Exception:
            return True  # 互斥创建失败不阻塞启动(非关键路径)
    else:
        lock_path = os.path.join(APP_DIR, 'app.lock')
        try:
            import fcntl
            f = open(lock_path, 'w')
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.write(str(os.getpid()))
            f.flush()
            _single_lock_file = f
            return True
        except (OSError, IOError):
            print('程序已在运行,请勿重复打开。')
            return False
    try:
        req = requests.get('https://v2.xxapi.cn/api/aiqinggongyu', timeout=10)
        if req.status_code == 200:
            data = req.json().get('data')
            if data:
                if isinstance(data, str):
                    return data
                try:
                    return json.dumps(data, ensure_ascii=False)
                except Exception:
                    return str(data)
    except Exception:
        pass
    return '暂无今日名言'

def format_time(time_str: str) -> str:
    """
    将时间字符串格式化为 HH:MM 格式
    例如: "9:23" -> "09:23", "9:5" -> "09:05", "09:23" -> "09:23"
    """
    if not time_str:
        return '22:00'

    # 统一替换中文冒号
    time_str = time_str.replace('：', ':').strip()

    try:
        # 分割小时和分钟
        parts = time_str.split(':')
        if len(parts) != 2:
            print(f'⚠️ 时间格式错误，使用默认时间 22:00')
            return '22:00'

        hour = int(parts[0])
        minute = int(parts[1])

        # 验证范围
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            print(f'⚠️ 时间范围错误，使用默认时间 22:00')
            return '22:00'

        # 格式化为两位数字
        return f"{hour:02d}:{minute:02d}"

    except ValueError:
        print(f'⚠️ 时间解析错误，使用默认时间 22:00')
        return '22:00'

class TrueString:
    def __init__(self, is_bool, string):
        self.is_bool = is_bool
        self.string = string

class UserFriendsInfo:
    def __init__(self, username, avatar, fire):
        self.username = username
        self.avatar = avatar
        self.fire = fire

class Douyin:
    def __init__(self, driver):
        self.driver = driver
        self.friends_xpath_list = {}  # {昵称: 会话元素xpath},实例级,每次刷新重建

    def PrintfFrinder(self):
        print(f'\n⏭️ 好友列表 共获取{len(self.friends_xpath_list)}位:\n------------------')
        for index in self.friends_xpath_list:
            print(index)
        print('------------------')

    def Updara_FrinderList(self):
        driver = self.driver
        self.friends_xpath_list = {}
        temp_list = []
        # 兜底多个包裹层类名(抖音改版时提高存活率)
        wrapper_xpaths = (
            '//div[contains(@class,"conversationConversationListwrapper")]',
            '//div[contains(@class,"ConversationListwrapper")]',
        )
        wrapper = None
        rows = []
        for w in wrapper_xpaths:
            rows = driver.find_elements(By.XPATH, f'{w}/div/div/div')
            if rows:
                wrapper = w
                break
        if not wrapper:
            return temp_list
        # 原版索引从第 2 个 div 开始(第 1 个通常是"消息"标题);昵称为空的行直接跳过
        for msg_len in range(1, len(rows) + 1):
            base = f'{wrapper}/div/div[{msg_len + 1}]'
            # ---- 昵称(必备)----
            name = ''
            name_xpath = ''
            for xp in (
                f'{base}/div[1]/div[2]/div[1]/div[1]',
                f'{base}/div[1]/div[2]/div/div[1]',
                f'{base}//div[contains(@class,"name")]',
            ):
                try:
                    name = driver.find_element(By.XPATH, xp).text.strip()
                except NoSuchElementException:
                    name = ''
                if name:
                    name_xpath = xp
                    break
            if not name:
                continue  # 标题行/空行
            # ---- 头像(可选)----
            avatar = ''
            for xp in (
                f'{base}/div[1]/div[1]/div/span/img',
                f'{base}/div/div/img',
                f'{base}//img',
            ):
                try:
                    avatar = driver.find_element(By.XPATH, xp).get_attribute('src') or ''
                except NoSuchElementException:
                    avatar = ''
                if avatar:
                    break
            # ---- 火苗天数(可选)----
            fire = ''
            for xp in (
                f'{base}/div[1]/div[2]/div[1]/div[2]/div[1]/div/div',
                f'{base}//div[contains(@class,"spark")]',
                f'{base}//*[contains(@class,"fire")]',
            ):
                try:
                    fire = driver.find_element(By.XPATH, xp).text.strip()
                except NoSuchElementException:
                    fire = ''
                if fire:
                    break
            self.friends_xpath_list[name] = name_xpath
            temp_list.append(UserFriendsInfo(name, avatar, fire))
        return temp_list

    def Send_Frinder(self, name: str, text: str):
        count = self.Updara_FrinderList()
        if not count:
            return TrueString(False, '更新好友列表失败或列表为空')
        friend_xpath = self.friends_xpath_list.get(name)
        if not friend_xpath:
            return TrueString(False, f'未找到好友: {name}')
        try:
            self.driver.find_element(By.XPATH, friend_xpath).click()
            time.sleep(1.5)
            editor = None
            for xp in (
                '//div[contains(@class,"messageEditorimChatEditorContainer")]/div/div',
                '//div[contains(@class,"imChatEditorContainer")]/div/div',
            ):
                try:
                    editor = self.driver.find_element(By.XPATH, xp)
                    break
                except NoSuchElementException:
                    continue
            if editor is None:
                return TrueString(False, '未找到聊天输入框')
            editor.click()
            editor.send_keys(text)
            editor.send_keys(Keys.ENTER)
            return TrueString(True, None)
        except Exception as e:
            return TrueString(False, str(e)[:200])

    def Find_Friends(self, name: str):
        count = self.Updara_FrinderList()
        if not count:
            return TrueString(False, '未初始化好友')
        if name in self.friends_xpath_list:
            return TrueString(True, None)
        return TrueString(False, f'未找到好友: {name}')


# 浏览器会话锁与崩溃自愈：抖音重页面偶发渲染进程崩溃(tab crashed)，
# 所有 driver 操作加锁串行化；检测到崩溃后自动重建浏览器会话
browser_lock = threading.Lock()

init = False
Login_is_bool = False
driver = None
douyin = None

def restart_driver():
    global driver, douyin, init, Login_is_bool
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    if not _find_chrome_binary():
        raise RuntimeError('未找到本机安装的 Google Chrome,请确认 /Applications/Google Chrome.app 存在,或用 CHROME_BIN 指定路径')
    driver = create_driver()
    driver.get(DOUYIN_CHAT)
    time.sleep(3)  # 等页面稳定,降低启动期渲染崩溃概率
    douyin = Douyin(driver)
    # 持久化配置下重启后可能仍保持登录
    try:
        Login_is_bool = any(c.get('name') == 'sessionid_ss' for c in driver.get_cookies())
    except Exception:
        Login_is_bool = False
    init = True

def ensure_login_dialog():
    """确保抖音登录弹窗存在；页面状态过期(弹窗关闭/被风控页替代)时重载聊天页"""
    for _attempt in range(3):
        try:
            if driver.find_elements(By.XPATH, LOGIN_PANEL_XPATH):
                return True
        except Exception:
            pass
        try:
            driver.get(DOUYIN_CHAT)
        except Exception:
            pass
        time.sleep(6)
    try:
        return bool(driver.find_elements(By.XPATH, LOGIN_PANEL_XPATH))
    except Exception:
        return False

def with_driver_recovery(fn):
    """driver 操作包装：加锁串行化；tab 崩溃时自动重建浏览器并重试一次"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with browser_lock:
            if not init:
                return {'code': 500, 'data': '浏览器未初始化,请先调用 /Api/Init'}
            try:
                driver.execute_script('return 1')  # 轻量探活，tab 崩溃时这里抛异常
            except WebDriverException as e:
                if 'tab crashed' in str(e):
                    try:
                        restart_driver()
                    except Exception as re_exc:
                        return {'code': 500, 'data': f'浏览器崩溃且自动重建失败: {str(re_exc)[:150]}'}
            try:
                return fn(*args, **kwargs)
            except WebDriverException as e:
                if 'tab crashed' in str(e):
                    try:
                        restart_driver()
                        return fn(*args, **kwargs)
                    except WebDriverException as e2:
                        return {'code': 500, 'data': f'浏览器崩溃且自动恢复失败: {str(e2)[:150]}'}
                raise
    return wrapper

# 定时任务发送(走锁 + 崩溃自愈),lambda 复用 with_driver_recovery 装饰器
_send_under_lock = with_driver_recovery(lambda name, text: douyin.Send_Frinder(name, text))

MAX_SEND_RETRIES = 3    # 发送失败最多重试次数
RETRY_DELAY_SECONDS = 60
_skipped_today = set()  # 今日因暂停被跳过的任务 db_id(恢复时补发)

def _cancel_retry_jobs(db_id):
    for j in list(schedule.jobs):
        if getattr(j, 'spark_retry_of', None) == db_id:
            schedule.cancel_job(j)

def _scheduled_send(db_id, name, text, attempt=0):
    """定时任务发送:失败 60s 后自动重试(最多 3 次),全程落 task_logs;暂停期间跳过并记录待补发"""
    run_time = time.strftime('%H:%M')
    # 授权失效时跳过(不进入补发队列,激活后次日正常执行)
    if check_license():
        db_log_task(db_id, name, run_time, 'skipped', '授权未激活或已过期,任务未执行')
        return
    if db_get_meta('tasks_paused') == '1':
        db_log_task(db_id, name, run_time, 'skipped', '任务已暂停(抖音登录失效或授权锁定)')
        _skipped_today.add(db_id)
        return
    out = None
    try:
        out = _send_under_lock(name, text)
    except Exception as e:
        out = TrueString(False, str(e)[:200])
    if out and out.is_bool:
        db_log_task(db_id, name, run_time, 'success', None)
        _cancel_retry_jobs(db_id)
        _skipped_today.discard(db_id)
        print(f'✅ 定时任务发送成功 → {name}')
        return
    err_msg = (out.string if out else '未知错误') or '发送失败'
    db_log_task(db_id, name, run_time, 'failed', err_msg)
    if attempt < MAX_SEND_RETRIES:
        print(f'⏰ 定时任务发送失败 → {name}: {err_msg}({RETRY_DELAY_SECONDS}s 后重试)')
        j = schedule.every(RETRY_DELAY_SECONDS).seconds.do(_scheduled_send, db_id, name, text, attempt + 1)
        j.spark_retry_of = db_id
    else:
        print(f'❌ 定时任务最终失败 → {name}: {err_msg}')

def _daily_task_job(db_id, name, text):
    """每日定时任务入口"""
    _scheduled_send(db_id, name, text, 0)

def register_task_job(db_id, name, text, play_time):
    """注册每日定时任务(新增与启动恢复共用)"""
    return schedule.every().day.at(play_time).do(_daily_task_job, db_id, name, text)

def restore_tasks_from_db():
    """服务启动时从 SQLite 恢复定时任务"""
    rows = db_load_tasks()
    for row in rows:
        task_id = f"{row['run_time']}_{row['friend_name']}"
        if task_id in scheduled_tasks:
            continue
        job = register_task_job(row['id'], row['friend_name'], row['message'], row['run_time'])
        scheduled_tasks[task_id] = {'job': job, 'db_id': row['id']}
    if rows:
        print(f'🔄 已从数据库恢复 {len(rows)} 个定时任务')

app = FastAPI()

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /api 前缀兼容:打包版没有 nginx/vite 代理,前端 baseURL 是 '/api',
# 由本中间件剥掉前缀后路由(dev 模式 vite 已剥前缀,无副作用)
from starlette.middleware.base import BaseHTTPMiddleware

class ApiPrefixMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.scope.get('path', '')
        if path.startswith('/api/'):
            request.scope['path'] = path[4:]
            request.scope['spark_original_path'] = path  # 供 SPA fallback 识别未知 API 路径
        return await call_next(request)

app.add_middleware(ApiPrefixMiddleware)

# 密码存储 (内存中，生产环境建议存入文件或数据库)
_password = os.environ.get('ADMIN_PASSWORD') or '123456'  # 默认密码

def hash_pwd(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()
# Token存储
_valid_tokens = set()
_last_login_ip = '无'
def generate_token() -> str:
    token = secrets.token_hex(32)
    _valid_tokens.add(token)
    return token
def verify_token(token: str) -> bool:
    return token in _valid_tokens
def remove_token(token: str):
    _valid_tokens.discard(token)
def require_auth(authorization: str = Header(None)):
    # 仅校验 token;授权(卡密)门禁只加在发送消息/创建任务等业务接口上,
    # 保证未激活用户也能进入首页、初始化浏览器、登录抖音、查看机器码
    if not authorization or not authorization.startswith('Bearer '):
        return {'code': 401, 'data': '未授权'}
    token = authorization[7:]
    if not verify_token(token):
        return {'code': 401, 'data': '未授权'}
    return None

def require_license():
    """业务门禁:未激活/过期/回拨时返回中文错误字典;有效返回 None"""
    err = check_license()
    if not err:
        return None
    _map = {
        'license_not_activated': '未激活授权,请联系卖家获取卡密后在「设置-授权信息」中激活',
        'license_expired': '授权已过期,请联系卖家续费',
        'license_rollback': '检测到系统时间异常,请恢复正确时间后重启',
        'license_locked': '授权已锁定',
        'license_config_missing': '程序未配置授权,请联系卖家',
        'license_account_mismatch': '当前登录的抖音账号与授权绑定的账号不一致,请登录绑定的抖音账号',
    }
    return {'code': 403, 'data': _map.get(err['data'], err['data'])}

# 定时任务存储
scheduled_tasks = {}  # 格式: {任务ID: job对象}


# 定时线程
def run_schedule():
    """后台线程运行定时任务"""
    while True:
        schedule.run_pending()
        time.sleep(1)

_scheduler_started = False
def start_scheduler():
    """启动定时任务调度线程(只启动一次)"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
    scheduler_thread.start()

start_time = datetime.now()

# ====== SQLite 存储 ======
DB_PATH = os.path.join(APP_DIR, 'douyin_spark.db')

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS license_activation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL UNIQUE,
    machine_code TEXT NOT NULL,
    days INTEGER NOT NULL,
    hours INTEGER NOT NULL DEFAULT 0,
    douyin_id TEXT NOT NULL DEFAULT '',
    activated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_time TEXT
);
CREATE TABLE IF NOT EXISTS used_cards (
    card_id TEXT PRIMARY KEY,
    used_at TEXT NOT NULL,
    machine_code TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_name TEXT NOT NULL UNIQUE,
    run_time TEXT NOT NULL,
    message TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    friend_name TEXT NOT NULL,
    run_time TEXT,
    status TEXT NOT NULL,
    message TEXT,
    executed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
'''

def _ensure_schema(conn):
    """表结构缺失时自动重建(防删库后新建空库导致静默失败);旧库自动补 hours 列"""
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='license_activation'").fetchone()
        if not row:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        else:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(license_activation)')]
            if 'hours' not in cols:
                conn.execute('ALTER TABLE license_activation ADD COLUMN hours INTEGER NOT NULL DEFAULT 0')
            if 'douyin_id' not in cols:
                conn.execute("ALTER TABLE license_activation ADD COLUMN douyin_id TEXT NOT NULL DEFAULT ''")
            conn.commit()
    except Exception:
        pass

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    _ensure_schema(conn)
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

def db_set_meta(key, value):
    try:
        with get_conn() as conn:
            conn.execute('INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)', (key, str(value)))
            conn.commit()
    except Exception:
        pass

def db_get_meta(key, default=None):
    try:
        with get_conn() as conn:
            row = conn.execute('SELECT value FROM app_meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default
    except Exception:
        return default

def db_save_cookies(cookies):
    """抖音登录 cookie 落库(供心跳探测使用),gzip+base64 存储"""
    if not cookies:
        return
    payload = base64.b64encode(gzip.compress(json.dumps(cookies).encode('utf-8'))).decode()
    db_set_meta('douyin_cookies', payload)

def db_load_cookies():
    payload = db_get_meta('douyin_cookies')
    if not payload:
        return []
    try:
        return json.loads(gzip.decompress(base64.b64decode(payload)).decode('utf-8'))
    except Exception:
        return []

def db_insert_task(friend_name, run_time, message):
    with get_conn() as conn:
        cur = conn.execute(
            'INSERT INTO tasks(friend_name, run_time, message, enabled, created_at) VALUES (?,?,?,1,?)',
            (friend_name, run_time, message, _utc_iso()))
        conn.commit()
        return cur.lastrowid

def db_update_task_time(db_id, new_time):
    with get_conn() as conn:
        conn.execute('UPDATE tasks SET run_time=? WHERE id=?', (new_time, db_id))
        conn.commit()

def db_delete_task(db_id):
    with get_conn() as conn:
        conn.execute('UPDATE task_logs SET task_id=NULL WHERE task_id=?', (db_id,))
        conn.execute('DELETE FROM tasks WHERE id=?', (db_id,))
        conn.commit()

def db_load_tasks():
    try:
        with get_conn() as conn:
            rows = conn.execute('SELECT * FROM tasks WHERE enabled=1').fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

def db_log_task(task_db_id, friend_name, run_time, status, message):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT INTO task_logs(task_id, friend_name, run_time, status, message, executed_at) VALUES (?,?,?,?,?,?)',
                (task_db_id, friend_name, run_time, status, (message or '')[:500], _utc_iso()))
            conn.commit()
    except Exception:
        pass

def db_get_task_logs(limit=100):
    try:
        with get_conn() as conn:
            rows = conn.execute('SELECT * FROM task_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ====== 授权 License(离线机器码绑定卡密) ======
# 公钥混淆存储:运行「卡密生成器.py init」会打印 _LICENSE_PUBLIC_KEY_OBFUSCATED,
# 粘贴到下面即可 —— 用户无法从源码/二进制直接读出公钥明文(配合 Nuitka 编译内置)。
# 环境变量 SPARK_LICENSE_PUBLIC_KEY 可覆盖(自动化测试用);未配置公钥程序拒绝启动。
_LICENSE_PUBLIC_KEY_OBFUSCATED = '0GSL3YW2ipHAOaJWWggxHTNbjyTinJtSTgfnhu0mNY7Wf/7EkfHpjKMPihk='

def _decode_license_public_key() -> str:
    try:
        key = hashlib.sha256(b'DouyinSpark-license-key-v1').digest()
        blob = base64.b64decode(_LICENSE_PUBLIC_KEY_OBFUSCATED)
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(blob)).decode('ascii')
    except Exception:
        return ''

LICENSE_PUBLIC_KEY_B64 = os.environ.get('SPARK_LICENSE_PUBLIC_KEY') or _decode_license_public_key()
ROLLBACK_TOLERANCE_MINUTES = 10   # 时钟回拨判定容差(容忍 NTP/校时抖动)
LICENSE_LOCKED = False            # 运行期授权锁(过期/回拨后置位,重新激活后复位)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_OK = True
except Exception:
    _CRYPTO_OK = False

def _license_config_ok() -> bool:
    """授权配置是否完整(公钥已填且密码学库可用)。没有 dev 模式:配置缺失一律 fail-closed。"""
    return bool(LICENSE_PUBLIC_KEY_B64.strip()) and _CRYPTO_OK

def _license_public_key():
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(LICENSE_PUBLIC_KEY_B64.strip()))

class LicenseError(Exception):
    """卡密/授权错误,code: 400格式 401签名 402机器码 403已用"""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _utc_iso(dt=None) -> str:
    return (dt or _now_utc()).isoformat()

def _parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def normalize_card(card: str) -> str:
    """归一化卡密:去空白、去 DY- 前缀、去分组符。
    注意:base64 大小写敏感,不能统一转大写(卡密只需粘贴,无需手敲)。"""
    c = (card or '').strip().replace(' ', '')
    if c.startswith('DY-'):
        c = c[3:]
    elif c.startswith('DY'):
        c = c[2:]
    return c.replace('-', '')

def _marker_paths():
    return [os.path.join(APP_DIR, '.spark_lic'), os.path.join(PROFILE_DIR, '.spark_lic')]

def _marker_key(machine_code: str) -> bytes:
    return hashlib.sha256((machine_code + 'spark-lic-v1').encode()).digest()

def write_license_markers(card_ids, machine_code: str, expires_at: str, douyin_id: str = ''):
    """双隐藏标记文件(防删库重放):XOR 混淆 + sha256 校验和。
    记录全部历史已用卡号,删库后旧卡仍无法重放。"""
    data = json.dumps({'card_ids': list(card_ids), 'machine_code': machine_code,
                       'expires_at': expires_at, 'douyin_id': douyin_id or ''}, sort_keys=True).encode()
    key = _marker_key(machine_code)
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    blob = hashlib.sha256(data).digest() + xored
    payload = base64.b64encode(blob).decode()
    for p in _marker_paths():
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as f:
                f.write(payload)
        except Exception:
            pass

def read_license_markers():
    """读取并校验标记文件,返回 {card_ids, machine_code, expires_at, douyin_id} 或 None"""
    mc = get_machine_code()
    key = _marker_key(mc)
    for p in _marker_paths():
        try:
            with open(p) as f:
                payload = f.read().strip()
            blob = base64.b64decode(payload)
            if len(blob) < 33:
                continue
            digest, xored = blob[:32], blob[32:]
            data = bytes(b ^ key[i % len(key)] for i, b in enumerate(xored))
            if hashlib.sha256(data).digest() != digest:
                continue
            info = json.loads(data.decode('utf-8'))
            if info.get('machine_code') == mc and isinstance(info.get('card_ids'), list):
                return info
        except Exception:
            continue
    return None

def _load_activation():
    try:
        with get_conn() as conn:
            row = conn.execute('SELECT * FROM license_activation ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def detect_clock_rollback(activation) -> bool:
    now = _now_utc()
    activated = _parse_iso(activation.get('activated_at'))
    if activated and now < activated - timedelta(minutes=ROLLBACK_TOLERANCE_MINUTES):
        return True
    last_seen = _parse_iso(activation.get('last_seen_time'))
    if last_seen and now < last_seen - timedelta(minutes=ROLLBACK_TOLERANCE_MINUTES):
        return True
    return False

def _touch_last_seen(activation):
    if not activation:
        return
    now_iso = _utc_iso()
    try:
        with get_conn() as conn:
            conn.execute('UPDATE license_activation SET last_seen_time=? WHERE id=?',
                         (now_iso, activation['id']))
            conn.commit()
    except Exception:
        pass

def license_status() -> dict:
    """授权状态(无鉴权接口用)"""
    mc = get_machine_code()
    if not _license_config_ok():
        return {'status': 'config_missing', 'machine_code': mc, 'activated_at': None,
                'expires_at': None, 'days_left': None, 'expires_in_seconds': None, 'now': _utc_iso()}
    act = _load_activation()
    if not act:
        return {'status': 'none', 'machine_code': mc, 'activated_at': None,
                'expires_at': None, 'days_left': None, 'expires_in_seconds': None, 'now': _utc_iso()}
    now = _now_utc()
    expires = _parse_iso(act.get('expires_at'))
    days_left = None
    expires_in_seconds = None
    if expires:
        expires_in_seconds = max(0, int((expires - now).total_seconds()))
        days_left = max(0, math.ceil(expires_in_seconds / 86400))
    bound_uid = (act.get('douyin_id') or '').strip()
    cur_uid = (db_get_meta('douyin_user_id') or '').strip()
    if detect_clock_rollback(act):
        status = 'rollback'
    elif expires and now >= expires:
        status = 'expired'
    elif bound_uid and cur_uid and cur_uid != bound_uid:
        status = 'account_mismatch'
    elif LICENSE_LOCKED:
        status = 'locked'
    else:
        status = act.get('status') or 'active'
    return {'status': status, 'machine_code': mc, 'activated_at': act.get('activated_at'),
            'expires_at': act.get('expires_at'), 'days_left': days_left,
            'expires_in_seconds': expires_in_seconds, 'bound_douyin_id': bound_uid, 'now': _utc_iso()}

def check_license():
    """授权校验:有效返回 None,否则返回 {code, data} 错误字典(接口可直接返回)。
    无 dev 模式:公钥未配置时一律 fail-closed。"""
    if not _license_config_ok():
        return {'code': 403, 'data': 'license_config_missing'}
    if LICENSE_LOCKED:
        return {'code': 403, 'data': 'license_locked'}
    act = _load_activation()
    if not act:
        return {'code': 403, 'data': 'license_not_activated'}
    if detect_clock_rollback(act):
        return {'code': 403, 'data': 'license_rollback'}
    expires = _parse_iso(act.get('expires_at'))
    if expires and _now_utc() >= expires:
        return {'code': 403, 'data': 'license_expired'}
    # 卡密绑定抖音账号:已登录时校验当前账号是否一致(未登录无法校验,登录即生效)
    bound_uid = (act.get('douyin_id') or '').strip()
    if bound_uid:
        cur_uid = (db_get_meta('douyin_user_id') or '').strip()
        if cur_uid and cur_uid != bound_uid:
            return {'code': 403, 'data': 'license_account_mismatch'}
    _touch_last_seen(act)
    return None

def verify_card(card: str) -> dict:
    """验签 + 机器码校验 + 防重放;返回 payload,失败抛 LicenseError"""
    if not _license_config_ok():
        raise LicenseError(400, '程序未配置授权公钥')
    token = normalize_card(card)
    if not token or '.' not in token:
        raise LicenseError(400, '卡密格式错误')
    payload_b64, sig_b64 = token.split('.', 1)
    try:
        payload = base64.b64decode(payload_b64)
        sig = base64.b64decode(sig_b64)
    except Exception:
        raise LicenseError(400, '卡密格式错误')
    try:
        _license_public_key().verify(sig, payload)
    except Exception:
        raise LicenseError(401, '卡密签名无效')
    try:
        data = json.loads(payload.decode('utf-8'))
    except Exception:
        raise LicenseError(400, '卡密数据损坏')
    if data.get('v') != 1 or not data.get('machine') or not isinstance(data.get('days'), int) or not data.get('card_id'):
        raise LicenseError(400, '卡密数据不完整')
    hours = data.get('hours', 0)
    if not isinstance(hours, int):
        hours = 0
    if not (0 <= data['days'] <= 3650) or not (0 <= hours <= 72):
        raise LicenseError(400, '卡密时长非法')
    if data['days'] + hours <= 0:
        raise LicenseError(400, '卡密时长非法')
    douyin_id = data.get('douyin_id') or ''
    if not isinstance(douyin_id, str) or len(douyin_id) > 64:
        raise LicenseError(400, '卡密抖音标识非法')
    if data['machine'] != get_machine_code():
        raise LicenseError(402, '卡密与本机机器码不匹配')
    try:
        with get_conn() as conn:
            row = conn.execute('SELECT 1 FROM used_cards WHERE card_id=?', (data['card_id'],)).fetchone()
    except Exception:
        row = None
    if row:
        raise LicenseError(403, '该卡密已被使用过')
    marker = read_license_markers()
    if marker and data['card_id'] in marker.get('card_ids', []):
        raise LicenseError(403, '该卡密已被使用过')
    return data

def activate_card(card: str) -> dict:
    """激活卡密:时长从激活时刻起算(支持天数+小时数);卡密绑定抖音标识时,
    必须在绑定的抖音账号登录状态下激活;成功后复位授权锁并恢复定时任务"""
    payload = verify_card(card)
    bound_uid = (payload.get('douyin_id') or '').strip()
    if bound_uid:
        # 卡密与抖音账号绑定:必须已登录且账号一致
        if not Login_is_bool:
            raise LicenseError(400, '请先登录抖音账号,再激活卡密(该卡密已与抖音账号绑定)')
        try:
            info = _extract_douyin_account(driver.page_source)
        except Exception:
            info = {}
        cur_uid = info.get('douyin_id') or ''
        if not cur_uid:
            raise LicenseError(400, '无法读取当前抖音账号标识,请确认已登录抖音')
        if cur_uid != bound_uid:
            raise LicenseError(402, '当前登录的抖音账号与卡密绑定的账号不一致,请切换到绑定的抖音账号后再激活')
    now = _now_utc()
    expires = now + timedelta(days=payload['days'], hours=payload.get('hours', 0))
    activated_at = _utc_iso(now)
    expires_at = _utc_iso(expires)
    with get_conn() as conn:
        conn.execute('DELETE FROM license_activation')
        conn.execute(
            'INSERT INTO license_activation(card_id, machine_code, days, hours, douyin_id, activated_at, expires_at, status, last_seen_time) VALUES (?,?,?,?,?,?,?,?,?)',
            (payload['card_id'], get_machine_code(), payload['days'], payload.get('hours', 0),
             bound_uid, activated_at, expires_at, 'active', activated_at))
        conn.execute('INSERT OR IGNORE INTO used_cards(card_id, used_at, machine_code) VALUES (?,?,?)',
                     (payload['card_id'], activated_at, get_machine_code()))
        conn.commit()
    if bound_uid:
        db_set_meta('douyin_user_id', bound_uid)  # 缓存绑定账号标识,运行时防换号
    marker = read_license_markers() or {}
    new_ids = list(dict.fromkeys(marker.get('card_ids', []) + [payload['card_id']]))
    write_license_markers(new_ids, get_machine_code(), expires_at, bound_uid)
    global LICENSE_LOCKED
    LICENSE_LOCKED = False
    resume_all_tasks()  # 重新激活后恢复定时任务
    print(f'✅ 卡密激活成功,到期时间: {expires_at}' + (f',绑定抖音标识: {bound_uid}' if bound_uid else ''))
    return {'card_id': payload['card_id'], 'activated_at': activated_at,
            'expires_at': expires_at, 'days': payload['days'], 'douyin_id': bound_uid}

def reconcile_markers():
    """启动时:DB 无激活记录但标记文件有效 → 用标记重建授权(合法清理不丢激活);
    DB 有记录但标记丢失 → 补写标记"""
    if not _license_config_ok():
        return
    act = _load_activation()
    if act:
        marker = read_license_markers() or {}
        ids = list(dict.fromkeys(marker.get('card_ids', []) + [act['card_id']]))
        write_license_markers(ids, act['machine_code'], act['expires_at'], act.get('douyin_id') or '')
        return
    info = read_license_markers()
    if not info or not info.get('card_ids'):
        return
    now_iso = _utc_iso()
    latest_card = info['card_ids'][-1]
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT INTO license_activation(card_id, machine_code, days, douyin_id, activated_at, expires_at, status, last_seen_time) VALUES (?,?,?,?,?,?,?,?)',
                (latest_card, info['machine_code'], 0, info.get('douyin_id') or '', now_iso, info['expires_at'], 'active', now_iso))
            for cid in info['card_ids']:
                conn.execute('INSERT OR IGNORE INTO used_cards(card_id, used_at, machine_code) VALUES (?,?,?)',
                             (cid, now_iso, info['machine_code']))
            conn.commit()
        print('🔁 已从标记文件恢复授权记录')
    except Exception:
        pass

def pause_tasks(reason: str = ''):
    """暂停全部定时任务(授权锁定/抖音掉线时调用)"""
    db_set_meta('tasks_paused', '1')
    print(f'⏸️ 定时任务已暂停: {reason}')

def resume_all_tasks():
    """恢复定时任务:取消暂停,并补发今日被跳过的任务"""
    db_set_meta('tasks_paused', '0')
    if _skipped_today:
        print(f'▶️ 恢复定时任务,补发 {len(_skipped_today)} 个今日跳过的任务')
        for info in list(scheduled_tasks.values()):
            job = info['job']
            args = getattr(job.job_func, 'args', ()) or ()
            if len(args) >= 3:
                db_id, name, text = args[0], args[1], args[2]
                if db_id in _skipped_today:
                    threading.Thread(target=_scheduled_send, args=(db_id, name, text, 0), daemon=True).start()
        _skipped_today.clear()

def _apply_license_lock(reason: str):
    global LICENSE_LOCKED, Login_is_bool
    if LICENSE_LOCKED:
        return
    LICENSE_LOCKED = True
    Login_is_bool = False
    print(f'🔒 授权已锁定: {reason}')
    try:
        _valid_tokens.clear()
    except Exception:
        pass
    try:
        schedule.clear()  # 移除全部定时任务与重试任务
    except Exception:
        pass
    pause_tasks(reason)
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass

def _license_guard_loop():
    while True:
        try:
            err = check_license()
            # 仅过期/回拨触发硬锁;未激活/未配置状态不锁(用户需要操作界面)
            if err and err['data'] in ('license_expired', 'license_rollback'):
                _apply_license_lock(err['data'])
        except Exception:
            pass
        time.sleep(10)

def start_license_guard():
    threading.Thread(target=_license_guard_loop, daemon=True).start()

# ====== 心跳探测(60s 检测抖音登录态,轻量 HTTP,不启动浏览器) ======
HEARTBEAT_INTERVAL = 60
# 探测 URL 必须选"强制登录态"的页面:抖音首页未登录也能正常渲染,无法判定登录态;
# 聊天页未登录时会渲染登录弹窗(douyin_login_comp_flat_panel)/跳转 passport,可准确判定
PROBE_URL = os.environ.get('SPARK_PROBE_URL', 'https://www.douyin.com/chat?isPopup=1')

def probe_login(cookies):
    """轻量 HTTP 探测,返回 (登录态, 抖音标识):
    登录态 True=已登录 False=已掉线 None=无法判定(网络异常/页面结构变化,本次不判定)
    判定依据:聊天页 SSR 数据 RENDER_DATA 中的 app.user.isLogin 布尔值(实测权威字段)"""
    if not cookies:
        return (None, None)
    session = requests.Session()
    for c in cookies:
        try:
            session.cookies.set(c.get('name', ''), c.get('value', ''),
                                domain=c.get('domain', ''), path=c.get('path', '/'))
        except Exception:
            continue
    try:
        r = session.get(PROBE_URL, timeout=15, allow_redirects=True,
                        headers={'User-Agent': _platform_user_agent()})
    except Exception:
        return (None, None)
    data = _parse_render_data(r.text)
    if data:
        try:
            user = data.get('app', {}).get('user', {})
            if isinstance(user, dict) and 'isLogin' in user:
                is_login = user.get('isLogin')
                if isinstance(is_login, str):
                    is_login = is_login.lower() in ('true', '1')
                uid = ''
                if is_login:
                    odin = data.get('app', {}).get('odin', {})
                    uid = str(user.get('unique_id') or user.get('uid')
                              or user.get('sec_uid') or odin.get('user_unique_id') or '')
                return (bool(is_login), uid)
        except Exception:
            pass
    # 兜底:被踢到 passport 登录域 → 未登录
    if 'passport.douyin.com' in r.url:
        return (False, None)
    return (None, None)  # 页面结构异常时不判定,避免误伤

def _heartbeat_loop():
    global Login_is_bool
    while True:
        try:
            # 授权异常时不探测(授权锁已暂停任务)
            if check_license():
                time.sleep(HEARTBEAT_INTERVAL)
                continue
            cookies = db_load_cookies()
            if cookies:
                result, uid = probe_login(cookies)
                heartbeat_state['last_probe'] = _utc_iso()
                if result is True:
                    heartbeat_state['last_ok'] = _utc_iso()
                    Login_is_bool = True
                    if uid:
                        db_set_meta('douyin_user_id', uid)  # 心跳顺带刷新账号标识缓存
                    if db_get_meta('tasks_paused') == '1':
                        resume_all_tasks()
                        print('💓 心跳:抖音已恢复登录,任务已恢复')
                elif result is False:
                    heartbeat_state['last_ok'] = None
                    Login_is_bool = False
                    if db_get_meta('tasks_paused') != '1':
                        pause_tasks('心跳检测到抖音登录已失效')
                    print('💔 心跳:抖音登录已失效,任务已暂停')
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)

def start_heartbeat():
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

# ====== 抖音账号唯一标识提取 ======

def _parse_render_data(page_or_html: str):
    """解析页面 SSR 数据 RENDER_DATA(URL 编码的 JSON),返回 dict 或 None"""
    m = re.search(r'<script id="RENDER_DATA" type="application/json"[^>]*>([^<]+)</script>', page_or_html)
    if not m:
        return None
    try:
        return json.loads(urllib.parse.unquote(m.group(1)))
    except Exception:
        return None

def _extract_douyin_account(page_source: str) -> dict:
    """从页面 SSR 数据(RENDER_DATA)提取抖音账号唯一标识。
    仅 isLogin 为真时字段可信 —— 未登录时 user_unique_id 是匿名轮换值,必须靠 isLogin 守卫。
    候选顺序: app.user.unique_id / uid / sec_uid / app.odin.user_unique_id"""
    result = {'logged_in': False, 'douyin_id': '', 'nickname': '', 'uid': ''}
    data = _parse_render_data(page_source)
    if data:
        try:
            app = data.get('app', {})
            user = app.get('user', {})
            is_login = user.get('isLogin')
            if isinstance(is_login, str):
                is_login = is_login.lower() in ('true', '1')
            result['logged_in'] = bool(is_login)
            if result['logged_in']:
                odin = app.get('odin', {})
                result['douyin_id'] = str(user.get('unique_id') or user.get('uid')
                                          or user.get('sec_uid') or odin.get('user_unique_id') or '')
                result['uid'] = str(user.get('uid') or '')
                result['nickname'] = str(user.get('nickname') or '')
        except Exception:
            pass
    if result['logged_in'] and not result['nickname']:
        m2 = re.search(r'\\"nickname\\":\s*\\"([^\\"]+)\\"', page_source)
        if m2:
            result['nickname'] = m2.group(1)
    return result

def _cache_douyin_user_id():
    """登录成功后从浏览器页面提取并缓存当前抖音账号标识(用于卡密账号绑定校验)"""
    try:
        info = _extract_douyin_account(driver.page_source)
        uid = info.get('douyin_id') or ''
        if uid:
            db_set_meta('douyin_user_id', uid)
    except Exception:
        pass

# ========== 抖音操作 ==========

# ========== 抖音操作 ==========
@app.get('/Home')
def Home(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'time': start_time}

# ========== 授权与系统接口 ==========

@app.get('/Api/GetMachineCode')  # 获取本机机器码(未激活界面显示)
def GetMachineCode():
    return {'code': 200, 'data': get_machine_code()}

@app.post('/Api/Activate')  # 激活卡密(时长从激活时刻起算)
def Activate(card: str = Body(default=None, embed=True)):  # embed=True: 前端发送 {"card": "..."} 对象
    if not card:
        return {'code': 400, 'data': '请输入卡密'}
    try:
        info = activate_card(card.strip())
    except LicenseError as e:
        return {'code': e.code, 'data': str(e)}
    except Exception as e:
        return {'code': 500, 'data': f'激活失败: {str(e)[:150]}'}
    return {'code': 200, 'data': info}

@app.get('/Api/LicenseStatus')  # 授权状态(激活页/锁屏页轮询)
def LicenseStatus():
    return {'code': 200, 'data': license_status()}

# 心跳状态(心跳线程启动后持续更新)
heartbeat_state = {'last_probe': None, 'last_ok': None, 'interval': HEARTBEAT_INTERVAL}

@app.get('/Api/Status')  # 聚合状态:登录/暂停/心跳/授权
def Status(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': {
        'login': 'Yes' if Login_is_bool else 'No',
        'browser_init': 'Yes' if init else 'No',
        'tasks_paused': db_get_meta('tasks_paused') == '1',
        'heartbeat': heartbeat_state,
        'license': license_status(),
    }}

@app.get('/Api/LocalBootstrap')  # 本机免密引导:签发本地 token(仅限 127.0.0.1,不校验授权)
def LocalBootstrap(request: Request):
    if request.client.host not in ('127.0.0.1', '::1'):
        return {'code': 403, 'data': '仅允许本机访问'}
    return {'code': 200, 'data': generate_token()}

@app.post('/Api/Shutdown')  # 退出程序(桌面版)
def Shutdown(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    def _do_shutdown():
        time.sleep(0.5)
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {'code': 200, 'data': '正在退出...'}

@app.get('/Api/SetAutoStart')  # 开机自启开关(定时任务需要程序常驻)
def SetAutoStart(enable: str = Query('0'), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    want = str(enable).strip() in ('1', 'true', 'True', 'yes')
    try:
        if sys.platform == 'win32':
            import winreg
            key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
            # onefile 下 sys.executable 是临时解包出来的载荷,重启后已不存在,必须写原始 exe
            exe = os.environ.get('NUITKA_ONEFILE_BINARY') or sys.executable
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
                if want:
                    winreg.SetValueEx(k, 'DouyinSpark', 0, winreg.REG_SZ, f'"{exe}"')
                else:
                    try:
                        winreg.DeleteValue(k, 'DouyinSpark')
                    except OSError:
                        pass
        else:
            plist_dir = os.path.expanduser('~/Library/LaunchAgents')
            plist_path = os.path.join(plist_dir, 'com.douyin.spark.plist')
            if want:
                exe = os.environ.get('NUITKA_ONEFILE_BINARY') or sys.executable
                script = os.path.abspath(__file__)
                args = [exe] if IS_FROZEN else [exe, script]
                plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.douyin.spark</string>
<key>ProgramArguments</key><array>
{"".join(f"<string>{a}</string>" for a in args)}
</array>
<key>RunAtLoad</key><true/>
</dict></plist>'''
                os.makedirs(plist_dir, exist_ok=True)
                with open(plist_path, 'w') as f:
                    f.write(plist)
            else:
                if os.path.exists(plist_path):
                    os.remove(plist_path)
        return {'code': 200, 'data': f'开机自启已{"开启" if want else "关闭"}'}
    except Exception as e:
        return {'code': 500, 'data': f'设置失败: {str(e)[:150]}'}

@app.get('/Api/Init')  # 初始化浏览器
def Init(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err

    global init, driver, douyin

    if not init:
        try:
            with browser_lock:
                restart_driver()
            start_scheduler()  # 启动调度线程
            return {'code': 200, 'data': 'success'}
        except SessionNotCreatedException as e:
            if "This version of ChromeDriver only supports" in str(e):
                return {'code': 400, 'data': 'chromedriver 版本与本机 Chrome 不匹配:建议移除环境中的 chromedriver 让 Selenium Manager 自动匹配,或更新驱动'}
            return {'code': 400, 'data': f'浏览器会话创建失败: {str(e)[:200]}'}
        except Exception as e:
            return {'code': 500, 'data': f'初始化失败: {str(e)[:200]}'}
    else:
        return {'code': 200, 'data': 'init Repeated!'}

@app.get('/Api/GetInit')
def GetInit(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': 'Yes' if init else 'No'}


# ========== Cookie 解析(登录用) ==========
ALLOWED_COOKIE_KEYS = ('name', 'value', 'domain', 'path', 'secure', 'httpOnly', 'expiry', 'sameSite')

def parse_cookie_payload(cooke: str, gzip_flag: bool):
    """解析客户端传来的 Cookie 数据,逐层剥离直到得到 cookie 字典列表。

    前端 /Api/login 会把用户粘贴的内容 JSON.stringify 后再 gzip+base64,
    因此支持以下输入格式(可嵌套组合):
    1. 纯 JSON 列表/对象:   [{"name":...,"value":...}, ...]
    2. base64 的 JSON:      <b64(JSON)>
    3. 旧版双层 base64:     <b64(<b64(JSON)>)>
    4. JSON 字符串包 base64: "<b64(JSON)>"(前端 JSON.stringify 产生的引号层)
    5. GetCooke 接口返回的 gzip+base64(gzip_flag=True,或用"字符串包base64"方式粘贴)
    """
    raw = cooke.strip()
    if not raw:
        raise ValueError('cookie 为空')

    def try_b64(data: str):
        try:
            padded = data + '=' * (-len(data) % 4)
            return base64.b64decode(padded, validate=True)
        except Exception:
            return None

    # 1) 外层:前端总是 base64 编码(自动补齐 = 号)
    blob = try_b64(raw)
    if gzip_flag:
        if blob is None:
            raise ValueError('gzip 标志为真,但 cookie 不是有效 base64')
        try:
            blob = gzip.decompress(blob)
        except Exception as e:
            raise ValueError(f'gzip 解压失败,请确认数据来源与 gzip 标志: {e}')
    if blob is not None:
        try:
            text = blob.decode('utf-8').strip()
        except UnicodeDecodeError:
            text = raw  # 解码失败,退回原始文本
    else:
        text = raw

    # 2) 逐层剥离:JSON 字符串 -> base64 -> (可选 gzip) -> JSON,最多 5 层
    cookies = None
    for _layer in range(5):
        stripped = text.strip()
        if stripped.startswith(('{', '[', '"')):
            try:
                obj = json.loads(stripped)
            except Exception:
                raise ValueError(f'cookie JSON 解析失败: {stripped[:80]}...')
            if isinstance(obj, list):
                cookies = obj
                break
            if isinstance(obj, dict):
                cookies = [obj]
                break
            if isinstance(obj, str):  # JSON 字符串包装的一层,继续剥
                text = obj.strip()
                continue
            raise ValueError('cookie JSON 格式错误: 应为列表/对象/字符串')
        # 尝试 base64 剥层
        blob = try_b64(text)
        if blob is not None:
            try:
                text = blob.decode('utf-8').strip()
                continue
            except UnicodeDecodeError:
                pass  # 可能是 gzip 二进制,下面尝试解压
            try:
                text = gzip.decompress(blob).decode('utf-8').strip()
                continue
            except Exception:
                raise ValueError('cookie 无法解析: base64 内容既不是文本也不是 gzip')
        raise ValueError(f'cookie 无法解析: 既不是 JSON 也不是有效 base64 ({stripped[:80]}...)')

    if cookies is None:
        raise ValueError('cookie 解析失败')
    if not all(isinstance(c, dict) for c in cookies):
        raise ValueError('cookie 格式错误: 应为字典列表')
    return cookies

@app.post('/Api/login')  # 登录 传入cooke
@with_driver_recovery
def Login(cooke: str = Body(default=None), gzip_flag: bool = Body(default=False), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    global Login_is_bool
    if not cooke:
        return {'code': 404, 'data': 'login-error-not cooker'}
    try:
        cookie_list = parse_cookie_payload(cooke, gzip_flag)
    except Exception as e:
        return {'code': 404, 'data': f'login-error-cookie parse error: {e}(如使用 GetCooke 接口返回的数据,请携带 gzip_flag=true)'}

    # 先打开 douyin 域名再注入 cookie(域名不符会被浏览器丢弃)
    driver.get(DOUYIN_HOME)
    ok_count = 0
    for cookie in cookie_list:
        clean = {k: cookie[k] for k in ALLOWED_COOKIE_KEYS if k in cookie and cookie[k] not in (None, '')}
        if 'name' not in clean or 'value' not in clean:
            continue
        # sameSite 归一化,避免 chromedriver 报非法值
        ss = str(clean.get('sameSite', '')).lower()
        if ss in ('lax', 'strict', 'none', 'no_restriction'):
            clean['sameSite'] = {'no_restriction': 'None'}.get(ss, ss.capitalize())
        else:
            clean.pop('sameSite', None)
        try:
            driver.add_cookie(clean)
            ok_count += 1
        except Exception as e:
            print(f'⚠️ 注入 cookie 失败({clean.get("name")}): {e}')
    if ok_count == 0:
        return {'code': 404, 'data': 'login-error-no valid cookie injected'}

    # 打开聊天页验证登录结果
    driver.get(DOUYIN_CHAT)
    time.sleep(3)
    panel_exists = bool(driver.find_elements(By.XPATH, LOGIN_PANEL_XPATH))
    has_session = any(c.get('name') == 'sessionid_ss' for c in driver.get_cookies())
    if panel_exists or not has_session:
        Login_is_bool = False
        return {'code': 404, 'data': 'login-error-cooker cant login(Cookie 已失效或未登录)'}
    Login_is_bool = True
    db_save_cookies(driver.get_cookies())  # 供心跳探测使用
    _cache_douyin_user_id()  # 缓存当前抖音账号标识(卡密账号绑定校验)
    print('✅ Cookie 登录成功')
    return {'code': 200, 'data': 'ok'}

@app.get('/Api/Pnglogin')  # 扫码登录状态检测
@with_driver_recovery
def PngLogin(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    global Login_is_bool
    # 已登录:直接提示(兼容轮询:仍返回 code 200)
    if Login_is_bool:
        return {'code': 200, 'data': 'already_logged_in'}
    # 不刷新页面（刷新会打断正在进行的扫码）；通过“登录弹窗消失 + 存在登录会话cookie”判定登录成功
    try:
        panel_exists = bool(driver.find_elements(By.XPATH, LOGIN_PANEL_XPATH))
    except Exception:
        panel_exists = True
    cooke = driver.get_cookies()
    has_session = any(c.get('name') == 'sessionid_ss' for c in cooke)
    if not panel_exists and has_session:
        Login_is_bool = True
        db_save_cookies(cooke)  # 供心跳探测使用
        _cache_douyin_user_id()  # 缓存当前抖音账号标识(卡密账号绑定校验)
        print('✅ 扫码登录成功')
        return {'code': 200, 'data': 'ok'}
    return {'code': 404, 'data': '未登录,请继续扫码'}

@app.get('/Api/GetLogin')  # 获取登录
def GetLogin(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': 'Yes' if Login_is_bool else 'No'}

@app.get('/Api/login/Init/GetLoginPng')  # 获取登录扫码
@with_driver_recovery
def GetLoginPng(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 已登录:直接提示,无需弹登录框(扫码入口)
    if Login_is_bool:
        return {'code': 200, 'data': 'already_logged_in'}
    if not ensure_login_dialog():
        return {'code': 404, 'data': '登录弹窗未出现,请重试'}
    for _attempt in range(3):
        login_src = None
        try:
            img_element = driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]//img')
            login_src = img_element.get_attribute('src')
        except NoSuchElementException:
            login_src = None
        if login_src:
            return {'code': 200, 'data': login_src}
        # 二维码已扫描/失效时点击刷新层获取新二维码；无刷新层时静默失败
        try:
            driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/div').click()
        except Exception:
            pass
        time.sleep(3)
    return {'code': 404, 'data': 'cant find LoginPng src attribute'}

@app.get('/Api/login/Init/GetCooker')  # 获取cooke
@with_driver_recovery
def GetCooke(password: str = Query(None), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 验证密码
    if not password or hash_pwd(password) != hash_pwd(_password):
        return {'code': 401, 'data': '密码错误'}
    if Login_is_bool:
        cooke = driver.get_cookies()
        cookie_json = json.dumps(cooke)
        # 先gzip压缩，再base64编码
        cookie_gzip = gzip.compress(cookie_json.encode('utf-8'))
        cookie_base64 = base64.b64encode(cookie_gzip).decode('utf-8')
        return {'code': 200, 'data': {'cooke': cookie_base64, 'gzip_flag': True}}
    else:
        return {'code': 401, 'data': '未登录'}

@app.get('/Api/GetFriendsList')  # 获取好友列表
@with_driver_recovery
def GetFriendsList(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not Login_is_bool:
        # 前端 Friends.vue 按 code==401 弹"前往登录"引导,这里必须返回 401 而非 404
        return {'code': 401, 'data': '未登录,无法获取好友列表'}
    try:
        friends_list = douyin.Updara_FrinderList()
    except Exception as e:
        return {'code': 404, 'data': f'获取好友列表失败: {str(e)[:200]}'}
    if not friends_list:
        return {'code': 404, 'data': '暂无好友或页面未加载'}
    dicts = {v.username: [v.avatar, v.fire] for v in friends_list}
    return {'code': 200, 'data': {'count': len(friends_list), 'list': dicts}}

@app.get('/Api/Send')  # 发送信息(需授权:激活且未过期)
def Send(name: str, text: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 授权检查优先于浏览器检查,未激活时提示更准确
    license_err = require_license()
    if license_err:
        return license_err
    return _send_with_driver(name, text)

@with_driver_recovery
def _send_with_driver(name: str, text: str):
    if not name or not text:
        return {'code': 404, 'data': 'name/text 不能为空'}
    out = douyin.Send_Frinder(name, text)
    if out.is_bool:
        return {'code': 200, 'data': 'Send successfully'}
    return {'code': 404, 'data': out.string or '发送失败'}

@app.get('/Api/GetDouyinUserInfo')  # 抖音账号唯一标识(设置页授权信息卡展示)
@with_driver_recovery
def GetDouyinUserInfo(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not Login_is_bool:
        return {'code': 200, 'data': {'logged_in': False, 'douyin_id': '', 'nickname': '', 'uid': ''}}
    try:
        info = _extract_douyin_account(driver.page_source)
        info['logged_in'] = info['logged_in'] or Login_is_bool
        if info.get('douyin_id'):
            db_set_meta('douyin_user_id', info['douyin_id'])  # 刷新账号标识缓存
        return {'code': 200, 'data': info}
    except Exception as e:
        return {'code': 400, 'data': f'获取失败: {str(e)[:150]}'}

@app.get('/Api/GetUsername')  # 获取用户名
@with_driver_recovery
def GetUserInfo(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not Login_is_bool:
        return {'code': 400, 'data': '未登录'}
    try:
        page = driver.page_source
    except Exception as e:
        return {'code': 400, 'data': f'获取页面失败: {str(e)[:150]}'}
    for pattern in (r'\\"nickname\\":\s*\\"([^\\"]+)\\"', r'"nickname":\s*"([^"]+)"'):
        match = re.search(pattern, page)
        if match:
            return {'code': 200, 'data': match.group(1)}
    return {'code': 400, 'data': '已登录,但未获取到用户名'}

@app.get('/Api/GetScrlk')  # 获取截图
@with_driver_recovery
def GetScrlk(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not Login_is_bool:
        return {'code': 401, 'data': '您还未登录'}
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            tmp_path = f.name
        driver.save_screenshot(tmp_path)
        with open(tmp_path, 'rb') as f:
            img_data = base64.b64encode(f.read()).decode('utf-8')
        return {'code': 200, 'data': img_data}
    except Exception as e:
        return {'code': 400, 'data': f'截图错误:{str(e)[:150]}'}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

@app.get('/Api/DieLogin')  # 取消登录
@with_driver_recovery
def DieLogin(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    global Login_is_bool
    driver.delete_all_cookies()
    Login_is_bool = False
    return {'code': 200, 'data': '已清除Cooke'}

@app.get('/Api/LoginPhone')  # 验证码登录
@with_driver_recovery
def LoginPhone(phone: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 已登录:直接提示,无需验证码登录
    if Login_is_bool:
        return {'code': 200, 'data': 'already_logged_in'}
    if not re.fullmatch(r'1\d{10}', phone):
        return {'code': 400, 'data': '手机号格式错误'}
    if not ensure_login_dialog():
        return {'code': 400, 'data': '登录弹窗未出现,请重试'}
    try:
        # 无头浏览器无历史状态，登录弹窗默认停留在扫码页签，需先切换到“验证码登录”页签
        try:
            tab = driver.find_element(By.XPATH, f'{LOGIN_PANEL_XPATH}//*[text()="验证码登录"]')
            tab.click()
            time.sleep(1.5)
        except NoSuchElementException:
            pass
        inp = driver.find_element(By.XPATH, '//*[@id="normal-input"]')
        inp.clear()
        inp.send_keys(phone)
        span = driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_button_input_id"]/span')
        span.click()
        time.sleep(3)
        if span.text.strip() == '获取验证码':
            return {'code': 400, 'data': '验证码发送失败'}
        return {'code': 200, 'data': '验证码发送成功'}
    except WebDriverException:
        raise  # 交给自愈装饰器重建浏览器并重试
    except Exception as e:
        return {'code': 400, 'data': str(e)[:200]}

@app.get('/Api/LoginPhoneInput')  # 验证码登录 2 输入验证码
@with_driver_recovery
def LoginPhoneInput(code: str, authorization: str = Header(None)):
    global Login_is_bool
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    if not code or not code.strip():
        return {'code': 400, 'data': '验证码不能为空'}
    try:
        inp = driver.find_element(By.XPATH, '//*[@id="button-input"]')
        inp.clear()
        inp.send_keys(code.strip())
        button = driver.find_element(By.XPATH, '//*[@id="douyin_login_comp_btn_id"]')
        button.click()
        time.sleep(3)
        panel_exists = bool(driver.find_elements(By.XPATH, LOGIN_PANEL_XPATH))
        has_session = any(c.get('name') == 'sessionid_ss' for c in driver.get_cookies())
        if not panel_exists and has_session:
            Login_is_bool = True
            db_save_cookies(driver.get_cookies())
            _cache_douyin_user_id()  # 缓存当前抖音账号标识(卡密账号绑定校验)
            print('✅ 验证码登录成功')
            return {'code': 200, 'data': '登录成功'}
        return {'code': 400, 'data': '登录失败(验证码错误或已过期)'}
    except WebDriverException:
        raise
    except Exception as e:
        return {'code': 400, 'data': str(e)[:200]}


# ========== 定时任务操作 ==========
@app.get('/Time/add')
def add_time(time: str, name: str, text: str = None, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    # 创建任务需授权(激活且未过期);授权检查优先于浏览器检查
    license_err = require_license()
    if license_err:
        return license_err
    return _add_time_with_driver(time, name, text)

@with_driver_recovery
def _add_time_with_driver(time: str, name: str, text: str = None):
    if not name:
        return {'code': 400, 'data': 'name 不能为空'}
    # 检查是否已存在该好友的定时任务
    for task_id in scheduled_tasks:
        if task_id.endswith(f"_{name}"):
            return {'code': 400, 'data': f'好友 {name} 已有定时任务，请先删除或修改'}

    temp = douyin.Find_Friends(name)
    if temp.is_bool:
        play_time = format_time(time)
        msg = AiqingGongyu_text() if text in (None, '') else text
        # 先写数据库(重启后自动恢复)
        db_id = db_insert_task(name, play_time, msg)
        job = register_task_job(db_id, name, msg, play_time)
        # 生成唯一任务ID
        task_id = f"{play_time}_{name}"
        scheduled_tasks[task_id] = {'job': job, 'db_id': db_id}
        return {'code': 200, 'data': f'已添加定时任务: {play_time}', 'task_id': task_id}
    else:
        return {'code': 404, 'data': temp.string}

@app.get('/Time/del')
def del_time(task_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """根据任务ID删除定时任务"""
    if task_id in scheduled_tasks:
        info = scheduled_tasks[task_id]
        schedule.cancel_job(info['job'])
        _cancel_retry_jobs(info['db_id'])
        db_delete_task(info['db_id'])
        del scheduled_tasks[task_id]
        return {'code': 200, 'data': f'已删除任务: {task_id}'}
    else:
        return {'code': 404, 'data': '任务ID不存在'}

@app.get('/Time/edit')
def edit_time(name: str, new_time: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """修改指定好友的定时任务时间"""
    # 查找该好友的现有任务
    old_task_id = None
    for task_id in scheduled_tasks:
        if task_id.endswith(f"_{name}"):
            old_task_id = task_id
            break

    if not old_task_id:
        return {'code': 404, 'data': f'好友 {name} 没有定时任务'}

    # 取消旧任务
    old_info = scheduled_tasks[old_task_id]
    old_job = old_info['job']
    old_db_id = old_info['db_id']
    schedule.cancel_job(old_job)
    _cancel_retry_jobs(old_db_id)

    # 解析旧任务信息
    parts = old_task_id.split('_', 1)
    old_time = parts[0] if len(parts) == 2 else ""

    # 创建新任务
    new_play_time = format_time(new_time)
    msg = AiqingGongyu_text()  # 获取新的名言
    db_update_task_time(old_db_id, new_play_time)
    new_job = register_task_job(old_db_id, name, msg, new_play_time)

    # 生成新任务ID并替换
    new_task_id = f"{new_play_time}_{name}"
    scheduled_tasks[new_task_id] = {'job': new_job, 'db_id': old_db_id}
    del scheduled_tasks[old_task_id]

    return {
        'code': 200,
        'data': f'已将 {name} 的定时任务从 {old_time} 修改为 {new_play_time}',
        'old_time': old_time,
        'new_time': new_play_time,
        'task_id': new_task_id
    }

@app.get('/Time/getlist')
def get_time_list(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """获取当前所有定时任务列表"""
    tasks = []
    paused = db_get_meta('tasks_paused') == '1'
    for task_id, info in scheduled_tasks.items():
        # 解析任务ID获取信息
        parts = task_id.split('_', 1)
        if len(parts) == 2:
            time_str, name = parts
            job = info['job']
            tasks.append({
                'task_id': task_id,
                'time': time_str,
                'name': name,
                'next_run': str(job.next_run) if job.next_run else None,
                'paused': paused,
            })
    return {'code': 200, 'data': {'count': len(tasks), 'tasks': tasks}}

@app.get('/Api/GetTaskLogs')  # 任务执行日志
def GetTaskLogs(limit: int = Query(50), authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': {'logs': db_get_task_logs(min(max(limit, 1), 500))}}


# ========== 后台登录 ==========
@app.get('/Api/Login/Admin')
def admin_login(username: str, password: str, request: Request = None):
    global _last_login_ip
    if username == 'admin' and hash_pwd(password) == hash_pwd(_password):
        _last_login_ip = request.client.host if request else '127.0.0.1'
        token = generate_token()
        return {'code': 200, 'data': token}
    else:
        return {'code': 400, 'data': '登录失败'}

@app.get('/Api/GetLastLoginIP')
def get_last_login_ip(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    return {'code': 200, 'data': _last_login_ip}

# 退出登录
@app.get('/Api/logout')
def logout(authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    token = authorization[7:]
    remove_token(token)
    return {'code': 200, 'data': '已退出登录'}

# 密码修改
@app.get('/Api/ChangePassword')
def change_password(old_password: str, new_password: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    global _password
    if hash_pwd(old_password) != hash_pwd(_password):
        return {'code': 400, 'data': '原密码错误'}
    _password = new_password
    return {'code': 200, 'data': '密码修改成功'}

# ====== 打包版启动引导 ======
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist')

def setup_static():
    """打包版:后端托管前端构建产物(SPA fallback);未知 API 路径返回 404 JSON"""
    if not os.path.isdir(STATIC_DIR):
        return
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, JSONResponse
    app.mount('/assets', StaticFiles(directory=os.path.join(STATIC_DIR, 'assets')), name='assets')

    @app.get('/{full_path:path}')
    def spa_fallback(request: Request, full_path: str):
        original = request.scope.get('spark_original_path', '')
        if original.startswith('/api/'):
            return JSONResponse({'code': 404, 'data': '接口不存在'}, status_code=404)
        target = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(target):
            return FileResponse(target)
        return FileResponse(os.path.join(STATIC_DIR, 'index.html'))

def pick_port(preferred: int = 9844, attempts: int = 20) -> int:
    """优先用固定端口(用户可预测、能手动访问),被占用则顺延;全被占用才回退随机端口。
    随机端口 + 隐藏控制台 = 浏览器没弹出来时用户完全无从下手,所以不再默认随机。"""
    import socket
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def _open_url(url: str) -> bool:
    """打开浏览器,多级兜底,每步都落日志。
    Windows 冻结 exe 下 webbrowser.open 可能返回 True 但实际没弹浏览器(默认浏览器
    关联缺失/被组策略限制),日志还完全静默 —— 所以 Windows 优先直接用 ShellExecute
    (os.startfile,冻结程序里最可靠),webbrowser.open 降级为兜底。"""
    if sys.platform == 'win32':
        try:
            os.startfile(url)
            print('✅ 浏览器已打开(os.startfile)')
            return True
        except Exception as e:
            print(f'⚠️ os.startfile 失败: {e}')
    try:
        if webbrowser.open(url):
            print('✅ 浏览器已打开(webbrowser.open)')
            return True
        print('⚠️ webbrowser.open 返回 False')
    except Exception as e:
        print(f'⚠️ webbrowser.open 失败: {e}')
    if sys.platform == 'win32':
        try:
            subprocess.Popen(['cmd', '/c', 'start', '', url], creationflags=0x08000000)  # CREATE_NO_WINDOW
            print('✅ 浏览器已打开(cmd start)')
            return True
        except Exception as e:
            print(f'⚠️ cmd start 失败: {e}')
    else:
        try:
            subprocess.Popen(['open', url])
            print('✅ 浏览器已打开(open)')
            return True
        except Exception as e:
            print(f'⚠️ open 失败: {e}')
    return False

def open_browser_later(port: int):
    url = f'http://127.0.0.1:{port}/home'  # 启动直接进首页
    url_file = os.path.join(APP_DIR, 'url.txt')
    try:
        with open(url_file, 'w', encoding='utf-8') as f:
            f.write(url + '\n')
    except Exception:
        pass
    print(f'🌐 浏览器将自动打开: {url}')

    def _open_final():
        if _open_url(url):
            return
        print(f'❌ 无法自动打开浏览器,请手动访问: {url}')
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, f'未能自动打开浏览器,请手动在浏览器中访问:\n\n{url}\n\n'
                          f'该地址也已保存到:\n{url_file}', '抖音火花助手', 0x40)
            except Exception:
                pass

    def _open():
        if _open_url(url):
            return
        print('⏳ 首次打开失败,5 秒后重试一次')
        t2 = threading.Timer(5, _open_final)
        t2.daemon = True
        t2.start()

    t = threading.Timer(1.5, _open)
    t.daemon = True  # 非守护线程(尤其弹窗未关闭时)会卡住进程退出
    t.start()

def setup_chromedriver_fallback():
    """打包版兜底:exe 同目录放置 chromedriver(.exe) 时直接使用,避免依赖联网下载"""
    if os.environ.get('CHROMEDRIVER_BIN'):
        return
    if not IS_FROZEN:
        return
    cand = os.path.join(_exe_dir(), 'chromedriver' + ('.exe' if sys.platform == 'win32' else ''))
    if os.path.exists(cand):
        os.environ['CHROMEDRIVER_BIN'] = cand
        print('ℹ️ 检测到同目录 chromedriver,已启用')

def main():
    if not acquire_single_instance():
        return
    # 无 dev 模式:未配置授权公钥直接拒绝启动
    if not _license_config_ok():
        print('❌ 未配置授权公钥!请先运行「卡密生成器.py init」,')
        print('   把打印的 LICENSE_PUBLIC_KEY_B64 粘贴到后端文件后重新启动/打包。')
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, '程序未配置授权公钥,请先运行「卡密生成器.py init」并将公钥粘贴到后端文件后重新打包。', '抖音火花助手', 0x10)
            except Exception:
                pass
        sys.exit(1)
    setup_chromedriver_fallback()
    init_db()
    reconcile_markers()
    start_license_guard()
    start_heartbeat()
    restore_tasks_from_db()
    start_scheduler()
    # 端口:环境变量 > 固定 9844(被占用则顺延)
    port = int(os.environ['PORT']) if os.environ.get('PORT') else pick_port(9844)
    # 监听地址:打包版仅本机(顺带避开 Win10 防火墙授权弹窗);
    # dev 默认 0.0.0.0(兼容 Docker 容器前端经 host.docker.internal 访问)
    host = os.environ.get('HOST') or ('127.0.0.1' if IS_FROZEN else '0.0.0.0')
    setup_static()
    print(f'🚀 抖音火花助手启动: http://127.0.0.1:{port} (监听 {host})')
    if os.environ.get('SPARK_NO_OPEN') != '1':
        open_browser_later(port)
    uvicorn.run(app, host=host, port=port, reload=False)

def _run():
    """顶层异常兜底:打包版控制台是隐藏的,不落盘的话启动崩溃将完全无痕"""
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        print('❌ 启动失败:\n' + tb)
        if not _log_teed:  # 日志没接上时补写一次,保证 traceback 一定落盘
            try:
                with open(LOG_PATH, 'a', encoding='utf-8', errors='replace') as f:
                    f.write(f'\n===== 启动失败 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{tb}')
            except Exception:
                pass
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, f'程序启动失败:\n\n{tb[-700:]}\n\n完整日志:\n{LOG_PATH}', '抖音火花助手', 0x10)
            except Exception:
                pass
        sys.exit(1)

if __name__ == "__main__":
    _run()
