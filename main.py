#!/usr/bin/env python3
"""
Host2Play 自动续期脚本 - Xray 代理版

基于原版 Host2Play 脚本修改，核心变更：
  ✅ 移除所有 WARP 相关代码（restart_warp, warp-cli, WARP 安装步骤）
  ✅ Chrome 通过 --proxy-server=socks5:// 使用 Xray SOCKS5 代理
  ✅ IP 被封锁时等待重试，不再切换 WARP
  ✅ 保留：DrissionPage + Chrome 自动化、reCAPTCHA 音频破解、Telegram 通知、截图

原项目：https://github.com/oyz8/Host2Play
代理方案参考：https://github.com/unizxn/Zampto_Re
"""

import os
import sys
import time
import random
import requests
import tempfile
import subprocess
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

# 代理设置 - Xray SOCKS5 代理
PROXY_HOST = "127.0.0.1"
PROXY_PORT = os.environ.get("PROXY_PORT", "10808")
SOCKS5_PROXY = f"socks5://{{PROXY_HOST}}:{{PROXY_PORT}}"

# 续期链接列表
RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=test-12345",
]

# 重试配置
MAX_CAPTCHA = int(os.environ.get("MAX_CAPTCHA", "5"))
MAX_RENEW_RETRIES_PER_URL = int(os.environ.get("MAX_RENEW_RETRIES", "3"))

# Telegram 配置
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

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
    print(f"{{prefix}} {{msg}}", flush=True)

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
    url = f"https://api.telegram.org/bot{{token}}/sendPhoto"
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
        log(f"Telegram 图片通知异常: {{e}}", "ERROR")

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
            f"服务器：{{server_name}}",
            f"到期: {{old_expire}} -> {{new_expire}}",
            f"URL: {{url}}",
            f"代理: {{SOCKS5_PROXY}}",
        ]
    else:
        lines = [
            "❌ 续订失败",
            "",
            f"服务器：{{server_name}}",
            f"URL: {{url}}",
            f"代理: {{SOCKS5_PROXY}}",
        ]
        if failure_reason:
            lines.append(f"失败原因: {{failure_reason}}")
    lines.append("")
    lines.append("Host2Play Auto Renew (Xray Proxy)")
    return "\n".join(lines)

def capture_page_screenshot(page, file_name):
    try:
        page.get_screenshot(path=file_name)
        return file_name
    except Exception as e:
        log(f"截图失败: {{e}}", "WARN")
        return None

# ==============================================================================
# reCAPTCHA 辅助函数
# ==============================================================================

def find_recaptcha_frame(page, kind):
    try:
        for frame in page.get_frames():
            frame_url = frame.url or ""
            if "recaptcha" in frame_url and kind in frame_url:
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            try:
                token = frame.run_js("return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value")
                if token and len(token) > 30:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    anchor = find_recaptcha_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js("return document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'")
            if checked:
                return True
        except Exception:
            pass
    return False

def is_blocked(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        return bool(bframe.run_js("""
            const h = document.querySelector('.rc-doscaptcha-header-text');
            if (h && h.textContent.toLowerCase().includes('try again later')) return true;
            const e = document.querySelector('.rc-audiochallenge-error-message');
            if (e && e.offsetParent !== null) return true;
            return false;
        """))
    except Exception:
        return False

def click_recaptcha_checkbox(page):
    anchor = find_recaptcha_frame(page, "anchor")
    if not anchor:
        for _ in range(120):
            anchor = find_recaptcha_frame(page, "anchor")
            if anchor:
                break
            time.sleep(1)
    if not anchor:
        raise RuntimeError("未找到 reCAPTCHA anchor frame")
    checkbox = anchor.ele('#recaptcha-anchor', timeout=3)
    if not checkbox:
        raise RuntimeError("未找到 reCAPTCHA 复选框")
    page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0))
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except Exception:
        checkbox.click(by_js=True)
    time.sleep(3)
    if is_blocked(page):
        raise CaptchaBlocked("点击复选框后检测到 IP 被封锁")

