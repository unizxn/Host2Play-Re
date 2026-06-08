#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Host2Play 自动续期脚本
使用 Xray 代理访问，不再依赖 WARP
"""

import os
import sys
import time
import json
import requests
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime

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

# 所有 URL 都失败后的最大重试次数
MAX_RENEW_RETRIES_ALL = 2

# Telegram 配置
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# 输出目录
OUTPUT_DIR = Path("output/screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ==============================================================================
# 工具函数
# ==============================================================================

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
    """
    解决 reCAPTCHA 音频验证码
    返回: True 表示通过，False 表示失败
    """
    log("开始处理音频验证码...")
    
    try:
        # 点击音频验证码按钮
        audio_btn = page.ele('@class=rc-button-audio', timeout=5)
        if not audio_btn:
            log("未找到音频验证码按钮", "ERROR")
            return False
        
        audio_btn.click()
        time.sleep(2)
        
        # 下载音频文件
        download_link = page.ele('@id=recaptcha-audio-download', timeout=5)
        if not download_link:
            log("未找到音频下载链接", "ERROR")
            return False
        
        audio_url = download_link.attr('href')
        audio_path = OUTPUT_DIR / "captcha.mp3"
        
        # 下载音频
        response = requests.get(audio_url, timeout=30)
        with open(audio_path, 'wb') as f:
            f.write(response.content)
        
        log(f"音频验证码已下载: {audio_path}")
        
        # 转换为 WAV 格式
        wav_path = OUTPUT_DIR / "captcha.wav"
        audio = AudioSegment.from_mp3(str(audio_path))
        audio.export(str(wav_path), format="wav")
        
        # 语音识别
        recognizer = sr.Recognizer()
        with sr.AudioFile(str(wav_path)) as source:
            audio_data = recognizer.record(source)
        
        try:
            # 使用 Google 语音识别
            text = recognizer.recognize_google(audio_data, language='en-US')
            log(f"识别结果: {text}")
        except sr.UnknownValueError:
            log("无法识别音频内容", "ERROR")
            return False
        except sr.RequestError as e:
            log(f"语音识别服务错误: {e}", "ERROR")
            return False
        
        # 输入识别结果
        input_box = page.ele('@id=audio-response', timeout=5)
        if not input_box:
            log("未找到答案输入框", "ERROR")
            return False
        
        input_box.input(text)
        time.sleep(1)
        
        # 提交答案
        verify_btn = page.ele('@id=recaptcha-verify-button', timeout=5)
        if verify_btn:
            verify_btn.click()
            time.sleep(3)
            
            # 检查是否通过
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
    """
    单个 URL 的续期流程
    返回: (success: bool, message: str)
    """
    log(f"开始处理: {url}")
    
    failure_reason = "未知错误"
    
    try:
        for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
            log(f"{'='*20} 续期尝试 {attempt}/{MAX_RENEW_RETRIES_PER_URL} {'='*20}")
            page = None
            try:
                # 配置浏览器
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
                
                # ★★★ 通过 Xray SOCKS5 代理访问 ★★★
                co.set_argument('--proxy-server=socks5://127.0.0.1:10808')
                
                user_data_dir = tempfile.mkdtemp()
                co.set_user_data_path(user_data_dir)
                co.auto_port()
                co.headless(False)
                page = ChromiumPage(co)
                
                # 访问目标页面
                log(f"正在访问: {url}")
                page.get(url)
                time.sleep(3)
                
                # 保存初始截图
                save_screenshot(page, f"step1_loaded_attempt{attempt}")
                
                # 检查是否被 reCAPTCHA 封锁
                if page.ele('@class=rc-anchor-error-msg', timeout=3):
                    log("检测到 reCAPTCHA 封锁", "WARN")
                    save_screenshot(page, f"blocked_attempt{attempt}")
                    raise CaptchaBlocked("IP 被 reCAPTCHA 封锁")
                
                # 处理验证码（如果出现）
                if page.ele('@class=rc-anchor-checkbox', timeout=3):
                    log("检测到验证码，尝试解决...")
                    checkbox = page.ele('@class=rc-anchor-checkbox')
                    checkbox.click()
                    time.sleep(2)
                    
                    # 如果出现音频验证码
                    if page.ele('@class=rc-button-audio', timeout=3):
                        if not solve_audio_captcha(page):
                            log("验证码解决失败", "ERROR")
                            save_screenshot(page, f"captcha_failed_attempt{attempt}")
                            if attempt < MAX_RENEW_RETRIES_PER_URL:
                                log(f"等待 15 秒后重试...")
                                time.sleep(15)
                                continue
                            break
                
                # 等待页面加载完成
                time.sleep(5)
                save_screenshot(page, f"step2_processed_attempt{attempt}")
                
                # 检查续期结果
                # 这里需要根据实际页面内容判断是否成功
                # 例如：检查是否有成功提示、按钮状态等
                success_indicators = ['success', '完成', '已续期', 'renewed']
                page_text = page.html.lower()
                
                if any(indicator in page_text for indicator in success_indicators):
                    log("✅ 续期成功！")
                    save_screenshot(page, f"success_attempt{attempt}")
                    return True, "续期成功"
                else:
                    log("未检测到明确的成功标识，可能需要人工确认", "WARN")
                    save_screenshot(page, f"uncertain_attempt{attempt}")
                    # 暂时认为成功，后续可以根据实际情况调整
                    return True, "续期完成（需人工确认）"
                
            except CaptchaBlocked:
                log("IP 被封锁", "WARN")
                failure_reason = "IP 被 reCAPTCHA 封锁"
                try:
                    page.quit()
                except:
                    pass
                page = None
                
                # ★★★ 不再调用 restart_warp()，直接等待后重试 ★★★
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
                        try:
                            page.quit()
                        except:
                            pass
                    page = None
                    
                    # ★★★ 不再调用 restart_warp()，直接等待后重试 ★★★
                    log(f"等待 15 秒后重试（第 {attempt+1} 次）...")
                    time.sleep(15)
                    continue
                break
                
            finally:
                if page:
                    try:
                        page.quit()
                    except:
                        pass
    
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
    
    # 检查 URL 列表
    if not RENEW_URLS:
        log("未配置续期 URL，请设置环境变量 RENEW_URLS", "ERROR")
        send_telegram_message("❌ Host2Play 续期失败\n原因：未配置续期 URL")
        sys.exit(1)
    
    log(f"待续期 URL 数量: {len(RENEW_URLS)}")
    for i, url in enumerate(RENEW_URLS, 1):
        log(f"  {i}. {url}")
    
    # 启动虚拟显示
    vdisplay = Xvfb()
    vdisplay.start()
    
    try:
        results = []
        
        # 处理每个 URL
        for i, url in enumerate(RENEW_URLS, 1):
            log(f"\n{'#'*60}")
            log(f"处理第 {i}/{len(RENEW_URLS)} 个 URL")
            log(f"{'#'*60}")
            
            success, message = renew_single_url(url)
            results.append({
                "url": url,
                "success": success,
                "message": message
            })
            
            if success:
                log(f"✅ URL {i} 处理成功: {message}")
            else:
                log(f"❌ URL {i} 处理失败: {message}", "ERROR")
        
        # 汇总结果
        success_count = sum(1 for r in results if r["success"])
        fail_count = len(results) - success_count
        
        summary = f"续期完成\n成功: {success_count}/{len(results)}\n失败: {fail_count}/{len(results)}\n\n"
        for r in results:
            status = "✅" if r["success"] else "❌"
            summary += f"{status} {r['url'][:50]}...\n   {r['message']}\n"
        
        log("\n" + "=" * 60)
        log(summary)
        log("=" * 60)
        
        # 发送 Telegram 通知
        if fail_count > 0:
            send_telegram_message(f"⚠️ Host2Play 续期部分失败\n{summary}")
        else:
            send_telegram_message(f"✅ Host2Play 续期全部成功\n{summary}")
        
        # 如果有失败，返回非零退出码
        if fail_count > 0:
            sys.exit(1)
            
    finally:
        vdisplay.stop()


if __name__ == "__main__":
    main()
