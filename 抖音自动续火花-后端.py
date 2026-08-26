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
import io
import platform
import zipfile
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
import atexit
import urllib.parse
import webbrowser
from datetime import datetime, timezone, timedelta

import requests
import schedule
import uvicorn
from selenium import webdriver
from selenium.webdriver import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
# selenium 4.x 的 webdriver.ChromeOptions / webdriver.Chrome 走 __getattr__ 懒加载,
# Nuitka 静态分析看不到,冻结 exe 会报 No module named 'selenium.webdriver.chrome.options'。
# 显式导入保证打包时带上(仅用于打包可见性,运行时仍用 webdriver.ChromeOptions)
from selenium.webdriver.chrome.options import Options as _ChromeOptions
from selenium.webdriver.chrome.webdriver import WebDriver as _ChromeWebDriver
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

def _cached_chromedriver_path() -> str:
    """本程序自己下载存放的驱动(APP_DIR/chromedriver/),跨重启复用"""
    exe = 'chromedriver' + ('.exe' if sys.platform == 'win32' else '')
    cand = os.path.join(APP_DIR, 'chromedriver', exe)
    return cand if os.path.exists(cand) else ''

def _local_chrome_version(chrome_bin: str):
    """chrome --version → (major, minor, build, patch),失败返回 None"""
    flags = 0x08000000 if sys.platform == 'win32' else 0  # CREATE_NO_WINDOW
    try:
        out = subprocess.run([chrome_bin, '--version'], capture_output=True, text=True,
                             timeout=20, creationflags=flags)
        m = re.search(r'(\d+)\.(\d+)\.(\d+)\.(\d+)', (out.stdout or '') + (out.stderr or ''))
        return tuple(map(int, m.groups())) if m else None
    except Exception as e:
        print(f'⚠️ 获取本机 Chrome 版本失败: {e}')
        return None

def _pick_cft_version(versions: list, chrome_ver: tuple):
    """从 Chrome for Testing 版本列表挑最匹配本机 Chrome 的:精确匹配 > 同 major 最新 > 全局最新"""
    parsed = []
    for v in versions:
        m = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)\.(\d+)', v.get('version', ''))
        if m:
            parsed.append((tuple(map(int, m.groups())), v))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    for k, v in parsed:
        if k == chrome_ver:
            return v
    same_major = [v for k, v in parsed if k[0] == chrome_ver[0]]
    if same_major:
        return same_major[-1]
    return parsed[-1][1]

def download_chromedriver_from_mirror() -> str:
    """兜底:Selenium Manager 离线无缓存且联网失败时(国内网络访问不了 Google),
    从 npmmirror 的 Chrome for Testing 镜像下载与本机 Chrome 匹配的 chromedriver
    到 APP_DIR/chromedriver/。成功返回可执行文件路径,失败返回空字符串(不抛异常)。"""
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        return ''
    ver = _local_chrome_version(chrome_bin)
    if not ver:
        return ''
    if sys.platform == 'win32':
        plat = 'win64'
    elif sys.platform == 'darwin':
        plat = 'mac-arm64' if platform.machine() == 'arm64' else 'mac-x64'
    else:
        plat = 'linux64'
    try:
        print(f'📥 从 npmmirror 镜像获取 chromedriver(本机 Chrome {".".join(map(str, ver))})…')
        r = requests.get('https://registry.npmmirror.com/-/binary/chrome-for-testing/'
                         'known-good-versions-with-downloads.json', timeout=30)
        r.raise_for_status()
        chosen = _pick_cft_version(r.json().get('versions', []), ver)
        if not chosen:
            return ''
        url = ''
        for p in chosen.get('downloads', {}).get('chromedriver', []):
            if p.get('platform') == plat:
                url = p.get('url', '').replace(
                    'https://storage.googleapis.com/chrome-for-testing-public/',
                    'https://cdn.npmmirror.com/binaries/chrome-for-testing/')
                break
        if not url:
            return ''
        print(f'📥 匹配版本 {chosen["version"]},下载 {url}')
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        exe = 'chromedriver' + ('.exe' if sys.platform == 'win32' else '')
        dest_dir = os.path.join(APP_DIR, 'chromedriver')
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(dest_dir)
        # zip 内有顶层目录(如 chromedriver-win64/),把可执行文件翻到上层
        for root, _dirs, files in os.walk(dest_dir):
            for fn in files:
                if fn == exe:
                    src = os.path.join(root, fn)
                    dst = os.path.join(dest_dir, fn)
                    if src != dst:
                        shutil.move(src, dst)
                    if sys.platform != 'win32':
                        os.chmod(dst, 0o755)
                    print(f'✅ chromedriver 已就绪: {dst}')
                    return dst
        return ''
    except Exception as e:
        print(f'⚠️ npmmirror 获取 chromedriver 失败: {e}')
        return ''

