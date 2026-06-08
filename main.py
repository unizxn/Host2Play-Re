#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Host2Play 自动续期脚本
使用 Xray SOCKS5 代理访问，已移除 WARP 逻辑
包含 URL 隐私保护，防止敏感参数泄露到日志和 Telegram 通知中
"""

import os
import sys
import time
import json
import requests
import tempfile
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

from DrissionPage import ChromiumPage, ChromiumOptions
from xvfbwrapper import Xvfb
import speech_recognition as sr
from pydub import AudioSegment

# ==============================================================================
# 配置区域
# ==============================================================================

# 续期 URL 列表（从环境变量读取，JSON 格式）
RENEW_URLS = json.loads(os.getenv("RENEW_URLS", "[]"))

# 每个 URL 的最大重试次数
MAX_RENEW_RETRIES_PER_URL = 3

# Telegram 配置
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# 输出目录
OUTPUT_DIR = Path("output/screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ==============================================================================
# 工具函数
# ==============================================================================

def mask_url(url):
    """
    隐私处理 URL，隐藏敏感参数值，只保留域名、路径和参数名。
    例如：?i=ae2b2db1-xxxx-xxxx-xxxx-xxxxxxxxxx59 -> ?i=ae********************************59
    """
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        masked_params = {}
        for key, value in params.items():
            if value:
                val = value[0]
                if len(val) > 8:
                    # 保留前2位和后2位，中间用 * 替代
                    masked_params[key] = [f"{val[:2]}{'*' * (len(val) - 4)}{val[-2:]}"]
                else:
                    masked_params[key] = ['*' * len(val)]
            else:
                masked_params[key] = ['']
        
        masked_query = urlencode(masked_params, doseq=True)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{masked_query}"
    except Exception:
        # 如果解析失败，返回完全脱敏的占位符
        return "https://***.***.***/***?i=***"


def log(message, level="INFO"):
    """带时间戳的日志输出"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")
    sys.stdout.flush()