def switch_to_audio(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    for attempt in range(3):
        try:
            audio_btn = bframe.ele('#recaptcha-audio-button', timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                if is_blocked(page):
                    raise CaptchaBlocked("点击音频按钮后检测到 IP 被封锁")
                input_box = bframe.ele('#audio-response', timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        try:
            bframe.run_js("""
                const btn = document.querySelector('#recaptcha-audio-button');
                if (btn) btn.click();
            """)
            time.sleep(3)
            if is_blocked(page):
                raise CaptchaBlocked("JS点击音频按钮后检测到 IP 被封锁")
            input_box = bframe.ele('#audio-response', timeout=1)
            if input_box and input_box.states.is_displayed:
                return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        time.sleep(2)
    return False

def get_audio_url(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele('.rc-audiochallenge-tdo-link', timeout=1)
            if link:
                href = link.attr('href')
                if href:
                    return href
        except Exception:
            pass
        try:
            link = bframe.ele('tag:a@text():Download', timeout=1)
            if link:
                href = link.attr('href')
                if href:
                    return href
        except Exception:
            pass
        time.sleep(1)
    return None

def recognize_audio(audio_url):
    recognizer = sr.Recognizer()
    for attempt in range(3):
        try:
            resp = requests.get(audio_url, timeout=15)
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            audio = AudioSegment.from_mp3(tmp_path)
            wav_path = tmp_path.replace('.mp3', '.wav')
            audio.export(wav_path, format='wav')
            os.unlink(tmp_path)
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
            os.unlink(wav_path)
            text = recognizer.recognize_google(audio_data)
            log(f"识别结果: {{text}}")
            return text
        except sr.UnknownValueError:
            log(f"语音识别失败（第 {{attempt+1}} 次），无法理解音频", "WARN")
        except sr.RequestError as e:
            log(f"Google 语音识别 API 请求失败: {{e}}", "WARN")
            time.sleep(2)
        except Exception as e:
            log(f"音频处理异常: {{e}}", "WARN")
            time.sleep(1)
    return None

def submit_audio_answer(page, answer):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=3)
        if not input_box:
            return False
        input_box.clear()
        input_box.input(answer)
        time.sleep(random.uniform(0.5, 1.0))
        submit_btn = bframe.ele('#recaptcha-verify-button', timeout=3)
        if submit_btn:
            try:
                submit_btn.click()
            except Exception:
                submit_btn.click(by_js=True)
            time.sleep(3)
            return True
    except Exception as e:
        log(f"提交音频答案异常: {{e}}", "WARN")
    return False

# ==============================================================================
# 核心续期逻辑
# ==============================================================================

def do_renew(page, url):
    """对单个 URL 执行续期操作，返回 (success, old_expire, new_expire, failure_reason)"""
    server_name = "未知"
    old_expire = "未知"

    for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
        log(f"==================== 续期尝试 {{attempt}}/{{MAX_RENEW_RETRIES_PER_URL}} ====================")
        log(f"访问: {{url}}")

        try:
            page.get(url)
            time.sleep(3)

            server_name = get_server_name(page)
            old_expire = get_expire_time(page)
            log(f"服务器: {{server_name}}，到期时间: {{old_expire}}")

            # 点击续期按钮打开弹窗
            try:
                renew_btn = page.ele('text:Renew', timeout=5) or page.ele('text:Odnow', timeout=2)
                if renew_btn:
                    renew_btn.click()
                    time.sleep(2)
                    log("打开续期弹窗...")
                else:
                    log("未找到 Renew 按钮，可能已在弹窗页面", "WARN")
            except Exception:
                pass

            # 启动 reCAPTCHA 处理
            log("启动 reCAPTCHA 音频破解...")
            try:
                click_recaptcha_checkbox(page)
            except CaptchaBlocked as e:
                log(f"IP 被封锁，等待后重试: {{e}}", "WARN")
                # ✅ 关键变更：不再调用 restart_warp()，而是等待后重试
                # Xray 代理提供纯净住宅 IP，不易被封
                wait_time = 60 + random.randint(10, 30)
                log(f"等待 {{wait_time}} 秒后重试（Xray 代理模式，不切换 IP）...")
                time.sleep(wait_time)
                continue
            except Exception as e:
                log(f"点击验证码复选框异常: {{e}}", "WARN")

            time.sleep(2)

            # 检查是否直接通过
            if is_recaptcha_solved(page):
                log("reCAPTCHA 直接通过")
            else:
                # 尝试音频模式
                if switch_to_audio(page):
                    for captcha_attempt in range(MAX_CAPTCHA):
                        audio_url = get_audio_url(page)
                        if not audio_url:
                            log("未获取到音频 URL", "WARN")
                            break

                        answer = recognize_audio(audio_url)
                        if answer:
                            if submit_audio_answer(page, answer):
                                time.sleep(3)
                                if is_recaptcha_solved(page):
                                    log(f"✅ reCAPTCHA 音频破解成功（第 {{captcha_attempt+1}} 次）")
                                    break
                                else:
                                    log(f"验证码验证失败（第 {{captcha_attempt+1}} 次），重试...", "WARN")
                                    if is_blocked(page):
                                        raise CaptchaBlocked("验证码验证后 IP 被封锁")
                        else:
                            log("语音识别失败", "WARN")

                        time.sleep(2)
                else:
                    log("无法切换到音频模式", "WARN")

            # 检查是否成功解决验证码
            if not is_recaptcha_solved(page):
                if is_blocked(page):
                    log("IP 被封锁", "WARN")
                    wait_time = 60 + random.randint(10, 30)
                    log(f"等待 {{wait_time}} 秒后重试...")
                    time.sleep(wait_time)
                    continue
                log("验证码未解决，重试...", "WARN")
                continue

            # 验证码通过，检查续期结果
            time.sleep(3)
            new_expire = get_expire_time(page)

            if new_expire != "未知" and new_expire != old_expire:
                log(f"✅ 续期成功！{{old_expire}} -> {{new_expire}}")
                return True, old_expire, new_expire, ""

            # 检查页面是否有成功提示
            try:
                page_text = page.html.lower()
                if any(w in page_text for w in ['success', 'renewed', 'odnowion', 'przedłużon']):
                    log("✅ 检测到续期成功提示")
                    return True, old_expire, new_expire, ""
            except Exception:
                pass

            log(f"续期结果不确定，到期时间: {{new_expire}}", "WARN")

        except CaptchaBlocked as e:
            log(f"IP 被封锁: {{e}}", "WARN")
            wait_time = 60 + random.randint(10, 30)
            log(f"等待 {{wait_time}} 秒后重试（Xray 代理模式）...")
            time.sleep(wait_time)
            continue
        except Exception as e:
            log(f"续期过程异常: {{e}}", "ERROR")
            time.sleep(5)
            continue

    return False, old_expire, "未知", "达到最大重试次数"

# ==============================================================================
# 主函数
# ==============================================================================

def main():
    log("=" * 60)
    log("Host2Play 自动续期 - Xray 代理版")
    log(f"代理: {{SOCKS5_PROXY}}")
    log(f"续期链接数: {{len(RENEW_URLS)}}")
    log(f"最大验证码重试: {{MAX_CAPTCHA}}")
    log(f"最大续期重试: {{MAX_RENEW_RETRIES_PER_URL}}")
    log(f"时间: {{datetime.now().isoformat()}}")
    log("=" * 60)

    if not RENEW_URLS:
        log("未配置续期链接！", "ERROR")
        return

    # 检查代理 IP
    try:
        proxy_ip = requests.get(
            "https://api.ipify.org",
            proxies={"https": f"socks5h://{{PROXY_HOST}}:{{PROXY_PORT}}"},
            timeout=10
        ).text
        log(f"代理 IP: {{proxy_ip}}")
    except Exception as e:
        log(f"获取代理 IP 失败: {{e}}", "WARN")

    # 设置截图目录
    os.makedirs("output/screenshots", exist_ok=True)

    success_count = 0
    total = len(RENEW_URLS)

    with Xvfb(width=1280, height=720, colordepth=24):
        # 创建 Chrome 实例，配置 SOCKS5 代理
        co = ChromiumOptions()
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--window-size=1280,720")
        co.set_argument("--disable-infobars")
        co.set_argument("--disable-extensions")
        co.set_argument("--disable-notifications")
        co.set_argument("--lang=en-US")
        # ✅ 核心配置：使用 Xray SOCKS5 代理替代 WARP
        co.set_argument(f"--proxy-server={{SOCKS5_PROXY}}")
        log(f"Chrome 配置 SOCKS5 代理: {{SOCKS5_PROXY}}")

        page = ChromiumPage(addr_or_opts=co)

        for i, url in enumerate(RENEW_URLS, 1):
            log(f"\n处理第 {{i}}/{{total}} 个链接")
            success, old_expire, new_expire, reason = do_renew(page, url)

            # 截图
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"output/screenshots/{{ts}}_{{'success' if success else 'fail'}}_{{i}}.png"
            capture_page_screenshot(page, screenshot_name)

            # 发送通知
            server_name = get_server_name(page)
            notification = build_notification(success, url, server_name, old_expire, new_expire, reason)
            send_tg_photo(TG_BOT_TOKEN, TG_CHAT_ID, screenshot_name, notification)

            if success:
                success_count += 1

            if i < total:
                time.sleep(5)

        try:
            page.quit()
        except Exception:
            pass

    log(f"\n全部完成，成功 {{success_count}}/{{total}} 个链接")

if __name__ == "__main__":
    main()
