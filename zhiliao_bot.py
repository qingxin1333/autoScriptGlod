#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知了快看(new.liao.view) 自动看广告领金币脚本
原理: adb 截图 -> RapidOCR 识别文字+坐标 -> 按状态机点击
用法: py zhiliao_bot.py [--interval 2.5] [--max-loops 0]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass

import cv2
import numpy as np

ADB = r"D:\AndroidSdk\platform-tools\adb.exe"
PKG = "new.liao.view"            # 知了快看包名
MAIN_HINT = "HomeActivity"       # 主界面 activity 特征

# ---------- 关键词表 ----------
SKIP_WORDS = ["跳过", "关闭"]                    # 广告退出按钮(顶部胶囊)
REWARD_DONE = ["领取成功", "奖励已领取", "奖励已发放", "恭喜获得奖励"]  # 广告奖励到账(顶部区域)
CONTINUE_WATCH = ["继续观看"]                    # 下载拦截/提醒 -> 继续看
CLAIM_CHAIN = ["全部都要", "开心收下", "看视频再领", "再领", "继续开红包", "立即领取奖励", "翻倍领取"]  # 领奖弹窗 -> 继续看广告
COIN_POPUP = ["恭喜获得金币", "获得金币", "惊喜红包", "获得现金红包"]  # +N 金币/红包弹窗
AD_CTA = ["我要加速", "点我加速", "立即体验", "立即查看", "查看详情", "上滑或点击"]  # 卡死广告 CTA
FAST_CLAIM = ["更快拿奖", "加速拿奖"]                # 限时弹层: 浏览详情15s直接领奖
AD_X_SPOTS = [(1160, 186), (1160, 252), (1170, 316), (1080, 325), (1160, 400),
              (1168, 2536), (1166, 2617)]  # 广告X常见位置: 右上一列扫描 + 右下两位
TASK_CLAIM = ["领奖励", "开宝箱得金币", "可领取"]   # 任务中心按钮
TASK_MILESTONE = ["领取"]                        # 里程碑领取(需配合上下文)
TAB_EARN = ["赚零花"]
INTERCEPT = ["已拦截", "尝试打开"]                # 系统拦截跳转提示
ALLOW_OPEN = ["仍要打开", "仍然打开", "允许打开"]   # 拦截框:放行跳转到外部APP
CANCEL_OPEN = ["知道了", "取消", "不允许", "暂不"]  # 拦截框里点这个留下(荣耀是"知道了")
GIVE_UP = ["放弃福利", "放弃奖励", "放弃领取"]      # 福利弹窗 -> 直接放弃等倒计时
INSTALL_DENY = ["禁止", "拒绝", "暂不"]            # 安装授权弹窗 -> 拒绝(永远不装)
# 广告挽留弹窗: "继续看X秒或下载安装即可领奖, 确定要退出吗?" [去领取奖励|坚持退出]
# 注意: 弹窗里"X秒"会被误读成倒计时, 必须先于倒计时解析处理
LEAVE_AD = ["坚持退出", "狠心离开", "仍要退出", "确认退出", "退出观看", "放弃奖励并退出"]
STAY_AD = ["去领取奖励", "继续观看", "继续播放", "再看一会", "继续浏览"]
SYS_PKGS = ("com.hihonor.iconnect", "com.hihonor.coauthservice", "com.android.systemui",
            "NotificationShade", "android", "com.miui", "com.huawei.systemmanager",
            "com.android.packageinstaller", "com.android.settings", "com.hihonor.systemmanager")
# 绝不能点
BLACKLIST = ["下载", "安装", "打开淘宝", "打开京东", "打开拼多多", "只领", "提现", "退出", "暂不"]

COUNTDOWN_RE = re.compile(r"(\d+)\s*[sS秒]")


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int = 0  # 词框宽度, 用于定位词内子串位置(如"领取成功|跳过"里的"跳过")


def sh(args, timeout=25, binary=False):
    cp = subprocess.run([ADB] + args, capture_output=True, timeout=timeout)
    return cp.stdout if binary else cp.stdout.decode("utf-8", errors="ignore")


def screenshot():
    try:
        png = sh(["exec-out", "screencap", "-p"], timeout=30, binary=True)
        if not png:
            return None
        return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def tap(x, y):
    sh(["shell", "input", "tap", str(int(x)), str(int(y))])
    log(f"点击 ({x},{y})")


def back():
    sh(["shell", "input", "keyevent", "KEYCODE_BACK"])
    log("返回键")