def create_driver():
    """创建浏览器实例,驱动获取顺序:
    1) CHROMEDRIVER_BIN 指定 → 直接使用(打包版 exe 同目录 chromedriver.exe 兜底)
    2) APP_DIR/chromedriver 里已有的驱动(镜像下载过的,跨重启复用)
    3) Selenium Manager 先离线(用缓存),失败再联网
    4) 还不行 → npmmirror 镜像下载匹配本机 Chrome 的驱动
    5) 全失败 → 给出指引性报错"""
    options = build_options()
    explicit_bin = os.environ.get('CHROMEDRIVER_BIN', '')
    driver_bin = explicit_bin or _cached_chromedriver_path()
    if driver_bin:
        try:
            return webdriver.Chrome(service=ChromeService(executable_path=driver_bin), options=options)
        except Exception:
            if explicit_bin:
                raise  # 用户显式指定的驱动:错误透出(Init 接口有版本提示)
            # 自动缓存的驱动已失效(Chrome 升级等):降级走自动获取
            print('⚠️ 本地缓存驱动已失效,改用自动获取')
    os.environ['SE_OFFLINE'] = 'true'
    try:
        return webdriver.Chrome(options=options)
    except Exception as e1:
        print(f'⚠️ Selenium Manager 离线无缓存: {str(e1)[:120]}')
        os.environ.pop('SE_OFFLINE', None)  # 删掉才是确定的"在线",设 'false' 字符串语义有歧义
        try:
            return webdriver.Chrome(options=options)
        except Exception as e2:
            print(f'⚠️ Selenium Manager 联网获取失败: {str(e2)[:120]}')
    driver_bin = download_chromedriver_from_mirror()
    if driver_bin:
        os.environ['CHROMEDRIVER_BIN'] = driver_bin
        return webdriver.Chrome(service=ChromeService(executable_path=driver_bin), options=options)
    raise RuntimeError('自动获取 chromedriver 失败:请将与本机 Chrome 版本匹配的 '
                       'chromedriver.exe 放到程序同目录后重试')


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
def AiqingGongyu_text() -> str:
    """今日名言(空消息时的默认文案),网络失败时返回兜底文本"""
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

# 会话过期状态:心跳确认"真掉线"后置 True,前端提示"会话已过期,请重新登录";
# 重新登录成功/主动退出登录时复位
LOGIN_EXPIRED = False
LOGIN_EXPIRED_REASON = ''

def _kill_profile_orphans(profile_dir: str):
    """杀掉此前异常退出遗留的 Chrome 进程。仍占着同一 --user-data-dir 的旧进程
    会让新会话直接退出('Chrome instance exited'),所以创建会话前先清理。
    mac/Windows 各自按命令行过滤,只杀带本项目 profile 参数的进程,不碰用户正常浏览器"""
    if not profile_dir:
        return
    try:
        if sys.platform == 'win32':
            ps = (f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*--user-data-dir={profile_dir}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
            subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                           capture_output=True, timeout=20)
        else:
            # 注意:模式不能带前导 '--',否则被 pkill 当作选项解析而失效
            subprocess.run(['pkill', '-f', f'user-data-dir={profile_dir}'],
                           capture_output=True, timeout=20)
        time.sleep(1)  # 等进程释放 profile 锁
    except Exception as e:
        print(f'⚠️ 清理遗留 Chrome 进程失败: {e}')

def _quit_driver_atexit():
    """进程退出前关闭浏览器会话,防止遗留孤儿 Chrome 占用 profile"""
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass

atexit.register(_quit_driver_atexit)

def restart_driver():
    global driver, douyin, init, Login_is_bool
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    # 异常退出遗留的 Chrome 仍占着 profile 会让新会话直接退出,先清理
    _kill_profile_orphans(PROFILE_DIR)
    if not _find_chrome_binary():
        if sys.platform == 'win32':
            raise RuntimeError('未找到本机安装的 Google Chrome,请先安装 Chrome,或用 CHROME_BIN 指定路径')
        raise RuntimeError('未找到本机安装的 Google Chrome,请确认 /Applications/Google Chrome.app 存在,或用 CHROME_BIN 指定路径')
    try:
        driver = create_driver()
    except SessionNotCreatedException:
        # 'Chrome instance exited' 常见于旧进程尚未释放 profile,再清一次并重试
        _kill_profile_orphans(PROFILE_DIR)
        time.sleep(2)
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

def _login_panel_visible() -> bool:
    """登录弹窗是否可见。扫码成功后抖音常把弹窗隐藏而非从 DOM 移除,
    只看节点存在会把"已登录"误判成"还在等扫码" —— 必须按可见性判断"""
    try:
        els = driver.find_elements(By.XPATH, LOGIN_PANEL_XPATH)
        return any(e.is_displayed() for e in els)
    except Exception:
        return True  # 异常时按"弹窗还在"处理,避免误判已登录

def ensure_login_dialog():
    """确保抖音登录弹窗可见；页面状态过期(弹窗关闭/被风控页替代)时重载聊天页"""
    for _attempt in range(3):
        try:
            if _login_panel_visible():
                return True
        except Exception:
            pass
        try:
            driver.get(DOUYIN_CHAT)
        except Exception:
            pass
        time.sleep(6)
    return _login_panel_visible()

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

def _scheduled_send(db_id, name, text, slot_id='default', attempt=0):
    """定时任务发送:失败 60s 后自动重试(最多 3 次),全程落 task_logs;暂停期间跳过并记录待补发。
    任务按账号归属:slot_id 为其他抖音账号时,当前账号登录态下不执行"""
    run_time = time.strftime('%H:%M')
    cur_uid = _current_douyin_id()
    # 该任务属于其他账号 → 不执行(任务保留,切回原账号后恢复执行)
    if slot_id not in ('', 'default', cur_uid):
        db_log_task(db_id, name, run_time, 'skipped', '该任务属于其他抖音账号,当前账号下不执行')
        return
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
        j = schedule.every(RETRY_DELAY_SECONDS).seconds.do(_scheduled_send, db_id, name, text, slot_id, attempt + 1)
        j.spark_retry_of = db_id
    else:
        print(f'❌ 定时任务最终失败 → {name}: {err_msg}')