def send_telegram_message(message):
    """发送 Telegram 消息"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("Telegram 未配置，跳过通知", "WARN")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            log("Telegram 通知发送成功")
        else:
            log(f"Telegram 通知发送失败: {response.text}", "ERROR")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def save_screenshot(page, name):
    """保存截图"""
    try:
        filepath = OUTPUT_DIR / f"{name}_{int(time.time())}.png"
        page.get_screenshot(path=str(filepath), full_page=False)
        log(f"截图已保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "ERROR")
        return None


class CaptchaBlocked(Exception):
    """IP 被 reCAPTCHA 封锁异常"""
    pass


def solve_audio_captcha(page):
    """解决 reCAPTCHA 音频验证码"""
    log("开始处理音频验证码...")
    try:
        audio_btn = page.ele('@class=rc-button-audio', timeout=5)
        if not audio_btn:
            return False
        audio_btn.click()
        time.sleep(2)
        
        download_link = page.ele('@id=recaptcha-audio-download', timeout=5)
        if not download_link:
            return False
        
        audio_url = download_link.attr('href')
        audio_path = OUTPUT_DIR / "captcha.mp3"
        
        response = requests.get(audio_url, timeout=30)
        with open(audio_path, 'wb') as f:
            f.write(response.content)
        
        wav_path = OUTPUT_DIR / "captcha.wav"
        audio = AudioSegment.from_mp3(str(audio_path))
        audio.export(str(wav_path), format="wav")
        
        recognizer = sr.Recognizer()
        with sr.AudioFile(str(wav_path)) as source:
            audio_data = recognizer.record(source)
        
        try:
            text = recognizer.recognize_google(audio_data, language='en-US')
            log(f"识别结果: {text}")
        except sr.UnknownValueError:
            log("无法识别音频内容", "ERROR")
            return False
        except sr.RequestError as e:
            log(f"语音识别服务错误: {e}", "ERROR")
            return False
        
        input_box = page.ele('@id=audio-response', timeout=5)
        if not input_box:
            return False
        
        input_box.input(text)
        time.sleep(1)
        
        verify_btn = page.ele('@id=recaptcha-verify-button', timeout=5)
        if verify_btn:
            verify_btn.click()
            time.sleep(3)
            if page.ele('@class=rc-anchor-error-msg', timeout=2):
                log("验证码答案错误", "ERROR")
                return False
            log("验证码通过！")
            return True
    except Exception as e:
        log(f"处理验证码异常: {e}", "ERROR")
    return False


# ==============================================================================
# 单个 URL 续期流程
# ==============================================================================

def renew_single_url(url):
    """单个 URL 的续期流程"""
    # ★★★ 关键：日志输出使用脱敏后的 URL ★★★
    masked_url = mask_url(url)
    log(f"开始处理: {masked_url}")
    failure_reason = "未知错误"
    
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
                
                # 通过 Xray SOCKS5 代理访问
                co.set_argument('--proxy-server=socks5://127.0.0.1:10808')
                
                user_data_dir = tempfile.mkdtemp()
                co.set_user_data_path(user_data_dir)
                co.auto_port()
                co.headless(False)
                page = ChromiumPage(co)
                
                # ★★★ 关键：日志输出使用脱敏后的 URL，但 page.get 必须使用原始真实的 url ★★★
                log(f"正在访问: {masked_url}")
                page.get(url)
                time.sleep(3)
                
                save_screenshot(page, f"step1_loaded_attempt{attempt}")
                
                if page.ele('@class=rc-anchor-error-msg', timeout=3):
                    log("检测到 reCAPTCHA 封锁", "WARN")
                    save_screenshot(page, f"blocked_attempt{attempt}")
                    raise CaptchaBlocked("IP 被 reCAPTCHA 封锁")
                
                if page.ele('@class=rc-anchor-checkbox', timeout=3):
                    log("检测到验证码，尝试解决...")
                    checkbox = page.ele('@class=rc-anchor-checkbox')
                    checkbox.click()
                    time.sleep(2)
                    
                    if page.ele('@class=rc-button-audio', timeout=3):
                        if not solve_audio_captcha(page):
                            log("验证码解决失败", "ERROR")
                            save_screenshot(page, f"captcha_failed_attempt{attempt}")
                            if attempt < MAX_RENEW_RETRIES_PER_URL:
                                log(f"等待 15 秒后重试...")
                                time.sleep(15)
                                continue
                            break
                
                time.sleep(5)
                save_screenshot(page, f"step2_processed_attempt{attempt}")
                
                success_indicators = ['success', '完成', '已续期', 'renewed']
                page_text = page.html.lower()
                
                if any(indicator in page_text for indicator in success_indicators):
                    log("✅ 续期成功！")
                    save_screenshot(page, f"success_attempt{attempt}")
                    return True, "续期成功"
                else:
                    log("未检测到明确的成功标识，暂判为完成", "WARN")
                    save_screenshot(page, f"uncertain_attempt{attempt}")
                    return True, "续期完成（需人工确认）"
                
            except CaptchaBlocked:
                log("IP 被封锁", "WARN")
                failure_reason = "IP 被 reCAPTCHA 封锁"
                if page:
                    try: page.quit()
                    except: pass
                page = None
                
                if attempt < MAX_RENEW_RETRIES_PER_URL:
                    log(f"等待 15 秒后重试（第 {attempt+1} 次）...")
                    time.sleep(15)
                    continue
                break
                
            except Exception as e:
                log(f"续期尝试异常: {e}", "ERROR")
                failure_reason = f"运行异常: {str(e)[:200]}"
                if attempt < MAX_RENEW_RETRIES_PER_URL:
                    if page:
                        try: page.quit()
                        except: pass
                    page = None
                    log(f"等待 15 秒后重试（第 {attempt+1} 次）...")
                    time.sleep(15)
                    continue
                break
                
            finally:
                if page:
                    try: page.quit()
                    except: pass
    
    except Exception as e:
        log(f"续期流程异常: {e}", "ERROR")
        failure_reason = f"流程异常: {str(e)[:200]}"
    
    return False, failure_reason


# ==============================================================================
# 主流程
# ==============================================================================

def main():
    """主函数"""
    log("=" * 60)
    log("Host2Play 自动续期脚本启动")
    log("=" * 60)
    
    if not RENEW_URLS:
        log("未配置续期 URL，请设置环境变量 RENEW_URLS", "ERROR")
        send_telegram_message("❌ Host2Play 续期失败\n原因：未配置续期 URL (RENEW_URLS)")
        sys.exit(1)
    
    log(f"待续期 URL 数量: {len(RENEW_URLS)}")
    
    vdisplay = Xvfb()
    vdisplay.start()
    
    try:
        results = []
        for i, url in enumerate(RENEW_URLS, 1):
            log(f"\n{'#'*60}")
            log(f"处理第 {i}/{len(RENEW_URLS)} 个 URL")
            log(f"{'#'*60}")
            
            success, message = renew_single_url(url)
            # 存入结果时保留原始 URL，但在打印时脱敏
            results.append({"url": url, "success": success, "message": message})
            
            if success:
                log(f"✅ URL {i} 处理成功: {message}")
            else:
                log(f"❌ URL {i} 处理失败: {message}", "ERROR")
        
        success_count = sum(1 for r in results if r["success"])
        fail_count = len(results) - success_count
        
        summary = f"续期完成\n成功: {success_count}/{len(results)}\n失败: {fail_count}/{len(results)}\n\n"
        for r in results:
            status = "✅" if r["success"] else "❌"
            # ★★★ 关键：汇总报告和 TG 通知中的 URL 也必须脱敏 ★★★
            summary += f"{status} {mask_url(r['url'])}\n   {r['message']}\n"
        
        log("\n" + "=" * 60)
        log(summary)
        log("=" * 60)
        
        if fail_count > 0:
            send_telegram_message(f"⚠️ Host2Play 续期部分失败\n{summary}")
        else:
            send_telegram_message(f"✅ Host2Play 续期全部成功\n{summary}")
        
        if fail_count > 0:
            sys.exit(1)
            
    finally:
        vdisplay.stop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(f"脚本主动退出，退出码: {e.code}", "WARN")
        sys.stdout.flush()
        sys.exit(e.code)
    except Exception as e:
        error_msg = f"❌ Host2Play 脚本崩溃\n异常类型: {type(e).__name__}\n错误信息: {str(e)}"
        log(error_msg, "FATAL")
        send_telegram_message(error_msg)
        sys.stdout.flush()
        sys.exit(1)
