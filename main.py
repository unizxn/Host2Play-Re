import os
import sys
import time
import random
import html
import requests
import tempfile
import subprocess
import signal
from datetime import datetime, timezone, timedelta
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    pass

# ==============================================================================
# 配置区域
# ==============================================================================
RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=ae2b2db1-2ade-4401-adf9-786b055d8559",
    # 添加更多链接
]

MAX_CAPTCHA = 3
MAX_RENEW_RETRIES_PER_URL = 50

# Xray 代理配置
XRAY_PROXY = "socks5://127.0.0.1:10808"
XRAY_CONFIG_PATH = "config.json"
XRAY_PID_FILE = "/tmp/xray.pid"

# ==============================================================================
# 自定义异常
# ==============================================================================
class CaptchaBlocked(Exception):
    pass

# ==============================================================================
# 统一日志
# ==============================================================================
def log(msg, level="INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}", flush=True)

# ==============================================================================
# Telegram 通知
# ==============================================================================
def send_tg_photo(token, chat_id, photo_path, caption, parse_mode='HTML'):
    if not token or not chat_id:
        log("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知。", "WARN")
        return
    if not photo_path or not os.path.exists(photo_path):
        log("未找到截图文件，跳过通知。", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo_file:
            response = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                files={"photo": photo_file},
                timeout=30,
            )
        response.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 图片通知异常: {e}", "ERROR")

# ==============================================================================
# 页面元素提取
# ==============================================================================
def get_server_name(page):
    try:
        ele = page.ele('#serverName', timeout=2)
        if ele:
            return ele.text.strip()
    except Exception:
        pass
    return "未知"

def get_expire_time(page):
    try:
        ele = page.ele('#expireDate', timeout=2)
        if ele:
            return ele.text.strip()
    except Exception:
        pass
    selectors = ['text:Expires in:', 'text:Deletes on:']
    for selector in selectors:
        try:
            ele = page.ele(selector, timeout=1)
            if ele:
                text = (ele.text or "").strip()
                if ":" in text:
                    return text.split(":", 1)[1].strip()
                if text:
                    return text
        except Exception:
            pass
    return "未知"

# ==============================================================================
# 构建通知
# ==============================================================================
def build_notification(success, url, server_name, old_expire, new_expire=None, failure_reason=""):
    if success:
        lines = [
            "✅ 续订成功",
            "",
            f"服务器：{server_name}",
            f"到期: {old_expire} -> {new_expire}",
            f"URL: {url}",
        ]
    else:
        lines = [
            "❌ 续订失败",
            "",
            f"服务器：{server_name}",
            f"URL: {url}",
        ]
        if failure_reason:
            lines.append(f"失败原因: {failure_reason}")
    lines.append("")
    lines.append("Host2Play Auto Renew (Xray)")
    return "\n".join(lines)

def capture_page_screenshot(page, file_name):
    try:
        page.get_screenshot(path=file_name)
        return file_name
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None

# ==============================================================================
# Xray 代理管理（替代 WARP）
# ==============================================================================
def get_proxy_ip():
    """通过 Xray 代理获取当前出口 IP"""
    try:
        proxies = {
            "http": "socks5h://127.0.0.1:10808",
            "https": "socks5h://127.0.0.1:10808"
        }
        ip = requests.get("https://api.ipify.org", timeout=10, proxies=proxies).text
        return ip
    except Exception:
        return "未知"

def get_direct_ip():
    """获取直连 IP（不走代理）"""
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text
        return ip
    except Exception:
        return "未知"

def restart_xray():
    """重启 Xray 进程以尝试获取新的出口 IP"""
    log("正在重启 Xray 以尝试更换出口 IP...")
    old_ip = get_proxy_ip()
    log(f"当前代理 IP: {old_ip}")
    try:
        stop_xray()
        time.sleep(2)
        start_xray()
        time.sleep(5)
        new_ip = get_proxy_ip()
        if new_ip != "未知":
            log(f"Xray 重启成功，代理 IP: {new_ip}")
            if new_ip != old_ip:
                log(f"IP 已更换: {old_ip} -> {new_ip}")
            else:
                log("IP 未变化（代理节点可能为固定 IP），继续尝试续期")
            return True
        else:
            log("Xray 重启后代理不可用", "ERROR")
            return False
    except Exception as e:
        log(f"Xray 重启失败: {e}", "ERROR")
        return False