def _daily_task_job(db_id, name, text, slot_id):
    """每日定时任务入口"""
    _scheduled_send(db_id, name, text, slot_id, 0)

def register_task_job(db_id, name, text, play_time, slot_id):
    """注册每日定时任务(新增与启动恢复共用)"""
    return schedule.every().day.at(play_time).do(_daily_task_job, db_id, name, text, slot_id)

def _stamp_legacy_tasks():
    """旧任务(slot_id='default')按当前登录账号盖章归属:程序升级前创建的任务
    归到当下登录的抖音账号,此后切换账号不会再误执行"""
    uid = _current_douyin_id()
    if not uid:
        return
    try:
        with get_conn() as conn:
            cur = conn.execute("UPDATE tasks SET slot_id=? WHERE slot_id IN ('', 'default')", (uid,))
            conn.commit()
            if cur.rowcount:
                print(f'🏷️ 已将 {cur.rowcount} 个旧任务归属到当前抖音账号')
    except Exception:
        pass

def restore_tasks_from_db():
    """服务启动时从 SQLite 恢复全部定时任务(各账号任务都注册,执行时按账号过滤)"""
    _stamp_legacy_tasks()
    rows = db_load_tasks()
    for row in rows:
        sid = row.get('slot_id') or 'default'
        bucket = scheduled_tasks.setdefault(sid, {})
        task_id = f"{row['run_time']}_{row['friend_name']}"
        if task_id in bucket:
            continue
        job = register_task_job(row['id'], row['friend_name'], row['message'], row['run_time'], sid)
        bucket[task_id] = {'job': job, 'db_id': row['id']}
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
        'license_account_mismatch': '当前抖音账号未激活授权,请为该账号激活卡密,或换回已激活的抖音账号',
    }
    return {'code': 403, 'data': _map.get(err['data'], err['data'])}

# 定时任务存储
scheduled_tasks = {}  # 格式: {账号slot_id: {任务ID: {'job': job对象, 'db_id': 数据库id}}},按抖音账号分桶


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
    slot_id TEXT NOT NULL DEFAULT 'default',
    friend_name TEXT NOT NULL,
    run_time TEXT NOT NULL,
    message TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(slot_id, friend_name)
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
    """表结构缺失时自动重建(防删库后新建空库导致静默失败);旧库自动迁移:
    补 hours/douyin_id 列、tasks.slot_id 列(任务按抖音账号归属)"""
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
            # 任务按账号归属:旧库补 slot_id 列(旧任务归 'default',启动时按当前登录账号盖章归属)
            tcols = [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]
            if 'slot_id' not in tcols:
                conn.execute("ALTER TABLE tasks ADD COLUMN slot_id TEXT NOT NULL DEFAULT 'default'")
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

def db_insert_task(friend_name, run_time, message, slot_id='default'):
    with get_conn() as conn:
        cur = conn.execute(
            'INSERT INTO tasks(slot_id, friend_name, run_time, message, enabled, created_at) VALUES (?,?,?,?,1,?)',
            (slot_id, friend_name, run_time, message, _utc_iso()))
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