def swipe(x1, y1, x2, y2, ms=400):
    sh(["shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms))])
    log(f"上滑 ({x1},{y1})->({x2},{y2})")


def focus():
    out = sh(["shell", "dumpsys", "window"])
    m = re.search(r"mCurrentFocus=Window\{[^}]*? (\S+)\}", out)
    f = m.group(1) if m else ""
    return f.split(":", 1)[-1]  # 去掉 "SysXXXX:" 前缀


def relaunch_app():
    log("切回知了快看")
    sh(["shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1"])


def goto_home():
    """直接拉起主界面, 盖过赖着不走的广告页"""
    log("拉起主界面")
    sh(["shell", "am", "start", "-n", f"{PKG}/cn.youth.news.ui.homearticle.HomeActivity"])


_ocr = None
def ocr(img) -> list[Word]:
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    res, _ = _ocr(img)
    words = []
    for box, txt, conf in (res or []):
        if conf < 0.5:
            continue
        cx = int((box[0][0] + box[2][0]) / 2)
        cy = int((box[0][1] + box[2][1]) / 2)
        words.append(Word(txt, cx, cy, int(box[2][0] - box[0][0])))
    return words


def find(words, keys, blacklist=True):
    """找包含任一关键词的词; blacklist=True 时排除含危险词的词"""
    for w in words:
        for k in keys:
            if k in w.text:
                if blacklist and any(b in w.text for b in BLACKLIST):
                    continue
                return w
    return None


def sub_tap_x(w: Word, key: str) -> int:
    """词内子串的水平中心: '领取成功|跳过' 里点'跳过'应瞄右端而非词中心"""
    if not w.w or key not in w.text:
        return w.x
    frac = (w.text.find(key) + len(key) / 2) / len(w.text)
    return int(w.x - w.w / 2 + w.w * frac)


def find_all(words, keys, blacklist=True):
    out = []
    for w in words:
        for k in keys:
            if k in w.text and not (blacklist and any(b in w.text for b in BLACKLIST)):
                out.append(w)
                break
    return out


def countdown_left(words) -> int | None:
    """解析 '29s | 跳过' / 'X秒后可领' / '可提前X秒' 等倒计时"""
    for w in words:
        m = COUNTDOWN_RE.search(w.text)
        if m and ("跳过" in w.text or "秒" in w.text or "s" in w.text.lower()):
            return int(m.group(1))
    return None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def browse_wait(sec):
    """浏览等待: 每隔几秒下滑一次(部分落地页要求滚动才计时)"""
    end = time.time() + sec
    while time.time() < end:
        swipe(640, 2000, 640, 1200, 500)
        time.sleep(4)


def rescue_stuck_ad(timeout=12):
    """点CTA后快速轮询: 抓拦截框放行/跳外部APP/进落地页/倒计时恢复"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = focus()
        if f and not f.startswith(PKG):
            return "external"
        img = screenshot()
        if img is not None:
            ws = ocr(img)
            if find(ws, REWARD_DONE):
                return "ok"
            w = find(ws, ALLOW_OPEN, blacklist=False)
            if w:
                tap(w.x, w.y)
                return "external"
            iw = find(ws, INTERCEPT, blacklist=False)
            if iw:
                tap(iw.x, iw.y)   # 点拦截横幅, 让它弹出确认框
            w = next((w for w in ws if "返回" in w.text and w.y > 2200), None)
            if w:
                return "landing"
        time.sleep(1.2)
    return "nothing"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.5, help="轮询间隔秒")
    ap.add_argument("--max-loops", type=int, default=0, help="最大轮次 0=无限")
    ap.add_argument("--stuck-sec", type=float, default=18, help="倒计时不动多少秒判定卡死")
    args = ap.parse_args()

    # 连通性
    assert "device" in sh(["devices"]), "没有 adb 设备"
    size = sh(["shell", "wm", "size"])
    log(f"设备: {size.strip()}")

    last_cd = None           # 上次倒计时读数
    last_cd_change = time.time()
    browse_until = 0.0       # 点击 CTA 后等待落地页浏览结束的时刻
    reward_done_clicks = 0   # 奖励页连点退出次数(防卡死)
    reward_done_at = 0.0     # 最近一次看到奖励已领取的时间
    stuck_rescues = 0        # 当前广告连续卡死救援次数
    dead_backs = 0           # 连续无效返回次数
    abandon_ad = False       # 放弃当前广告: 遇挽留弹窗点"坚持退出"
    exit_stay_fails = 0      # 挽留弹窗连点留下仍反复弹 -> 转退出
    fast_claim_tries = 0     # "我要更快拿奖"弹层点击次数(跳外部仍不领奖则放弃)
    last_coin_sig = ""       # 金币弹窗去重签名
    coin_close_tries = 0     # 当前弹窗关闭尝试次数
    no_img = 0               # 连续截图失败次数
    want_jump_until = 0.0    # CTA点击后允许系统跳转的时间窗
    coins = 0
    loops = 0

    while True:
        loops += 1
        if args.max_loops and loops > args.max_loops:
            break
        try:
            f = focus()
            img = screenshot()
            if img is None:
                no_img += 1
                if no_img % 15 == 0:
                    log("截图失败, 检查设备连接...")
                    sh(["reconnect"], timeout=5)
                    devs = sh(["devices"])
                    log(f"adb devices: {devs.strip()}")
                time.sleep(2)
                continue
            no_img = 0
            words = ocr(img)
            text_all = " ".join(w.text for w in words)
            in_app = f.startswith(PKG)
            in_main = in_app and MAIN_HINT in f
            in_ad = in_app and not in_main
            now = time.time()

            # --- 0. 跳到外部APP(淘宝/应用市场等): 浏览15s(带下滑)再切回 ---
            if not in_app and f and not f.startswith(SYS_PKGS):
                log(f"外部APP: {f} 浏览15s后切回")
                browse_wait(15)
                relaunch_app()
                time.sleep(3)
                continue

            # --- 1. 落地页浏览中(点了CTA后的等待期, 边等边下滑) ---
            if now < browse_until:
                swipe(640, 2000, 640, 1200, 500)
                time.sleep(3)
                continue
            if browse_until and now >= browse_until:
                browse_until = 0
                # 等够浏览时长: 有新倒计时在走就继续等, 否则按返回
                if in_ad and countdown_left(words) is not None:
                    time.sleep(2)
                    continue
                back()
                time.sleep(2)
                continue

            # --- 2. 奖励已到账 -> 点跳过/关闭 退出广告 ---
            rd = next((w for w in find_all(words, REWARD_DONE) if w.y < 500), None)
            if rd:
                reward_done_at = now
                stuck_rescues = 0
                dead_backs = 0
                # 优先点顶部胶囊里的 跳过/关闭 文字(注意定位词内子串位置)
                w = next((w for w in find_all(words, SKIP_WORDS) if w.y < 600), None)
                if w:
                    key = next(k for k in SKIP_WORDS if k in w.text)
                    tap(sub_tap_x(w, key), w.y)
                else:
                    tap(1180, rd.y)  # 结算页标题右侧的X
                reward_done_clicks += 1
                if reward_done_clicks >= 3:
                    if reward_done_clicks >= 6:
                        log("连点6次没退出广告, 拉主界面硬退")
                        goto_home()
                        reward_done_clicks = 0
                        time.sleep(3)
                    else:
                        log("连点3次没退出广告, 按返回键")
                        back()
                    continue
                time.sleep(2)
                continue
            reward_done_clicks = 0

            # --- 2.5 安装授权弹窗(广告偷下APK) -> 永远禁止 ---
            if f.startswith("com.android.packageinstaller") or ("是否允许" in text_all and "安装" in text_all):
                w = find(words, INSTALL_DENY) or find(words, ["取消"])
                if w:
                    log(f"安装授权拒绝: {w.text}")
                    tap(w.x, w.y)
                else:
                    back()
                time.sleep(2)
                continue

            # --- 3. 系统拦截跳转框 ---
            # 刚点过CTA主动要跳 -> 仍要打开; 其余(广告自发乱跳/刚领完奖) -> 知道了取消
            if find(words, INTERCEPT, blacklist=False):
                if now < want_jump_until:
                    w = find(words, ALLOW_OPEN, blacklist=False)
                    if w:
                        log(f"放行跳转: {w.text}")
                        tap(w.x, w.y)
                        browse_wait(17)         # 外部APP浏览(含下滑)
                        relaunch_app()
                        time.sleep(3)
                        want_jump_until = 0
                    else:
                        time.sleep(1)
                else:
                    w = find(words, CANCEL_OPEN)
                    if w:
                        log(f"拦截框取消: {w.text}")
                        tap(w.x, w.y)
                    else:
                        back()
                    time.sleep(2)
                continue

            # --- 3.5 广告挽留弹窗(优先于倒计时解析, 否则"继续看21秒"被误读为cd) ---
            leave = find(words, LEAVE_AD, blacklist=False)
            if leave or "确定要退出" in text_all:
                if (abandon_ad or exit_stay_fails >= 2) and leave:
                    log(f"挽留弹窗 -> {leave.text} (离开广告)")
                    tap(leave.x, leave.y)
                    abandon_ad = False
                    exit_stay_fails = 0
                else:
                    stay = find(words, STAY_AD, blacklist=False)
                    if stay:
                        log(f"挽留弹窗 -> {stay.text} (留下继续看)")
                        tap(stay.x, stay.y)
                        exit_stay_fails += 1
                    elif leave:
                        tap(leave.x, leave.y)  # 没有留下按钮只能退
                        exit_stay_fails = 0
                    else:
                        back()
                last_cd = None
                last_cd_change = time.time()  # 留下后重新观察倒计时
                time.sleep(2)
                continue

            # --- 4. "继续观看"(下载拦截层) ---
            w = find(words, CONTINUE_WATCH)
            if w:
                log("点'继续观看'")
                tap(w.x, w.y)
                time.sleep(2)
                continue

            # --- 5. APP侧: 领奖弹窗 全部都要/开心收下/继续开红包(任意Activity都可能弹) ---
            if in_app:
                w = find(words, CLAIM_CHAIN)
                if w:
                    log(f"领奖弹窗: {w.text}")
                    tap(w.x, w.y)
                    last_cd = None
                    time.sleep(3)
                    continue
                if find(words, COIN_POPUP):
                    if text_all != last_coin_sig:
                        last_coin_sig = text_all
                        coin_close_tries = 0
                        m = re.search(r"\+(\d+)", text_all)
                        if m:
                            coins += int(m.group(1))
                            log(f"金币到账 +{m.group(1)} 累计 {coins}")
                    # 关闭位置: 识别到的×字符 > 右上角 > 弹窗下方 > 返回键
                    xw = next((w for w in words if w.text.strip(" Oo") in ("×", "X", "x", "✕") and w.y < 2000), None)
                    spots = [(1076, 325), (640, 2265), (1120, 632)]
                    if xw:
                        tap(xw.x, xw.y)
                    elif coin_close_tries < len(spots):
                        tap(*spots[coin_close_tries])
                    else:
                        back()
                    coin_close_tries += 1
                    time.sleep(2)
                    continue

            # --- 6. 广告页逻辑 ---
            if in_ad:
                # 福利弹窗 -> 放弃, 等倒计时领奖更快
                w = find(words, GIVE_UP)
                if w:
                    log(f"放弃福利弹窗: {w.text}")
                    tap(w.x, w.y)
                    time.sleep(2)
                    continue
                # 限时加速弹层: "我要更快拿奖"按钮 -> 浏览详情15s领奖(官方快速通道)
                # 注意: 标题"X秒更快拿奖"也会含关键词, 点标题无效, 须点按钮/标题下方按钮位
                cands = find_all(words, FAST_CLAIM) if fast_claim_tries < 2 else []
                w = next((w for w in cands if "我要" in w.text or "点我" in w.text
                          or "立即" in w.text), None)
                if w:
                    log(f"加速领奖弹层: {w.text}")
                    tap(w.x, w.y)
                elif cands:
                    log(f"加速领奖弹层(标题定位按钮): {cands[0].text}")
                    tap(cands[0].x, cands[0].y + 970)
                if w or cands:
                    fast_claim_tries += 1   # 跳外部(常为荣耀自家广告页)仍不领奖 -> 最多试2次
                    want_jump_until = now + 15
                    outcome = rescue_stuck_ad()
                    if outcome == "external":
                        browse_wait(17)
                        relaunch_app()
                        time.sleep(3)
                    elif outcome == "landing":
                        browse_until = now + 17
                    continue
                # 微信小程序落地窗 -> 下滑浏览再点它的X/返回
                if "MobRewardLoadPrompt" in f or ("小程序" in text_all and "通知" in text_all):
                    swipe(640, 1800, 640, 1100)
                    time.sleep(2)
                    xw = next((w for w in words if w.text.strip(" Oo") in ("×", "X", "x", "✕")
                               and w.y < 900), None)
                    if xw:
                        tap(xw.x, xw.y)
                    else:
                        tap(1086, 628)
                        time.sleep(1.5)
                        back()
                    time.sleep(2)
                    continue
                cd = countdown_left(words)
                if cd is not None and (last_cd is None or cd < last_cd):
                    last_cd_change = now   # 倒计时在走
                last_cd = cd

                if cd is None:
                    # cd=None但有"我要加速"弹层 -> 仍是卡死广告(顶部"去体验X秒"被OCR读丢), 点CTA救
                    cta = find(words, ["我要加速", "点我加速", "立即加速"])
                    if cta and stuck_rescues < 4:
                        log(f"卡死弹层(cd=None), 点CTA: {cta.text}")
                        tap(cta.x, cta.y)
                        want_jump_until = now + 15
                        outcome = rescue_stuck_ad()
                        if outcome == "external":
                            browse_wait(17)
                            relaunch_app()
                            time.sleep(3)
                        elif outcome == "landing":
                            browse_until = now + 17
                        stuck_rescues += 1
                        last_cd_change = time.time()
                        continue
                    if stuck_rescues >= 4:
                        if not abandon_ad:
                            log("弹层救援4次无效, 转退出扫描")
                        abandon_ad = True  # 保持计数, 此广告不再点CTA
                    # 无倒计时 = 结束卡/异常页: 只找X关, 不点CTA
                    sx, sy = AD_X_SPOTS[dead_backs % len(AD_X_SPOTS)]
                    log(f"广告结束卡, 试X位 ({sx},{sy})")
                    tap(sx, sy)
                    dead_backs += 1
                    if dead_backs % len(AD_X_SPOTS) == 0:
                        back()
                    if dead_backs >= 12:
                        log("X位全无效, 重启APP恢复")
                        abandon_ad = True   # 若是挽留弹窗挡住, 下轮点"坚持退出"
                        relaunch_app()
                        dead_backs = 0
                        time.sleep(4)
                    else:
                        time.sleep(1.5)
                    continue

                dead_backs = 0
                stuck = now - last_cd_change > args.stuck_sec
                if not stuck:
                    time.sleep(args.interval)
                    continue
                # 倒计时存在但卡死 -> 点CTA触发跳转救活
                cta = find(words, AD_CTA)
                if cta:
                    log(f"倒计时卡死(cd={cd}), 点CTA: {cta.text}")
                    tap(cta.x, cta.y)
                    if "上滑" in cta.text:
                        time.sleep(1)
                        swipe(cta.x, cta.y, cta.x, max(cta.y - 600, 300))
                    want_jump_until = now + 15  # 允许跳转窗口: 拦截框点仍要打开
                    outcome = rescue_stuck_ad()
                    if outcome == "external":
                        browse_wait(17)     # 需连续浏览满15s, 提前切回任务会失败
                        relaunch_app()
                        time.sleep(3)
                    elif outcome == "landing":
                        browse_until = now + 17
                    stuck_rescues += 1
                    last_cd = None
                    last_cd_change = time.time()
                    if stuck_rescues >= 4:
                        log("救援4次仍未领奖, 放弃这条广告")
                        abandon_ad = True      # 触发挽留弹窗时选"坚持退出"
                        w = find(words, SKIP_WORDS, blacklist=False)
                        if w:
                            tap(w.x, w.y)      # 顶部"跳过"无奖退出(常触发挽留弹窗)
                            time.sleep(1.5)
                        else:
                            tap(*AD_X_SPOTS[0])
                            time.sleep(1.2)
                        back()
                        time.sleep(2)
                        goto_home()            # 兜底: 拉主界面盖住赖着的广告页
                        time.sleep(3)
                        stuck_rescues = 0
                    continue
                # 卡死但没找到CTA -> 试上滑手势
                swipe(640, 2000, 640, 1200)
                time.sleep(2)
                continue

            # --- 7. APP侧: 任务中心按钮 ---
            if in_main:
                dead_backs = 0
                abandon_ad = False
                exit_stay_fails = 0
                fast_claim_tries = 0
                w = find(words, TASK_CLAIM)
                if w:
                    log(f"任务按钮: {w.text}")
                    tap(w.x, w.y)
                    last_cd = None
                    time.sleep(3)
                    continue
                # 里程碑"领取"(排除 已领取/倒计时数字类)
                ms = [w for w in find_all(words, TASK_MILESTONE)
                      if "已领取" not in w.text and not re.search(r"\d+\s*(分钟|秒|s)", w.text)]
                if ms:
                    w = ms[0]
                    log(f"里程碑领取: {w.text}")
                    tap(w.x, w.y)
                    time.sleep(3)
                    continue
                # 悬浮球 "再看X秒"/"领" -- 找右边缘小字
                bubble = [w for w in words if "再看" in w.text or w.text in ("领", "领取")]
                if bubble:
                    tap(bubble[0].x, bubble[0].y)
                    time.sleep(3)
                    continue
                # 确保在赚零花页
                w = find(words, TAB_EARN, blacklist=False)
                if w and w.y > 2400:  # 底部栏
                    log("切到赚零花页")
                    tap(w.x, w.y)
                    time.sleep(2)
                    continue

            time.sleep(args.interval)
        except subprocess.TimeoutExpired:
            log("adb 超时, 重试")
            time.sleep(3)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"异常: {e}")
            time.sleep(3)

    log(f"结束, 累计金币约 {coins}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