def start_xray():
    """启动 Xray 代理进程"""
    if not os.path.exists(XRAY_CONFIG_PATH):
        log(f"Xray 配置文件不存在: {XRAY_CONFIG_PATH}", "ERROR")
        return False
    try:
        if os.path.exists(XRAY_PID_FILE):
            try:
                with open(XRAY_PID_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)
                log(f"Xray 已在运行 (PID: {old_pid})")
                return True
            except (ProcessLookupError, ValueError, FileNotFoundError):
                pass
        process = subprocess.Popen(
            ["./xray", "run", "-c", XRAY_CONFIG_PATH],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        with open(XRAY_PID_FILE, 'w') as f:
            f.write(str(process.pid))
        log(f"Xray 已启动 (PID: {process.pid})")
        return True
    except Exception as e:
        log(f"Xray 启动失败: {e}", "ERROR")
        return False

def stop_xray():
    """停止 Xray 代理进程"""
    try:
        if os.path.exists(XRAY_PID_FILE):
            try:
                with open(XRAY_PID_FILE, 'r') as f:
                    pid = int(f.read().strip())
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                log(f"Xray 进程已停止 (PID: {pid})")
            except ProcessLookupError:
                log("Xray 进程已不存在")
            except Exception as e:
                log(f"停止 Xray 失败: {e}", "WARN")
            finally:
                try:
                    os.remove(XRAY_PID_FILE)
                except:
                    pass
        subprocess.run(["pkill", "-f", "xray"],
                      check=False, timeout=5, capture_output=True)
    except Exception as e:
        log(f"停止 Xray 异常: {e}", "WARN")

def verify_proxy():
    """验证 Xray 代理是否正常工作"""
    log("验证 Xray 代理连接...")
    direct_ip = get_direct_ip()
    proxy_ip = get_proxy_ip()
    log(f"直连 IP: {direct_ip}")
    log(f"代理 IP: {proxy_ip}")
    if proxy_ip == "未知":
        log("代理 IP 获取失败，请检查 V2RAY_CONFIG 配置", "ERROR")
        return False
    if proxy_ip != direct_ip:
        log("✅ 代理工作正常，IP 已切换")
    else:
        log("⚠️ 代理 IP 与直连 IP 相同，代理可能未生效", "WARN")
    return True

# ==============================================================================
# reCAPTCHA 辅助函数 (保持不变)
# ==============================================================================
# ... (与原版完全相同，此处省略以节省篇幅)

# ==============================================================================
# 单个 URL 续期流程（使用 Xray 代理替代 WARP）
# ==============================================================================
def renew_single_url(url):
    success = False
    server_name = "未知"
    old_expire = "未知"
    new_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    screenshot_dir = "output/screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    vdisplay = Xvfb(width=1280, height=720, colordepth=24)
    vdisplay.start()

    try:
        for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
            log(f"{'='*20} 续期尝试 {attempt}/{MAX_RENEW_RETRIES_PER_URL} {'='*20}")
            page = None
            try:
                co = ChromiumOptions()
                co.set_browser_path('/usr/bin/google-chrome')
                co.set_argument('--no-sandbox')
                co.set_argument('--disable-dev-shm-usage')
                co.set_argument('--disable-gpu')
                co.set_argument('--disable-setuid-sandbox')
                co.set_argument('--disable-software-rasterizer')
                co.set_argument('--disable-extensions')
                co.set_argument('--no-first-run')
                co.set_argument('--no-default-browser-check')
                co.set_argument('--disable-popup-blocking')
                co.set_argument('--window-size=1280,720')
                co.set_argument('--log-level=3')
                co.set_argument('--silent')

                # ==========================================
                # 关键变更：通过 Xray SOCKS5 代理访问
                # 替代原来的 WARP 系统级代理
                # ==========================================
                co.set_argument(f'--proxy-server={XRAY_PROXY}')

                user_data_dir = tempfile.mkdtemp()
                co.set_user_data_path(user_data_dir)
                co.auto_port()
                co.headless(False)
                page = ChromiumPage(co)

                # ... (反指纹注入、页面操作等与原版相同)

            except CaptchaBlocked:
                log("IP 被封锁，尝试重启 Xray 更换 IP 后重试", "WARN")
                # ==========================================
                # 关键变更：用 restart_xray() 替代 restart_warp()
                # ==========================================
                if attempt < MAX_RENEW_RETRIES_PER_URL:
                    restart_xray()
                    continue
                break
    finally:
        vdisplay.stop()
    return success, server_name, old_expire, new_expire, screenshot_path, failure_reason

# ==============================================================================
# 主入口
# ==============================================================================
def main():
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    if not RENEW_URLS:
        log("请在 RENEW_URLS 列表中添加续期链接", "ERROR")
        sys.exit(1)

    # 验证 Xray 代理是否可用
    if not verify_proxy():
        log("Xray 代理验证失败，请检查 V2RAY_CONFIG 配置", "ERROR")
        sys.exit(1)

    total_success = 0
    for idx, url in enumerate(RENEW_URLS, 1):
        # ... 续期逻辑与原版相同
        pass

if __name__ == "__main__":
    main()