def _load_activation(douyin_id=''):
    """读取激活记录:douyin_id='' 取机器卡行;非空取该抖音标识的账号卡行。
    多行共存:换号激活新卡不会覆盖旧账号的激活记录,时长照常扣减"""
    try:
        with get_conn() as conn:
            row = conn.execute('SELECT * FROM license_activation WHERE douyin_id=? ORDER BY id DESC LIMIT 1',
                               (douyin_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def _load_latest_activation_any():
    """取最新一行激活记录(不区分机器卡/账号卡,未登录等场景兜底用)"""
    try:
        with get_conn() as conn:
            row = conn.execute('SELECT * FROM license_activation ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def _activation_error(act):
    """单条激活记录的行内校验(不含账号匹配,由读取处处理):有效返回 None"""
    if detect_clock_rollback(act):
        return {'code': 403, 'data': 'license_rollback'}
    expires = _parse_iso(act.get('expires_at'))
    if expires and _now_utc() >= expires:
        return {'code': 403, 'data': 'license_expired'}
    return None

def _current_douyin_id() -> str:
    return (db_get_meta('douyin_user_id') or '').strip()

def _effective_activation():
    """当前登录账号应生效的激活记录:(机器卡 > 当前账号卡)。返回 (记录, 错误字典|None)
    兼容旧库单行记录:无机器卡且无当前账号卡时,回落到最新一行按旧规则校验"""
    machine_act = _load_activation('')
    if machine_act:
        return machine_act, _activation_error(machine_act)
    cur_uid = _current_douyin_id()
    if cur_uid:
        act = _load_activation(cur_uid)
        if act:
            return act, _activation_error(act)
        # 有绑定其他账号的卡,但当前账号没有 → 账号不匹配(提示换回或重新激活)
        if _load_latest_activation_any():
            return None, {'code': 403, 'data': 'license_account_mismatch'}
    # 旧库兼容:单行记录(可能绑 uid),未登录时无法校验账号匹配,登录即生效
    act = _load_latest_activation_any()
    if act:
        bound_uid = (act.get('douyin_id') or '').strip()
        if bound_uid and cur_uid and cur_uid != bound_uid:
            return act, {'code': 403, 'data': 'license_account_mismatch'}
        return act, _activation_error(act)
    return None, {'code': 403, 'data': 'license_not_activated'}

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
    """授权状态(无鉴权接口用)。按当前登录账号展示:
    机器卡 > 当前账号卡;时长按墙钟照扣,过期显示 expired"""
    mc = get_machine_code()
    if not _license_config_ok():
        return {'status': 'config_missing', 'machine_code': mc, 'activated_at': None,
                'expires_at': None, 'days_left': None, 'expires_in_seconds': None, 'now': _utc_iso()}
    act, err = _effective_activation()
    if not act:
        # 兼容旧命名(前端 license store 依赖):license_* → 旧状态名。
        # 账号不匹配(当前账号没有卡)对外展示为"未激活" —— 按账号激活模型下这就是没激活
        _legacy = {'license_expired': 'expired', 'license_rollback': 'rollback',
                   'license_account_mismatch': 'none', 'license_locked': 'locked',
                   'license_not_activated': 'none', 'license_config_missing': 'config_missing'}
        status = err['data'] if err else 'none'
        status = _legacy.get(status, status)
        return {'status': status, 'machine_code': mc, 'activated_at': None,
                'expires_at': None, 'days_left': None, 'expires_in_seconds': None, 'now': _utc_iso()}
    now = _now_utc()
    expires = _parse_iso(act.get('expires_at'))
    days_left = None
    expires_in_seconds = None
    if expires:
        expires_in_seconds = max(0, int((expires - now).total_seconds()))
        days_left = max(0, math.ceil(expires_in_seconds / 86400))
    bound_uid = (act.get('douyin_id') or '').strip()
    if err:
        status = err['data']
    elif LICENSE_LOCKED:
        status = 'locked'
    else:
        status = act.get('status') or 'active'
    # 兼容旧命名(前端 license store 依赖)
    _legacy = {'license_expired': 'expired', 'license_rollback': 'rollback',
               'license_account_mismatch': 'account_mismatch', 'license_locked': 'locked'}
    status = _legacy.get(status, status)
    return {'status': status, 'machine_code': mc, 'activated_at': act.get('activated_at'),
            'expires_at': act.get('expires_at'), 'days_left': days_left,
            'expires_in_seconds': expires_in_seconds, 'bound_douyin_id': bound_uid, 'now': _utc_iso()}

def check_license():
    """授权校验:有效返回 None,否则返回 {code, data} 错误字典(接口可直接返回)。
    机器卡有效 > 当前账号卡有效;时长照扣,到期返回 license_expired"""
    if not _license_config_ok():
        return {'code': 403, 'data': 'license_config_missing'}
    if LICENSE_LOCKED:
        return {'code': 403, 'data': 'license_locked'}
    act, err = _effective_activation()
    if err:
        return err
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
            cur_uid = _remember_douyin_identity(resolve_current_douyin_account())
        except Exception:
            cur_uid = _current_douyin_id()
        if not cur_uid:
            raise LicenseError(400, '无法读取当前抖音账号标识,请确认已登录抖音')
        if cur_uid != bound_uid:
            raise LicenseError(402, '当前登录的抖音账号与卡密绑定的账号不一致,请切换到绑定的抖音账号后再激活')
    now = _now_utc()
    expires = now + timedelta(days=payload['days'], hours=payload.get('hours', 0))
    activated_at = _utc_iso(now)
    expires_at = _utc_iso(expires)
    with get_conn() as conn:
        # 多行模型:只替换同类行 —— 机器卡(douyin_id='')只留一张;账号卡按绑定标识各留一张,
        # 换号激活新卡不会覆盖其他账号的激活记录(时长照常从各自激活时刻扣减)
        conn.execute('DELETE FROM license_activation WHERE douyin_id=?', (bound_uid,))
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
            # 只有机器卡过期/回拨才硬锁(整机授权失效);账号卡到期/回拨仅显示状态
            # 并让该账号的任务跳过执行(check_license 逐条判定),切换回其他账号不受影响
            machine_act = _load_activation('')
            if machine_act:
                err = _activation_error(machine_act)
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
    """轻量 HTTP 探测,返回 (登录态, 账号身份字典, 原因说明):
    登录态 True=已登录 False=已掉线 None=无法判定(网络异常/页面结构变化,本次不判定)
    判定依据:聊天页 SSR 数据 RENDER_DATA 中的 app.user.isLogin 布尔值(实测权威字段)"""
    if not cookies:
        return (None, None, '无 cookie')
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
        return (None, None, '网络异常')
    data = _parse_render_data(r.text)
    if data:
        try:
            user = data.get('app', {}).get('user', {})
            if isinstance(user, dict) and 'isLogin' in user:
                info = _account_from_render_data(data)
                return (info['logged_in'], info,
                        '' if info['logged_in'] else 'RENDER_DATA isLogin=false')
        except Exception:
            pass
    # 兜底:被踢到 passport 登录域 → 未登录
    if 'passport.douyin.com' in r.url:
        return (False, None, 'passport 重定向')
    return (None, None, '页面结构异常')  # 页面结构异常时不判定,避免误伤

def _driver_logged_in() -> bool:
    """浏览器会话仍在且带抖音登录 cookie 时返回 True。实测比 HTTP 探测更权威:
    风控/网络差异下 HTTP 探测可能假掉线(用户明明没退出登录),用它拦截误判"""
    global init, driver
    if not init or driver is None:
        return False
    try:
        driver.execute_script('return 1')
        return any(c.get('name') == 'sessionid_ss' for c in driver.get_cookies())
    except Exception:
        return False

def _heartbeat_loop():
    global Login_is_bool
    last_login_state = None  # 上一次判定,只在状态翻转时打日志,避免每分钟刷屏
    while True:
        try:
            # 授权异常时不探测(授权锁已暂停任务)
            if check_license():
                time.sleep(HEARTBEAT_INTERVAL)
                continue
            cookies = db_load_cookies()
            if cookies:
                result, probed, detail = probe_login(cookies)
                heartbeat_state['last_probe'] = _utc_iso()
                if result is True:
                    heartbeat_state['last_ok'] = _utc_iso()
                    Login_is_bool = True
                    LOGIN_EXPIRED = False
                    LOGIN_EXPIRED_REASON = ''
                    _remember_douyin_identity(probed)  # 心跳顺带刷新账号标识缓存
                    if db_get_meta('tasks_paused') == '1':
                        resume_all_tasks()
                        print('💓 心跳:抖音已恢复登录,任务已恢复')
                    last_login_state = True
                elif result is False:
                    if _driver_logged_in():
                        # 浏览器会话仍有效:HTTP 探测多半是误判,不暂停任务
                        if last_login_state is not False:
                            print(f'⚠️ 心跳:HTTP 探测判定掉线({detail}),但浏览器会话仍有效,不暂停任务')
                        last_login_state = False
                    else:
                        heartbeat_state['last_ok'] = None
                        Login_is_bool = False
                        LOGIN_EXPIRED = True
                        LOGIN_EXPIRED_REASON = detail
                        if db_get_meta('tasks_paused') != '1':
                            pause_tasks(f'心跳检测到抖音登录已失效({detail})')
                        if last_login_state is not False:
                            print(f'💔 心跳:抖音登录已失效({detail}),任务已暂停(请重新登录)')
                        last_login_state = False
                else:
                    last_login_state = None
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)

def start_heartbeat():
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

# ====== 抖音账号唯一标识提取 ======
# 【铁律】抖音标识必须取【账号级】字段 —— 同一个抖音账号,不管退出重登多少次、
# 换不换设备,取到的值都必须一模一样。RENDER_DATA 里可用的账号级字段(实测):
#   app.user.info.uid / app.odin.user_id   数字账号 ID(永久不变,首选)
#   app.user.info.secUid                   加密账号 ID(永久不变,uid 取不到时兜底)
#   app.user.info.uniqueId                 抖音号(用户可改,只做展示,不做标识)
# 绝对不能用 app.odin.user_unique_id —— 那是【设备/会话级】埋点 ID,重登即变。
# 历史 BUG:旧代码按 user.unique_id / user.uid / user.sec_uid 三个键取值,可真实结构
# 里它们都在 user.info 下,三个候选全部落空,于是一路兜底到 odin.user_unique_id,
# 造成"每次重新登录抖音标识都不一样",卡密绑定和任务归属随之全部错乱。
DOUYIN_ID_SCHEME = 'v2-account-uid'  # 标识方案版本(用于把库里遗留的设备级标识迁移一次)

def _parse_render_data(page_or_html: str):
    """解析页面 SSR 数据 RENDER_DATA(URL 编码的 JSON),返回 dict 或 None"""
    m = re.search(r'<script id="RENDER_DATA" type="application/json"[^>]*>([^<]+)</script>', page_or_html)
    if not m:
        return None
    try:
        return json.loads(urllib.parse.unquote(m.group(1)))
    except Exception:
        return None

def _numeric_uid(value) -> str:
    """账号数字 ID 归一化:未登录/占位时抖音会给 '0' 或空串,一律判无效"""
    s = str(value or '').strip()
    return s if s.isdigit() and s.strip('0') else ''

def _account_from_render_data(data) -> dict:
    """从 RENDER_DATA 解析账号身份。仅 isLogin 为真时字段可信 ——
    未登录时页面里那些 id 都是匿名轮换值,必须靠 isLogin 守卫住"""
    result = {'logged_in': False, 'douyin_id': '', 'nickname': '',
              'uid': '', 'sec_uid': '', 'unique_id': ''}
    if not isinstance(data, dict):
        return result
    app_data = data.get('app') if isinstance(data.get('app'), dict) else {}
    user = app_data.get('user') if isinstance(app_data.get('user'), dict) else {}
    odin = app_data.get('odin') if isinstance(app_data.get('odin'), dict) else {}
    is_login = user.get('isLogin')
    if isinstance(is_login, str):
        is_login = is_login.lower() in ('true', '1')
    result['logged_in'] = bool(is_login)
    if not result['logged_in']:
        return result
    info = user.get('info') if isinstance(user.get('info'), dict) else {}
    # 数字 uid:user.info.uid 与 odin.user_id 是同一个值,互为兜底(顺带兼容扁平结构)
    result['uid'] = (_numeric_uid(info.get('uid')) or _numeric_uid(user.get('uid'))
                     or _numeric_uid(odin.get('user_id')))
    result['sec_uid'] = str(info.get('secUid') or info.get('sec_uid')
                            or user.get('secUid') or user.get('sec_uid') or '').strip()
    result['unique_id'] = str(info.get('uniqueId') or info.get('unique_id')
                              or info.get('shortId') or info.get('short_id') or '').strip()
    result['nickname'] = str(info.get('nickname') or user.get('nickname') or '').strip()
    result['douyin_id'] = result['uid'] or result['sec_uid']  # 只认账号级 ID
    return result

def _extract_douyin_account(page_source: str) -> dict:
    """从浏览器当前页面的 SSR 数据(RENDER_DATA)提取抖音账号身份"""
    info = _account_from_render_data(_parse_render_data(page_source or ''))
    if info['logged_in'] and not info['nickname']:
        m2 = re.search(r'\\"nickname\\":\s*\\"([^\\"]+)\\"', page_source or '')
        if m2:
            info['nickname'] = m2.group(1)
    return info

def resolve_current_douyin_account() -> dict:
    """取当前登录账号身份:优先浏览器页面 SSR 数据;当前页没有(停在非抖音页或
    SPA 跳转后)则用 cookie 走一次轻量 HTTP 探测兜底,保证标识始终能取到"""
    try:
        info = _extract_douyin_account(driver.page_source)
    except Exception:
        info = {}
    if info.get('uid') or info.get('sec_uid'):
        return info
    try:
        cookies = driver.get_cookies()
    except Exception:
        cookies = db_load_cookies()
    try:
        logged_in, probed, _ = probe_login(cookies)
        if logged_in and probed:
            return probed
    except Exception:
        pass
    return info or {}

def _load_identity_record() -> dict:
    try:
        rec = json.loads(db_get_meta('douyin_identity') or '{}')
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}

def _remember_douyin_identity(info) -> str:
    """缓存账号身份并返回稳定标识。两条保证同一账号标识恒定的规则:
    ① 取不到账号级 ID 时保持旧值不动 —— 绝不用空值/设备值覆盖,否则又会一次一个样;
    ② uid 优先;只探到 sec_uid 且与缓存是同一账号时,沿用缓存里已有的标识,
       避免同一个账号在 uid / sec_uid 两套 ID 空间之间来回横跳"""
    if not isinstance(info, dict) or not info.get('logged_in'):
        return _current_douyin_id()
    uid, sec_uid = info.get('uid') or '', info.get('sec_uid') or ''
    if not uid and not sec_uid:
        return _current_douyin_id()
    rec = _load_identity_record()
    same_account = bool(rec) and ((uid and rec.get('uid') == uid)
                                  or (sec_uid and rec.get('sec_uid') == sec_uid))
    merged = dict(rec) if same_account else {}
    for key in ('uid', 'sec_uid', 'unique_id', 'nickname'):
        if info.get(key):
            merged[key] = info[key]
    stable = (merged.get('douyin_id') if same_account else '') or uid or sec_uid
    merged['douyin_id'] = stable
    db_set_meta('douyin_identity', json.dumps(merged, ensure_ascii=False))
    if stable != _current_douyin_id():
        _migrate_legacy_douyin_ids(stable)  # 仅升级后首次生效,之后换号就是真的换号
        db_set_meta('douyin_user_id', stable)
    return stable

def _migrate_legacy_douyin_ids(new_uid: str):
    """一次性迁移:老版本把设备级 odin.user_unique_id 当成了抖音标识,每次重登都是新值,
    库里遗留的任务归属(tasks.slot_id)和卡密绑定(license_activation.douyin_id)全是废值。
    升级后首次拿到真实账号 ID 时统一改写过来,否则用户原有定时任务会被当成
    "别的账号的任务"跳过、已激活的卡密会被判成"账号不匹配"而锁死。"""
    if not new_uid or db_get_meta('douyin_id_scheme') == DOUYIN_ID_SCHEME:
        return
    try:
        with get_conn() as conn:
            cur = conn.execute("UPDATE OR REPLACE tasks SET slot_id=? "
                               "WHERE slot_id NOT IN ('', 'default', ?)", (new_uid, new_uid))
            tasks_moved = cur.rowcount or 0
            cur = conn.execute("UPDATE license_activation SET douyin_id=? "
                               "WHERE douyin_id NOT IN ('', ?)", (new_uid, new_uid))
            cards_moved = cur.rowcount or 0
            # 多张遗留标识的卡被迁到同一账号名下时,只保留到期最晚的一行
            conn.execute("DELETE FROM license_activation WHERE douyin_id=? AND id NOT IN "
                         "(SELECT id FROM license_activation WHERE douyin_id=? "
                         " ORDER BY expires_at DESC, id DESC LIMIT 1)", (new_uid, new_uid))
            conn.commit()
    except Exception as e:
        print(f'⚠️ 抖音标识迁移失败(不影响继续使用): {str(e)[:120]}')
        return
    db_set_meta('douyin_id_scheme', DOUYIN_ID_SCHEME)
    try:  # 标记文件里的绑定标识同步改写,免得 reconcile_markers 又把旧废值写回库
        marker = read_license_markers()
        if marker and (marker.get('douyin_id') or '') not in ('', new_uid):
            write_license_markers(marker.get('card_ids') or [], marker.get('machine_code') or '',
                                  marker.get('expires_at') or '', new_uid)
    except Exception:
        pass
    if tasks_moved or cards_moved:
        print(f'🔧 抖音标识已升级为账号级 ID({new_uid}):'
              f'迁移定时任务 {tasks_moved} 个、激活记录 {cards_moved} 条')
        _reload_task_schedule()

def _reload_task_schedule():
    """按数据库里的最新归属重建定时任务:标识迁移后内存里的 job 还带着旧 slot_id,
    不重建会被 _scheduled_send 判成"别的账号的任务"而跳过不执行"""
    try:
        for bucket in list(scheduled_tasks.values()):
            for meta in list(bucket.values()):
                try:
                    schedule.cancel_job(meta['job'])
                except Exception:
                    pass
        scheduled_tasks.clear()
        restore_tasks_from_db()
    except Exception:
        pass

def resolve_identity_on_startup():
    """启动时用已存的 cookie 轻量探一次账号身份,让标识迁移赶在定时任务注册之前完成。
    网络不通就跳过(不阻塞启动),等心跳或下次登录时再补"""
    try:
        cookies = db_load_cookies()
        if cookies:
            logged_in, probed, _ = probe_login(cookies)
            if logged_in and probed:
                _remember_douyin_identity(probed)
    except Exception:
        pass

def _cache_douyin_user_id():
    """登录成功后提取并缓存当前抖音账号标识(卡密账号绑定校验、任务归属都依赖它)"""
    try:
        _remember_douyin_identity(resolve_current_douyin_account())
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
        'login_state': 'in' if Login_is_bool else ('expired' if LOGIN_EXPIRED else 'out'),
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
        panel_exists = _login_panel_visible()
    except Exception:
        panel_exists = True
    cooke = driver.get_cookies()
    has_session = any(c.get('name') == 'sessionid_ss' for c in cooke)
    if not panel_exists and has_session:
        Login_is_bool = True
        LOGIN_EXPIRED = False
        LOGIN_EXPIRED_REASON = ''
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
    # login_state 三态:in=已登录 / out=未登录 / expired=会话已过期(前端提示重新登录)
    state = 'in' if Login_is_bool else ('expired' if LOGIN_EXPIRED else 'out')
    return {'code': 200, 'data': 'Yes' if Login_is_bool else 'No',
            'login_state': state, 'expired_reason': LOGIN_EXPIRED_REASON}

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
        # 先尝试直接读二维码 img(正常状态)
        try:
            login_src = driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]//img').get_attribute('src')
        except NoSuchElementException:
            login_src = None
        if login_src:
            return {'code': 200, 'data': login_src}
        # 二维码已扫描/失效:点击旧版刷新层,或带"重新获取/点击刷新"文案的任意元素
        # (抖音改版后旧选择器 div[2]/div 已不存在,文案点击更稳)
        try:
            driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]/div[2]/div').click()
        except Exception:
            try:
                driver.find_element(By.XPATH,
                    '//*[@id="animate_qrcode_container"]//*[contains(text(),"重新获取") '
                    'or contains(text(),"点击刷新") or contains(text(),"刷新")]').click()
            except Exception:
                pass
        time.sleep(3)
    # 兜底:整页重载,登录弹窗会重新渲染出全新二维码
    try:
        driver.get(DOUYIN_CHAT)
        time.sleep(6)
        login_src = driver.find_element(By.XPATH, '//*[@id="animate_qrcode_container"]//img').get_attribute('src')
        if login_src:
            return {'code': 200, 'data': login_src}
    except Exception:
        pass
    return {'code': 404, 'data': 'cant find LoginPng src attribute'}

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
        info = resolve_current_douyin_account()
        stable = _remember_douyin_identity(info)  # 刷新账号标识缓存(只认账号级 ID)
        rec = _load_identity_record()
        return {'code': 200, 'data': {
            'logged_in': bool(info.get('logged_in')) or Login_is_bool,
            'douyin_id': stable or '',
            'nickname': info.get('nickname') or rec.get('nickname') or '',
            'uid': info.get('uid') or rec.get('uid') or '',
            'unique_id': info.get('unique_id') or rec.get('unique_id') or ''}}
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
    global Login_is_bool, LOGIN_EXPIRED, LOGIN_EXPIRED_REASON
    # 彻底登出:只清 cookie 不够 —— 抖音网页端把会话也存在 localStorage/IndexedDB,
    # 页面一重载又自动登回来(表现就是"强制退出后仍检测到历史账号")
    try:
        driver.execute_script(
            "localStorage.clear(); sessionStorage.clear();"
            "try { indexedDB.databases().then(function(dbs){ dbs.forEach(function(d){ indexedDB.deleteDatabase(d.name); }); }); } catch(e) {}"
        )
    except Exception as e:
        print(f'⚠️ 清理浏览器本地存储失败: {e}')
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    db_save_cookies([])  # 同步清空落库 cookie,防止心跳探测按旧 cookie 判登录
    Login_is_bool = False
    LOGIN_EXPIRED = False
    LOGIN_EXPIRED_REASON = ''
    # 重载页面让登录弹窗出现,方便立即重新扫码
    try:
        driver.get(DOUYIN_CHAT)
    except Exception:
        pass
    return {'code': 200, 'data': '已彻底退出登录'}

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
        panel_exists = _login_panel_visible()
        has_session = any(c.get('name') == 'sessionid_ss' for c in driver.get_cookies())
        if not panel_exists and has_session:
            Login_is_bool = True
            LOGIN_EXPIRED = False
            LOGIN_EXPIRED_REASON = ''
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

