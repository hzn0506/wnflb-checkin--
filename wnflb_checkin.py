# -*- coding: utf-8 -*-
"""
万能福利吧论坛 自动签到 v3.0
站点实测：Discuz! X / www.wnflb2023.com / cookie 前缀 S5r8_2132_

修复要点（对照 v2 报错"验证码填写错误"）：
  1. Discuz 登录验证码不是一道题，是三步链路，缺一步就报"验证码填写错误"：
       (a) GET 登录页           -> 拿 formhash / loginhash / idhash
       (b) GET misc.php?mod=seccode&action=update&idhash=IDHASH
                                -> 服务端 Set-Cookie: S5r8_2132_seccode<IDHASH>
                                   这一步不做，后面填什么都判错
       (c) GET misc.php?mod=seccode&update=UPD&idhash=IDHASH  -> PNG
       (d) POST 带上 seccodehash / seccodemodid / seccodeverify
     实测证据：走完整链路后用不存在账号提交，返回
       "登录失败，您还可以尝试 4 次"（loginperm:4）
     而不是"验证码填写错误"，证明验证码已被服务端接受。

  2. Cookie 域必须和请求域一致。本站主域是 www.wnflb2023.com，
     fuliba.net 是同站别名。cookie 跨域使用 = 直接判定未登录。

  3. Discuz 登录失败有次数闸门（loginperm 从 5 倒数），
     本脚本失败后立即停止，绝不连刷，避免账号被锁。

登录态判定：GET 首页，正则 discuz_uid = '(\\d+)'，非 0 即有效。
"""

import os
import re
import sys
import json
import time
import random
import logging
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, List
from urllib.parse import urljoin, urlparse, unquote

import requests
from requests.adapters import HTTPAdapter

# 北京时间（站点用 GMT+8，Actions 跑在 UTC，日期不能算错）
TZ8 = timezone(timedelta(hours=8))


def now8() -> datetime:
    return datetime.now(TZ8)


def today8() -> str:
    return now8().strftime("%Y-%m-%d")

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

BASE = os.environ.get("WNFLB_BASE") or os.environ.get("FORUM_BASE") or "https://www.wnflb2023.com"
BASE = BASE.rstrip("/")

# 变量名双兼容：仓库里原来配的是 FORUM_*，不改 secret 也能直接跑
USERNAME = (os.environ.get("WNFLB_USER") or os.environ.get("FORUM_USERNAME") or "").strip()
PASSWORD = (os.environ.get("WNFLB_PASS") or os.environ.get("FORUM_PASSWORD") or "").strip()
COOKIE_RAW = (os.environ.get("WNFLB_COOKIE") or os.environ.get("FORUM_COOKIE") or "").strip()

# Cookie 缓存文件：默认 cookies.json，兼容纯字符串 cookie.txt 和 JSON 字典两种格式。
# 公开仓库绝对不能提交 cookie —— 这里只落盘到 Actions cache，不进 git。
# utf-8-sig：吃掉 Windows 记事本 / PowerShell 写出来的 BOM，否则 cookie 名会带 \ufeff
COOKIE_FILE = (os.environ.get("WNFLB_COOKIE_FILE")
               or os.environ.get("COOKIE_FILE")
               or "cookies.json").strip()


def _flatten_cookie_json(obj, depth=0):
    """把缓存文件里读到的 JSON 压成 k=v 串。故意做得结构无关 —— 旧版脚本可能存成
    {"k":"v"}、{"cookies":{"k":"v"}}、{"cookies":"k=v; k=v"} 中的任意一种，
    读错了就等于没有缓存，会白白多登录一次。"""
    if depth > 4 or obj is None:
        return ""
    if isinstance(obj, str):
        s = obj.strip()
        return s if "=" in s or ";" in s else ""
    if isinstance(obj, dict):
        # 先试常见包装键
        for key in ("cookies", "cookie", "jar", "data", "value"):
            if key in obj and isinstance(obj[key], (dict, str)):
                got = _flatten_cookie_json(obj[key], depth + 1)
                if got:
                    return got
        # 再当作平铺的 {name: value}
        pairs = []
        for k, v in obj.items():
            if isinstance(v, str) and k and "=" not in str(k):
                pairs.append(f"{k}={v}")
        if pairs:
            return "; ".join(pairs)
        # 最后递归找
        for v in obj.values():
            got = _flatten_cookie_json(v, depth + 1)
            if got:
                return got
    if isinstance(obj, list):
        for v in obj:
            got = _flatten_cookie_json(v, depth + 1)
            if got:
                return got
    return ""


if not COOKIE_RAW and COOKIE_FILE and os.path.isfile(COOKIE_FILE):
    try:
        _raw = open(COOKIE_FILE, encoding="utf-8-sig").read().strip()
        if _raw.startswith("{") or _raw.startswith("["):
            _flat = _flatten_cookie_json(json.loads(_raw))
            if _flat:
                COOKIE_RAW = _flat
                _n = len([x for x in _flat.split(";") if "=" in x])
                print(f"[i] 已从 {COOKIE_FILE} 读取缓存 Cookie（{_n} 项）", flush=True)
            else:
                print(f"[!] {COOKIE_FILE} 里没解析出 cookie，按无缓存处理", flush=True)
        else:
            COOKIE_RAW = _raw
            print(f"[i] 已从 {COOKIE_FILE} 读取 Cookie（{len(COOKIE_RAW)} 字节）", flush=True)
    except Exception as _e:
        print(f"[!] 读 {COOKIE_FILE} 失败，按无缓存走登录: {_e}", flush=True)