def _current_task_buckets() -> list:
    """当前登录账号可见的任务桶:本账号 + default(未归属的旧任务)。
    返回 [(slot_id, bucket_dict), ...]"""
    uid = _current_douyin_id()
    out = []
    for sid in (uid, 'default'):
        if sid:
            out.append((sid, scheduled_tasks.setdefault(sid, {})))
    return out

@with_driver_recovery
def _add_time_with_driver(time: str, name: str, text: str = None):
    if not name:
        return {'code': 400, 'data': 'name 不能为空'}
    slot = _current_douyin_id() or 'default'
    bucket = scheduled_tasks.setdefault(slot, {})
    # 检查该账号下是否已存在此好友的定时任务
    for task_id in bucket:
        if task_id.endswith(f"_{name}"):
            return {'code': 400, 'data': f'好友 {name} 已有定时任务，请先删除或修改'}

    temp = douyin.Find_Friends(name)
    if temp.is_bool:
        play_time = format_time(time)
        msg = AiqingGongyu_text() if text in (None, '') else text
        # 先写数据库(重启后自动恢复),任务归属当前抖音账号
        db_id = db_insert_task(name, play_time, msg, slot)
        job = register_task_job(db_id, name, msg, play_time, slot)
        # 生成唯一任务ID
        task_id = f"{play_time}_{name}"
        bucket[task_id] = {'job': job, 'db_id': db_id}
        return {'code': 200, 'data': f'已添加定时任务: {play_time}', 'task_id': task_id}
    else:
        return {'code': 404, 'data': temp.string}