# 若账号设了"安全问题"，填序号(1-7)与答案，否则留空
QUESTION_ID = (os.environ.get("WNFLB_QID") or "0").strip() or "0"
QUESTION_ANSWER = (os.environ.get("WNFLB_ANSWER") or "").strip()

# 通知：PushPlus 为主，Server 酱为辅
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()

# 自动回写 Cookie 到 GitHub Secret（可选，不用也不影响）
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
GH_SECRET_NAME = (os.environ.get("WNFLB_SECRET_NAME") or "WNFLB_COOKIE").strip()

# UA 必须和取 cookie 的浏览器一致：Discuz 的 _auth 密钥里混了 HTTP_USER_AGENT。
# 默认跟仓库旧版保持一致用 Chrome/120，避免换了 UA 把存量的 cookie 判成失效。
UA = os.environ.get("WNFLB_UA") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

VERSION = "v3.3"
RUN_ENV = "Actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "本地"

MAX_LOGIN_ATTEMPTS = 3        # 验证码 OCR 不可能 100%，给 3 次机会
LOGIN_TIMEOUT = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wnflb")


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------

def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    s.mount("https://", HTTPAdapter(max_retries=2))
    s.mount("http://", HTTPAdapter(max_retries=2))
    s.verify = False
    return s


def decode(resp: requests.Response) -> str:
    """按 header 声明的 charset 解码，绝不用「先试 utf-8 失败再转 gbk」的猜法。

    教训：本站页面里混了 GBK 字节的插件片段，整体 decode("utf-8") 会抛异常，
    旧的猜法就整页转到 gbk，中文全变 '鎮ㄤ粖鏃ュ凡绛惧埌' 这种乱码，关键词判定全废。
    """
    ct = resp.headers.get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    enc = m.group(1).lower() if m else ""
    if enc in ("gb2312", "gb18030"):
        enc = "gbk"

    if enc:
        try:
            return resp.content.decode(enc, "replace")
        except LookupError:
            pass

    for fallback in ("utf-8", "gbk"):
        try:
            return resp.content.decode(fallback)
        except UnicodeDecodeError:
            continue
    return resp.content.decode("utf-8", "replace")


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """兼容三种粘贴格式：
       1) k=v; k=v; k=v          （浏览器 DevTools / 抓包）
       2) Netscape cookies.txt   （导出插件）
       3) JSON {"k":"v"}
    """
    raw = (raw or "").lstrip("\ufeff").replace("\u200b", "").strip()
    if not raw:
        return {}

    # JSON
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            return {str(k): str(v) for k, v in obj.items()}
        except Exception:
            pass

    jar: Dict[str, str] = {}
    # Netscape 格式：一行 7 列，tab 分隔
    if "\t" in raw and len(raw.splitlines()) > 1:
        for line in raw.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                jar[parts[5]] = parts[6]
        if jar:
            return jar

    # k=v; k=v
    raw = raw.replace("\n", "; ").replace("\r", "")
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k, v = k.strip(), v.strip().strip('"')
        if k:
            jar[k] = v
    return jar