@app.get('/Time/del')
def del_time(task_id: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """根据任务ID删除定时任务(仅当前账号可见的任务)"""
    for sid, bucket in _current_task_buckets():
        if task_id in bucket:
            info = bucket[task_id]
            schedule.cancel_job(info['job'])
            _cancel_retry_jobs(info['db_id'])
            db_delete_task(info['db_id'])
            del bucket[task_id]
            return {'code': 200, 'data': f'已删除任务: {task_id}'}
    return {'code': 404, 'data': '任务ID不存在'}

@app.get('/Time/edit')
def edit_time(name: str, new_time: str, authorization: str = Header(None)):
    auth_err = require_auth(authorization)
    if auth_err:
        return auth_err
    """修改指定好友的定时任务时间(仅当前账号可见的任务)"""
    # 查找该好友的现有任务
    old_slot, old_task_id = None, None
    for sid, bucket in _current_task_buckets():
        for task_id in bucket:
            if task_id.endswith(f"_{name}"):
                old_slot, old_task_id = sid, task_id
                break
        if old_task_id:
            break

    if not old_task_id:
        return {'code': 404, 'data': f'好友 {name} 没有定时任务'}

    bucket = scheduled_tasks.setdefault(old_slot, {})
    # 取消旧任务
    old_info = bucket[old_task_id]
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
    new_job = register_task_job(old_db_id, name, msg, new_play_time, old_slot)

    # 生成新任务ID并替换
    new_task_id = f"{new_play_time}_{name}"
    bucket[new_task_id] = {'job': new_job, 'db_id': old_db_id}
    del bucket[old_task_id]

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
    """获取当前账号的定时任务列表(其他账号的任务保留但不在本账号展示)"""
    tasks = []
    paused = db_get_meta('tasks_paused') == '1'
    for sid, bucket in _current_task_buckets():
        for task_id, info in bucket.items():
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
    """打包版:后端托管前端构建产物(SPA fallback);未知 API 路径返回 404 JSON。
    index.html 强制 no-cache:升级 exe 后浏览器自动加载新前端,
    不会继续用旧缓存的 JS 去调已删除的接口(如旧版 /Api/Accounts 报 404)"""
    if not os.path.isdir(STATIC_DIR):
        return
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, JSONResponse
    app.mount('/assets', StaticFiles(directory=os.path.join(STATIC_DIR, 'assets')), name='assets')

    NO_CACHE = {'Cache-Control': 'no-cache, no-store, must-revalidate'}

    @app.get('/{full_path:path}')
    def spa_fallback(request: Request, full_path: str):
        original = request.scope.get('spark_original_path', '')
        if original.startswith('/api/'):
            return JSONResponse({'code': 404, 'data': '接口不存在'}, status_code=404)
        target = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(target):
            return FileResponse(target)
        return FileResponse(os.path.join(STATIC_DIR, 'index.html'), headers=NO_CACHE)

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

def _browser_exe_candidates() -> list:
    """候选浏览器可执行文件:Chrome(本程序本来就依赖本机 Chrome)> Edge。
    os.startfile/webbrowser.open 走"默认浏览器"关联,Win10 上关联损坏/被篡改时
    会返回成功但什么都不弹;直接拉起具体 exe 不依赖关联,是冻结 exe 里最可靠的打开方式。"""
    cands = []
    chrome = _find_chrome_binary()
    if chrome:
        cands.append(chrome)
    if sys.platform == 'win32':
        pf = os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')
        pf64 = os.environ.get('PROGRAMFILES', r'C:\Program Files')
        cands += [
            os.path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(pf64, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        ]
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe') as k:
                cands.append(winreg.QueryValueEx(k, '')[0])
        except Exception:
            pass
    seen, out = set(), []
    for p in cands:
        if p and os.path.exists(p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out

def _open_url(url: str) -> bool:
    """打开浏览器,多级兜底,每步都落日志。
    Windows 冻结 exe 下 webbrowser.open/os.startfile 都可能"返回成功但实际没弹窗口"
    (默认浏览器关联缺失/损坏/被组策略限制),所以 Windows 优先直接启动具体的浏览器
    exe,不走默认关联;os.startfile/webbrowser.open 降级为兜底。"""
    if sys.platform == 'win32':
        for exe in _browser_exe_candidates():
            try:
                subprocess.Popen([exe, url], creationflags=0x08000000)  # CREATE_NO_WINDOW
                print(f'✅ 浏览器已打开(直接启动 {os.path.basename(exe)})')
                return True
            except Exception as e:
                print(f'⚠️ 直接启动 {os.path.basename(exe)} 失败: {e}')
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

    def _wait_server_ready(timeout: float = 20) -> bool:
        """等 uvicorn 真正开始响应再开浏览器。onefile 解包 + 冷启动在慢机器上
        可能超过 1.5s,直接开会导致页面显示"无法访问",且不算打开失败、不会重试。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if requests.get(url, timeout=2).status_code < 500:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

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
        if not _wait_server_ready():
            print('⚠️ 服务迟迟未就绪,仍尝试打开浏览器')
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
    resolve_identity_on_startup()  # 先定位账号标识,让遗留标识迁移赶在任务注册之前
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