def cookie_to_string(jar: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


# ----------------------------------------------------------------------------
# 登录态
# ----------------------------------------------------------------------------

def get_uid(session: requests.Session) -> int:
    """GET 首页取 discuz_uid。0 = 未登录。这是最稳的判定，不触发任何敏感接口。"""
    try:
        r = session.get(BASE + "/", timeout=20)
        html = decode(r)
        m = re.search(r"discuz_uid\s*=\s*'?(\d+)", html)
        if m:
            return int(m.group(1))
        # 兜底：有些皮不输出 discuz_uid，看登录链接是否还在
        if "mod=logging&amp;action=login" in html or "mod=logging&action=login" in html:
            return 0
        m = re.search(r"home\.php\?mod=space&amp;uid=(\d+)", html)
        if m:
            return int(m.group(1))
    except Exception as e:
        log.warning("取登录态失败: %s", e)
    return 0


def get_formhash(session: requests.Session, url: str = "/") -> str:
    r = session.get(urljoin(BASE, url), timeout=20)
    m = re.search(r'name="formhash"\s+value="([0-9a-f]+)"', decode(r))
    return m.group(1) if m else ""


# ----------------------------------------------------------------------------
# Discuz 登录：完整验证码链路
# ----------------------------------------------------------------------------

class LoginError(Exception):
    pass


def _fetch_captcha(session: requests.Session, idhash: str) -> Optional[bytes]:
    """走完 seccode 三步中的 (b)(c)，返回验证码 PNG 字节。
       必须先在同一个 session 里调 action=update，服务端才会下发
       S5r8_2132_seccode<idhash> cookie；漏了这步 -> 验证码必错。
    """
    upd_url = (f"{BASE}/misc.php?mod=seccode&action=update"
               f"&idhash={idhash}&modid=member::logging&mobile=no")
    r1 = session.get(upd_url, timeout=20,
                     headers={"Referer": f"{BASE}/member.php?mod=logging&action=login"})
    txt = decode(r1)
    m = re.search(r"update=(\d+)", txt)
    if not m:
        log.warning("seccode update 未返回更新号，body=%r", txt[:160])
        return None

    img_url = (f"{BASE}/misc.php?mod=seccode&update={m.group(1)}"
               f"&idhash={idhash}&mobile=no")
    r2 = session.get(img_url, timeout=20,
                     headers={"Referer": f"{BASE}/member.php?mod=logging&action=login"})
    if "image" not in r2.headers.get("Content-Type", "").lower():
        log.warning("验证码不是图片: %s %r", r2.status_code, decode(r2)[:160])
        return None
    if len(r2.content) < 200:
        log.warning("验证码图片异常小: %d bytes", len(r2.content))
        return None
    global _LAST_CAPTCHA
    _LAST_CAPTCHA = r2.content
    return r2.content


_OCR = None
_LAST_CAPTCHA = None    # 最近一张验证码原图，登录失败时落盘，方便回看 OCR 到底错在哪


def dump_captcha(tag: str = "fail") -> None:
    """把最后一张验证码存下来。CI 里由 workflow 上传成 artifact。"""
    if _LAST_CAPTCHA:
        try:
            fn = f"captcha_{tag}.png"
            with open(fn, "wb") as f:
                f.write(_LAST_CAPTCHA)
            log.info("    已保存验证码样本 %s（%d 字节）", fn, len(_LAST_CAPTCHA))
        except Exception:
            pass


def _ocr(png: bytes) -> str:
    global _OCR
    if _OCR is None:
        try:
            import ddddocr
        except ImportError:
            raise LoginError("缺少 ddddocr，请先 pip install ddddocr")
        _OCR = ddddocr.DdddOcr(show_ad=False)
    # Discuz 校验 seccode 时 strtolower 比对，大小写不敏感，统一小写提交
    return _OCR.classification(png).strip().lower()


def login(session: requests.Session) -> Tuple[bool, str]:
    """账密登录。返回 (成功, 说明)。"""
    if not USERNAME or not PASSWORD:
        return False, "未配置账号密码"

    login_page = f"{BASE}/member.php?mod=logging&action=login"
    r = session.get(login_page, timeout=LOGIN_TIMEOUT)
    html = decode(r)

    m_fh = re.search(r'name="formhash"\s+value="([0-9a-f]+)"', html)
    m_lh = re.search(r"loginhash=([A-Za-z0-9]+)", html)
    m_id = re.search(r"updateseccode\('([A-Za-z0-9]+)'", html)
    if not (m_fh and m_lh):
        return False, "登录页结构异常，未取到 formhash/loginhash（可能被 WAF 拦或站点改版）"

    formhash = m_fh.group(1)
    loginhash = m_lh.group(1)
    need_captcha = bool(m_id)

    log.info("登录页 OK: formhash=%s loginhash=%s 验证码=%s",
             formhash, loginhash, "需要" if need_captcha else "不需要")

    post_url = (f"{BASE}/member.php?mod=logging&action=login"
                f"&loginsubmit=yes&loginhash={loginhash}&inajax=1")

    last_msg = ""
    last_png = None
    last_code = ""
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        seccode = ""
        if need_captcha:
            idhash = m_id.group(1)
            # OCR 结果不合规就直接换图，不占用一次 loginperm
            for grab in range(5):
                png = _fetch_captcha(session, idhash)
                if png is None:
                    return False, "取验证码图片失败"
                try:
                    seccode = _ocr(png)
                except LoginError as e:
                    return False, str(e)
                if re.fullmatch(r"[0-9a-z]{4}", seccode):
                    last_png, last_code = png, seccode
                    break
                log.warning("OCR 结果不合规 %r，换图(%d/5)", seccode, grab + 1)
                seccode = ""
                time.sleep(0.6)
            if not seccode:
                return False, "验证码连续 5 次识别不合规"
            log.info("第 %d 次 OCR: %s", attempt, seccode)
        else:
            idhash = ""

        data = {
            "formhash": formhash,
            "referer": BASE + "/",
            "loginfield": "username",
            "username": USERNAME,
            "password": PASSWORD,
            "questionid": QUESTION_ID,
            "answer": QUESTION_ANSWER,
            "cookietime": "2592000",          # 记住我 30 天
            "loginsubmit": "true",
        }
        if need_captcha:
            data.update({
                "seccodehash": idhash,
                "seccodemodid": "member::logging",
                "seccodeverify": seccode,
            })

        rr = session.post(post_url, data=data, timeout=LOGIN_TIMEOUT,
                          headers={
                              "X-Requested-With": "XMLHttpRequest",
                              "Referer": login_page,
                              "Origin": BASE,
                          })
        txt = decode(rr)

        # 成功：succeedhandle_login / 欢迎回来 / 积分转账页
        if "succeedhandle_login" in txt or "欢迎您回来" in txt or "欢迎回来" in txt:
            log.info("登录成功（第 %d 次）", attempt)
            return True, "账密登录成功"

        # 已经登录过
        if "您已经登录" in txt or "已经登录" in txt:
            return True, "账号已处于登录态"

        last_msg = _strip_xml(txt)

        # 只有明确说"验证码错"才换图重试（验证码过了说明是凭证问题，立即停，省 loginperm）
        if "验证码" in last_msg:
            log.warning("第 %d 次验证码被拒: %s", attempt, last_msg)
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(random.uniform(1.2, 2.4))
                r2 = session.get(login_page, timeout=LOGIN_TIMEOUT)
                mi = re.search(r"updateseccode\('([A-Za-z0-9]+)'", decode(r2))
                if mi:
                    m_id = mi
                continue
            # 全挂了：把最后一张验证码和 OCR 结果存下来，workflow 会当 artifact 上传
            if last_png:
                try:
                    with open("captcha_fail.png", "wb") as f:
                        f.write(last_png)
                    log.warning("    验证码原图已存 captcha_fail.png（OCR 识别为 %r）", last_code)
                except Exception:
                    pass
            return False, f"验证码连续 {MAX_LOGIN_ATTEMPTS} 次错误: {last_msg}"

        # 到这里说明验证码已通过（服务端进到了凭证校验），任何失败都立刻收手
        if "次数过多" in last_msg or "分钟后" in last_msg:
            return False, f"账号被临时锁（验证码没问题，是密码/次数问题）: {last_msg}"
        if "禁止" in last_msg or "锁定" in last_msg or "封" in last_msg:
            return False, f"触发风控/封禁: {last_msg}"
        if "安全问题" in last_msg or "安全提问" in last_msg:
            return False, f"账号设了安全问题，需配 WNFLB_QID / WNFLB_ANSWER: {last_msg}"
        return False, f"凭证校验未通过（验证码已通过）: {last_msg}"

    return False, f"登录失败: {last_msg}"


def _strip_xml(txt: str) -> str:
    """从 Discuz 的 <root><![CDATA[...]]></root> 里抠出人话。"""
    m = re.search(r"<!\[CDATA\[(.*?)\]\]>", txt, re.S)
    body = m.group(1) if m else txt
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    return re.sub(r"\s+", " ", body).strip()[:200]


# ----------------------------------------------------------------------------
# Cookie 回写 GitHub Secret
# ----------------------------------------------------------------------------

def update_github_secret(cookie_str: str) -> bool:
    """把新 cookie 写回 repo secret，实现 Cookie 长期自续。
       需要 GH_PAT：classic PAT 勾 repo，或 fine-grained PAT 勾 Secrets: Read and write。
    """
    if not (GH_PAT and GH_REPO):
        return False
    try:
        from nacl import encoding, public
    except ImportError:
        log.warning("未安装 PyNaCl，跳过 secret 回写（pip install pynacl）")
        return False

    api = f"https://api.github.com/repos/{GH_REPO}/actions/secrets"
    hdr = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        r = requests.get(f"{api}/public-key", headers=hdr, timeout=20)
        r.raise_for_status()
        key = r.json()

        pk = public.PublicKey(key["key"].encode("utf-8"), encoding.Base64Encoder())
        box = public.SealedBox(pk)
        sealed = box.encrypt(cookie_str.encode("utf-8"))
        enc_val = base64.b64encode(sealed).decode("utf-8")

        r2 = requests.put(
            f"{api}/{GH_SECRET_NAME}",
            headers=hdr,
            json={"encrypted_value": enc_val, "key_id": key["key_id"]},
            timeout=20,
        )
        if r2.status_code in (201, 204):
            log.info("Cookie 已回写 GitHub Secret [%s]", GH_SECRET_NAME)
            return True
        log.warning("回写 secret 失败: %s %s", r2.status_code, r2.text[:200])
    except Exception as e:
        log.warning("回写 secret 异常: %s", e)
    return False


# ----------------------------------------------------------------------------
# 签到
# ----------------------------------------------------------------------------

SIGN_LIST = f"{BASE}/plugin.php?id=fx_checkin:list"
SIGN_ACTION = f"{BASE}/plugin.php?id=fx_checkin:checkin"
SIGN_RESIGN = f"{BASE}/plugin.php?id=fx_checkin:resign"


def _page_message(html: str) -> str:
    """从 Discuz 提示页里抠出人话正文。"""
    m = re.search(r'id="messagetext"[^>]*>(.*?)</div>', html, re.S)
    if not m:
        m = re.search(r'<div class="alert_(?:info|error|right)"[^>]*>(.*?)</div>', html, re.S)
    body = m.group(1) if m else html
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def _sign_stats(html: str) -> Dict[str, str]:
    """抽连续/累计签到天数，推送里带上更有用。"""
    out = {}
    m = re.search(r"连续签到\s*(\d+)\s*天", html)
    if m:
        out["连续"] = m.group(1)
    m = re.search(r"累计签到\s*(\d+)\s*天", html)
    if m:
        out["累计"] = m.group(1)
    m = re.search(r"今天已有\s*(\d+)\s*人签到", html)
    if m:
        out["今日全站"] = m.group(1)
    return out


def checkin(session: requests.Session) -> Tuple[bool, str]:
    """实测端点（fx_checkin 插件）：
         GET plugin.php?id=fx_checkin:list          -> 拿 formhash + 当前天数
         GET plugin.php?id=fx_checkin:checkin&formhash=XXXXXXXX&XXXXXXXX
                                                    -> 签到（第二个 hash 是 Discuz 防 CSRF 惯例）

    重要：这里【绝不做预判就跳过】。
    这个接口是幂等的 ——
        没签 -> "签到成功！今日第 N 个签到"
        签过 -> "您今日已签到，无需重复签到！"
    所以每次无脑打一次，让服务端回答。之前靠页面 alt 图标预判，
    结果把"未签到"误读成"已签到"，直接跳过提交，连着漏签好几天。
    """
    # 1) 打开签到页拿 formhash
    r = session.get(SIGN_LIST, timeout=25, headers={"Referer": f"{BASE}/"})
    html = decode(r)

    if "mod=logging&amp;action=login" in html and "member.php?mod=logging" in html:
        return False, "签到页要求登录 -> Cookie 没生效"

    m = re.search(r"formhash=([0-9a-f]{8})", html)
    if not m:
        m = re.search(r'name="formhash"\s+value="([0-9a-f]{8})"', html)
    if not m:
        plain = _page_message(html)
        return False, f"未取到 formhash：{plain[:160]}"
    formhash = m.group(1)

    before = _sign_stats(html)
    before_streak = before.get("连续")

    # 2) 无预判，直接提交
    url = f"{SIGN_ACTION}&formhash={formhash}&{formhash}"
    r2 = session.get(url, timeout=25,
                     headers={"Referer": SIGN_LIST,
                              "X-Requested-With": "XMLHttpRequest"})
    msg = _page_message(decode(r2)) or _strip_xml(decode(r2))

    if not msg:
        return False, f"签到接口无正文，HTTP {r2.status_code}"

    # 3) 重新拉签到页，用连续天数做硬核验
    after = {}
    try:
        r3 = session.get(SIGN_LIST, timeout=25, headers={"Referer": SIGN_LIST})
        after = _sign_stats(decode(r3))
    except Exception:
        pass
    after_streak = after.get("连续")
    stat_txt = "（连续 {} 天 / 累计 {} 天）".format(
        after.get("连续", before.get("连续", "?")),
        after.get("累计", before.get("累计", "?")))

    grew = (isinstance(before_streak, int) and isinstance(after_streak, int)
            and after_streak > before_streak)

    if "已经签到" in msg or "无需重复" in msg or "重复签到" in msg:
        # 服务端明确说今天签过了
        if grew:
            log.info("    连续天数 %s -> %s，确认生效", before_streak, after_streak)
        return True, f"今日已签到（服务端确认）{stat_txt}"

    if "成功" in msg or "恭喜" in msg or "奖励" in msg or "第" in msg:
        if grew:
            return True, f"{msg}〔连续天数 {before_streak}→{after_streak} 已核验〕{stat_txt}"
        # 服务端说成功但天数没变 —— 仍然算成功，但日志要说清楚
        log.warning("    接口返回成功，但连续天数未见变化（%s -> %s）", before_streak, after_streak)
        return True, f"{msg}（+未观察到天数增长，已记录）{stat_txt}"

    if "未登录" in msg or "请先登录" in msg:
        return False, msg
    if "关闭" in msg or "禁止" in msg or "无权" in msg:
        return False, msg

    return False, f"未识别的签到返回：{msg[:200]}"


# ----------------------------------------------------------------------------
# 签到状态核验 + 历史台账
# ----------------------------------------------------------------------------

STATE_FILE = os.environ.get("WNFLB_STATE_FILE", "state.json").strip()


def read_sign_status(session: requests.Session) -> Dict[str, object]:
    """读真实签到状态。权威来源是【首页】，不是签到页 ——
    fx_checkin 的今日状态图标只挂在首页导航里，签到页里只有排行榜。

    首页有两处可用信号：
      <img id="fx_checkin_b" ... alt="已签到">
      <div class="tip_c">今天已有9194人签到,您已连续签到121天,累计签到1085天!</div>
    外加 cookie S5r8_2132_lastcheckfeed = uid|最后签到时间戳
    """
    st: Dict[str, object] = {
        "signed": False, "streak": None, "total": None,
        "last_ts": None, "last_date": None, "ok": False, "uid": 0,
    }
    try:
        r = session.get(BASE + "/", timeout=25)
        html = decode(r)

        m = re.search(r"discuz_uid\s*=\s*'?(\d+)", html)
        uid = int(m.group(1)) if m else 0
        st["uid"] = uid
        if not uid:
            return st

        st["ok"] = True
        st["signed"] = bool(re.search(r'alt="已签到"', html))
        # 未签到时 alt 是别的字样，明确点名一下，避免误判
        if not st["signed"] and re.search(r'alt="(点击签到|立即签到|我要签到)"', html):
            st["signed"] = False

        m = re.search(r"连续签到\s*(\d+)\s*天", html)
        if m:
            st["streak"] = int(m.group(1))
        m = re.search(r"累计签到\s*(\d+)\s*天", html)
        if m:
            st["total"] = int(m.group(1))

        # 注意：cookie S5r8_2132_lastcheckfeed 是"最后一次打开签到页"的时间戳，
        # 不是签到时间。每次访问都会刷新，拿它判断"今天签没签"必然恒等于今天，
        # 于是永远跳过提交 —— 这就是 11-18 到 11-22 连续漏签的真凶。
        # 这里只留作日志参考，绝不参与签到判定。
        for c in session.cookies:
            if c.name.endswith("lastcheckfeed"):
                parts = unquote(c.value).split("|")
                if len(parts) >= 2 and parts[1].isdigit():
                    st["last_view_ts"] = int(parts[1])
                break
    except Exception as e:
        log.warning("读签到状态失败: %s", e)
    return st


def verify_signed_today(st: Dict[str, object], before_streak=None) -> bool:
    """事后核验：今天到底签没签。

    只认两个硬信号：
      1. 连续天数比提交前涨了（最硬）
      2. 页面出现 alt="已签到"，且天数不低于 before_streak
    lastcheckfeed 一律不用 —— 它是浏览时间不是签到时间。
    """
    if not st.get("ok"):
        return False
    streak = st.get("streak")
    if isinstance(before_streak, int) and isinstance(streak, int) and streak > before_streak:
        return True
    if st.get("signed"):
        if isinstance(before_streak, int) and isinstance(streak, int):
            return streak >= before_streak
        return True
    return False


def load_state() -> Dict[str, object]:
    try:
        if os.path.isfile(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8-sig") as f:
                return json.load(f) or {}
    except Exception as e:
        log.warning("读台账失败（按空台账处理）: %s", e)
    return {}


def save_state(st: Dict[str, object]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        log.info("[5] 台账已更新 %s", STATE_FILE)
    except Exception as e:
        log.warning("写台账失败: %s", e)


def gap_days(last_ok: str) -> int:
    """上次成功签到到今天隔了几天。0=今天，1=昨天（正常），>=2 就是断了。"""
    try:
        d0 = datetime.strptime(last_ok, "%Y-%m-%d").date()
        d1 = now8().date()
        return (d1 - d0).days
    except Exception:
        return -1


def hours_since_last_run(state: Dict[str, object]) -> Optional[float]:
    ts = state.get("last_run_ts")
    if not isinstance(ts, (int, float)):
        return None
    return (time.time() - ts) / 3600.0


# ----------------------------------------------------------------------------
# 通知
# ----------------------------------------------------------------------------

def notify(title: str, content: str) -> None:
    """双通道推送：PushPlus 为主，Server 酱为辅。配了哪个发哪个，都配就都发。"""
    if not PUSHPLUS_TOKEN and not SERVERCHAN_KEY:
        log.info("未配置任何推送通道，跳过推送")
        return

    if PUSHPLUS_TOKEN:
        try:
            r = requests.post(
                "https://www.pushplus.plus/send",
                json={"token": PUSHPLUS_TOKEN, "title": title, "content": content,
                      "template": "txt"},
                timeout=20,
            )
            try:
                j = r.json()
            except Exception:
                j = {}
            if r.status_code == 200 and str(j.get("code")) == "200":
                log.info("PushPlus 推送成功 msgId=%s", j.get("data") or "-")
            else:
                log.warning("PushPlus 返回异常 %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("PushPlus 推送失败: %s", e)

    if SERVERCHAN_KEY:
        try:
            sub = title
            desp = content
            # SendKey 以 sctp 开头是 Server酱³，走不同的接口
            if SERVERCHAN_KEY.startswith("sctp"):
                url = f"https://{SERVERCHAN_KEY.split('t')[1][:5]}.push.ft07.com/send/{SERVERCHAN_KEY}.send"
            else:
                url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
            r = requests.post(url, data={"title": sub[:100], "desp": desp}, timeout=20)
            try:
                j = r.json()
                ok = j.get("code") in (0, 200) or j.get("data", {}).get("errno") == 0
            except Exception:
                ok = r.status_code == 200
            if ok:
                log.info("Server酱 推送成功")
            else:
                log.warning("Server酱 返回异常 %s: %s", r.status_code, r.text[:160])
        except Exception as e:
            log.warning("Server酱 推送失败: %s", e)


def mark_refreshed() -> None:
    """把 refreshed=true 写给 GitHub Actions，workflow 据此判断要不要刷新 cookie 缓存。"""
    p = os.environ.get("GITHUB_OUTPUT")
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("refreshed=true\n")
        log.info("[out] refreshed=true")
    except Exception as e:
        log.warning("写 GITHUB_OUTPUT 失败: %s", e)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main() -> int:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info("=" * 52)
    log.info("  万能福利吧签到 %s  · %s  · %s", VERSION, RUN_ENV, started)
    log.info("  站点: %s", BASE)
    log.info("=" * 52)

    session = new_session()
    uid = 0
    cookie_source = "无"

    # --- L1: Cookie ---
    if COOKIE_RAW:
        jar = parse_cookie_string(COOKIE_RAW)
        log.info("[1] 载入 Cookie %d 项，校验登录态 ...", len(jar))

        has_auth = any(k.endswith("_auth") for k in jar)
        has_salt = any(k.endswith("_saltkey") for k in jar)

        if not has_auth:
            log.error("    ✗ Cookie 缺 _auth —— Discuz 登录态只认这一个字段")
            log.error("      它是 HttpOnly，document.cookie 与多数导出插件都拿不到")
            log.error("      解决：装 tampermonkey/wnflb-cookie-push.user.js，")
            log.error("            打开论坛点右下角 🍪，点「复制全部」")
            log.error("      或 DevTools > Application > Cookies 搜 auth 手动复制")
            log.error("      本次跳过 Cookie 分支")
        elif not has_salt:
            log.warning("    Cookie 缺 _saltkey，可能拼得不完整，按失效处理")

        other = [k for k in jar if "_2132_" not in k]
        if other:
            log.info("    非 Discuz 前缀项（无害）: %s", other)

        if has_auth:
            for k, v in jar.items():
                session.cookies.set(k, v, domain=urlparse(BASE).hostname, path="/")
            uid = get_uid(session)
            if uid:
                log.info("    -> Cookie 有效, uid=%d", uid)
                cookie_source = "Cookie"
            else:
                log.warning("    -> Cookie 不可用（域名不对 / UA 不一致 / 已过期）")
                # 关键：把脏 cookie 彻底清掉再登录。
                # 否则旧的 saltkey / sid 会残留，登录成功后写出的新 cookie 会混进死字段，
                # 下次用直接废掉（实测过：saltkey=EXPIREDxx 被带到了新 cookie 里）。
                session.cookies.clear()

    # --- L2: 账密 ---
    if not uid:
        log.info("[2] 使用账号密码登录 ...")
        ok, msg = login(session)
        log.info("    %s: %s", "成功" if ok else "失败", msg)
        if not ok:
            dump_captcha("fail")
            # 带上诊断，别让「验证码错误」四个字糊住真正的原因
            has_cookie_secret = bool((os.environ.get("WNFLB_COOKIE")
                                      or os.environ.get("FORUM_COOKIE") or "").strip())
            has_cache = os.path.isfile(COOKIE_FILE)
            diag = [
                f"登录失败：{msg}",
                f"版本：{VERSION} · {RUN_ENV}",
                f"站点：{BASE}",
                "",
                "── 现场 ──",
                f"缓存文件 {COOKIE_FILE}：{'有' if has_cache else '没有'}",
                f"secret FORUM_COOKIE：{'配了' if has_cookie_secret else '没配'}",
                "",
                "── 为什么在 Actions 里会这样 ──",
                "GitHub 的机器在境外，这个站要求境外 IP 每次登录都过验证码。",
                "验证码靠图像识别，识别错一两次就整次失败，这就是连续失败的原因。",
                "",
                "── 怎么彻底解决（做一次，以后不再登录）──",
                "在浏览器登录论坛 → F12 → Application → Cookies → www.wnflb2023.com",
                "找到 S5r8_2132_auth 和 S5r8_2132_saltkey，复制两段 Value，拼成一行：",
                "  S5r8_2132_auth=xxx; S5r8_2132_saltkey=yyy",
                "存到 仓库 → Settings → Secrets and variables → Actions → FORUM_COOKIE",
                "填好之后 CI 就只用 Cookie，一次验证码都不用过。",
                "",
                "── 今天别漏签 ──",
                "打开论坛页面（装了 tampermonkey/wnflb-auto-sign.user.js 会自动签），",
                "或者手动去 plugin.php?id=fx_checkin:list 点一下签到。",
            ]
            notify(f"❌ 福利吧登录失败 〔{VERSION} · {RUN_ENV}〕", "\n".join(diag))
            log.error("[FAIL] %s", msg)
            return 1
        uid = get_uid(session)
        if not uid:
            notify(f"❌ 福利吧登录失败 〔{VERSION} · {RUN_ENV}〕",
                   "登录接口返回成功但取不到 uid，Cookie 没落地。今天还没签到。")
            log.error("[FAIL] 登录接口通了但 uid 仍为 0，检查域名/代理")
            return 1
        cookie_source = "账密登录"
        log.info("    新 uid=%d", uid)

    # --- 签到前：读历史台账，算调度是否掉链子 ---
    state = load_state()
    prev_last_ok = state.get("last_ok") if isinstance(state.get("last_ok"), str) else None
    prev_streak = state.get("streak") if isinstance(state.get("streak"), int) else None
    prev_gap = gap_days(prev_last_ok) if prev_last_ok else -1
    stale_hours = hours_since_last_run(state)
    if stale_hours is not None and stale_hours > 30:
        log.warning("[!] 距上次运行 %.1f 小时 —— 定时任务没按点跑，检查 Actions 是否被禁用", stale_hours)
    log.info("[3] 执行签到 ...")
    signed = False
    msg = ""
    before: Dict[str, object] = {}
    after: Dict[str, object] = {}

    try:
        # 先读一次基线（只为拿连续天数做前后对比），不拿它决定要不要签。
        before = read_sign_status(session)
        before_streak = before.get("streak")
        log.info("    签到前：%s", "已签到" if before.get("signed") else "未签到")
        log.info("    连续天数基线：%s", before_streak if before_streak is not None else "未知")

        # 无条件打接口。它是幂等的：
        #   没签 -> 签到成功；签过 -> "您今日已签到，无需重复签到"
        # 靠预判跳过提交害过人，不再干这种聪明事。
        signed, msg = checkin(session)

        # 事后核验
        after = read_sign_status(session)
        verified = verify_signed_today(after, before_streak)
        if verified:
            signed = True
            if not msg:
                msg = "签到成功（事后核验通过）"
        elif signed:
            # 接口说成功但核验没过 —— 保留接口结论，但日志写明
            log.warning("    接口报成功，但页面核验未通过，请人工看一眼")
            msg = f"{msg}（页面核验未通过，建议人工确认）"
        else:
            msg = f"{msg}"
    except Exception as e:
        signed, msg = False, f"签到异常: {type(e).__name__}: {e}"

    streak = after.get("streak") if isinstance(after.get("streak"), int) else prev_streak
    total = after.get("total") if isinstance(after.get("total"), int) else state.get("total")
    stat_txt = f"（连续 {streak} 天 / 累计 {total} 天）" if streak else ""
    # checkin() 的返回里可能已经带了天数，别叠两次
    log.info("    %s%s", msg, "" if "连续" in str(msg) else stat_txt)

    # --- 漏签判定 ---
    # 原则：站点的连续天数(streak)是权威数据，只要拿得到就以它为准；
    # 日期差(last_ok)只是估算，只在拿不到 streak 时才用。
    # 踩过的坑：11-19 记的 last_ok，11-20 别处签上了，11-21 一跑就按日期差判成"断档"，
    # 但连续天数从 123 涨到 125 明明没断 —— 误报比不报更烦人。
    broken = False
    broken_reason = ""
    have_streak = isinstance(streak, int) and isinstance(prev_streak, int)

    if signed:
        if have_streak:
            # 有连续天数可比：只信它
            if streak < prev_streak:
                broken = True
                if streak == 1 and prev_streak > 1:
                    broken_reason = f"连续天数从 {prev_streak} 重置为 1，中间断过"
                else:
                    broken_reason = f"连续天数由 {prev_streak} 掉到 {streak}，中间断过"
            # streak >= prev_streak -> 没断，哪怕 last_ok 隔了好几天
        elif prev_gap >= 2:
            # 拿不到连续天数，只能按日期差估
            broken = True
            broken_reason = f"上次成功签到是 {prev_last_ok}，到今天隔了 {prev_gap} 天"
    elif prev_gap >= 1:
        # 今天还没签上，上次也不是今天 -> 已经在漏
        broken = True
        broken_reason = f"今天没签上，上次成功是 {prev_last_ok}"

    # --- Cookie 自续 ---
    jar_new = {c.name: c.value for c in session.cookies}
    new_cookie = cookie_to_string(jar_new)
    rotated = False
    if cookie_source == "账密登录" and new_cookie:
        # 落盘成 JSON 字典，和仓库旧版 cookies.json 格式一致，workflow 用 actions/cache 存，
        # 绝不进 git —— 这个仓库是公开的，cookie 明文提交等于把账号送人。
        try:
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(jar_new, f, ensure_ascii=False, indent=2)
                f.write("\n")
            log.info("[4] 新 Cookie 已写入 %s（%d 项）", COOKIE_FILE, len(jar_new))
        except Exception as e:
            log.warning("    写 %s 失败: %s", COOKIE_FILE, e)
        # 通知 workflow：本次重新登录过，缓存要换新的
        mark_refreshed()
        if signed:
            rotated = update_github_secret(new_cookie)

    # --- 更新台账 ---
    history = state.get("history") if isinstance(state.get("history"), list) else []
    entry = {
        "date": today8(),
        "at": now8().strftime("%H:%M:%S"),
        "signed": bool(signed),
        "streak": streak,
        "total": total,
        "source": cookie_source,
    }
    # 同一天多次运行只留最后一条
    history = [h for h in history if not (isinstance(h, dict) and h.get("date") == today8())]
    history.append(entry)
    history = history[-60:]

    # 推送决策必须在改写 notified_today 之前算，否则永远判定为"已推过"
    already_notified = state.get("notified_today") == today8() and not broken

    state.update({
        "last_ok": today8() if signed else prev_last_ok,
        "streak": streak,
        "total": total,
        "last_run_ts": int(time.time()),
        "last_run_date": today8(),
        "history": history,
    })

    # --- 收尾 & 分级推送 ---
    lines = [
        f"账号: {USERNAME or '(Cookie 模式)'}",
        f"uid : {uid}",
        f"来源: {cookie_source}",
        f"签到: {'成功' if signed else '失败'} — {msg}",
    ]
    if streak:
        lines.append(f"连续: {streak} 天    累计: {total} 天")
    if broken:
        lines.append("")
        lines.append(f"⚠️ 检测到断档：{broken_reason}")
    if stale_hours is not None and stale_hours > 30:
        lines.append("")
        lines.append(f"⚠️ 上次运行距今 {stale_hours:.1f} 小时，定时任务可能没按时触发")
    if rotated:
        lines.append("")
        lines.append(f"Cookie 已自动续期到 secret {GH_SECRET_NAME}")
    elif cookie_source == "账密登录" and new_cookie:
        # 只挑 Discuz 认的那两项，拼成能直接贴进 FORUM_COOKIE 的一行
        jar_pick = {}
        for c in session.cookies:
            if c.name.endswith("_auth") or c.name.endswith("_saltkey") or c.name.endswith("_sid"):
                jar_pick[c.name] = c.value
        secret_line = "; ".join(f"{k}={v}" for k, v in jar_pick.items())
        lines.append("")
        if RUN_ENV == "本地" and secret_line:
            lines.append("── 把下面这一整行贴进 GitHub Secret FORUM_COOKIE ──")
            lines.append(secret_line)
            lines.append("（仓库 Settings → Secrets and variables → Actions → FORUM_COOKIE）")
            lines.append("贴好之后，Actions 就只走 Cookie，再也不用过验证码。")
        else:
            lines.append("新 Cookie 已更新到缓存，本次没能自动写 secret。")
            lines.append("如果 Action 连续失败，手动配一份 FORUM_COOKIE 能一劳永逸。")

    body = "\n".join(lines)
    print("\n" + "=" * 52)
    print(body)
    print("=" * 52)
    if not rotated and cookie_source == "账密登录" and RUN_ENV == "本地":
        print("\n" + "*" * 52)
        print("  上面那行 FORUM_COOKIE=... 建议现在就复制走。")
        print("  填进仓库 secret 后，Actions 完全不用登录。")
        print("*" * 52)

    # 推送策略：断档 / 失败 -> 必推并告警；当天首次成功 -> 推；当天重复跑且一切正常 -> 静默
    # 所有标题都带版本号和运行环境，收到就知道是哪份脚本发的
    tag = f"〔{VERSION} · {RUN_ENV}〕"
    if broken:
        notify(f"⚠️ 福利吧漏签了 {tag}", body)
        state["notified_today"] = today8()
    elif not signed:
        notify(f"❌ 福利吧签到失败 {tag}", body)
        state["notified_today"] = today8()
    elif already_notified:
        log.info("[6] 今天已经推过，正常重复运行不骚扰")
    else:
        notify(f"福利吧签到成功 {tag}", body)
        state["notified_today"] = today8()

    save_state(state)
    return 0 if signed else 1


if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings()
    sys.exit(main())
