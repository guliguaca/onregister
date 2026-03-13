import json
import os
import re
import sys
import time
import uuid
import random
import string
import secrets
import hashlib
import base64
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Any, Dict, Optional, List
import urllib.parse
import urllib.request
import urllib.error

from curl_cffi import requests
from gptmail_client import GPTMailClient, GPTMailAPIError

OUT_DIR = Path(__file__).parent.resolve()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# ========== GPTMail helpers ==========

def _realistic_email_prefix() -> str:
    first_names = ["john", "michael", "david", "james", "robert", "daniel", "chris", "mark", "paul", "kevin", "andrew", "brian", "steven", "thomas", "anthony", "peter", "jason", "ryan", "nathan", "alex", "matthew", "charles", "ben", "adam", "josh"]
    last_names = ["smith", "johnson", "brown", "williams", "jones", "miller", "davis", "garcia", "rodriguez", "wilson", "martin", "lee", "thompson", "white", "king", "wright", "green", "clark", "baker", "hill", "young", "allen", "scott", "cooper", "turner"]
    middle = random.choice(["alex", "jordan", "taylor", "morgan", "lee", "reese", "blake", "quinn"])
    f = random.choice(first_names)
    l = random.choice(last_names)
    # 不含数字，附加一个短随机后缀提升唯一性
    tail = random.choice(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"])
    return f"{f}.{middle}.{l}.{tail}"


def get_email_and_code_fetcher(proxies: Any = None):
    base_url = os.environ.get("GPTMAIL_BASE_URL", "https://mail.chatgpt.org.uk")
    api_key = os.environ.get("GPTMAIL_API_KEY", "").strip() or "gpt-test"
    timeout = float(os.environ.get("GPTMAIL_TIMEOUT", "30") or 30)
    prefix = os.environ.get("GPTMAIL_PREFIX") or _realistic_email_prefix()
    domain = os.environ.get("GPTMAIL_DOMAIN") or None
    if not api_key:
        raise RuntimeError("GPTMAIL_API_KEY is required for registration")
    client = GPTMailClient(base_url, api_key, timeout=timeout)
    email = client.generate_email(prefix=prefix, domain=domain)

    def fetch_code(timeout_sec: int = 180, poll: float = 5.0) -> str | None:
        regex = r"(?<!\d)(\d{6})(?!\d)"
        start = time.monotonic()
        seen: set[str] = set()
        attempt = 0
        while time.monotonic() - start < timeout_sec:
            attempt += 1
            try:
                summaries = client.list_emails(email)
                print(f"[otp] poll #{attempt}, got {len(summaries)} emails, seen={len(seen)}, email={email}")
            except GPTMailAPIError as e:
                print(f"[otp] list_emails error: {e}")
                summaries = []
            for summary in summaries:
                # 优先从 summary 的 subject/内容抓验证码
                subj = str(summary.get("subject") or "")
                m = re.search(regex, subj)
                if m:
                    code_found = m.group(1)
                    print(f"[otp] code from subject: {code_found}")
                    return code_found

                email_id = _extract_email_id(summary)
                if not email_id or email_id in seen:
                    continue
                print(f"[otp] new email id: {email_id}")
                seen.add(email_id)
                try:
                    detail = client.get_email(email_id)
                except GPTMailAPIError as e:
                    print(f"[otp] get_email error: {e}")
                    continue
                blob_parts = _iter_strings(summary) + _iter_strings(detail)
                blob = "\n".join(blob_parts)
                preview = blob[:400].replace("\n", " ")
                print(f"[otp] email preview: {preview}")
                # 尝试多种模式
                m = re.search(regex, blob)
                if not m:
                    spaced = re.search(r"(\d\s+\d\s+\d\s+\d\s+\d\s+\d)", blob)
                    if spaced:
                        candidate = re.sub(r"\s+", "", spaced.group(1))
                        m = re.match(r"(\d{6})", candidate)
                if not m:
                    digits_only = re.sub(r"\D", "", blob)
                    long_match = re.search(r"(\d{6})", digits_only)
                    if long_match:
                        m = long_match
                if m:
                    code_found = m.group(1)
                    print(f"[otp] code found: {code_found}")
                    return code_found
                else:
                    print("[otp] no code in this email")
            time.sleep(poll)
        print(f"[otp] timeout waiting for code for email {email}")
        return None

    password = _gen_password()
    return email, password, fetch_code

def _iter_strings(obj: Any) -> list[str]:
    out: list[str] = []
    def _walk(v: Any):
        if v is None:
            return
        if isinstance(v, str):
            if v: out.append(v); return
        if isinstance(v, bytes):
            try: s = v.decode("utf-8", errors="replace")
            except Exception: return
            if s: out.append(s); return
        if isinstance(v, dict):
            for vv in v.values(): _walk(vv); return
        if isinstance(v, (list, tuple)):
            for vv in v: _walk(vv); return
    _walk(obj)
    return out

def _extract_email_id(summary: dict[str, Any]) -> str | None:
    for key in ("id","_id","email_id","emailId","message_id","messageId","mail_id","mailId"):
        v = summary.get(key)
        if v is None: continue
        s = str(v).strip()
        if s: return s
    return None

# ========== OAuth helpers ==========

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"

def _gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    special = "!@#$%^&*.-"
    base = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice(special),
    ]
    base += [random.choice(alphabet + special) for _ in range(12)]
    random.shuffle(base)
    return "".join(base)

def _random_name() -> str:
    letters = string.ascii_lowercase
    n = random.randint(5, 9)
    s = ''.join(random.choice(letters) for _ in range(n))
    return s.capitalize()

def _random_birthdate() -> str:
    start = datetime(1970,1,1); end = datetime(1999,12,31)
    delta = end - start
    d = start + timedelta(days=random.randrange(delta.days + 1))
    return d.strftime('%Y-%m-%d')

def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def _sha256_b64url_no_pad(s: str) -> str:
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())

def _random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)

def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)

def _parse_callback_url(callback_url: str) -> Dict[str, Any]:
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "","state": "","error": "","error_description": ""}
    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif any(ch in candidate for ch in "/?#") or ":" in candidate:
            candidate = f"http://{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for key, values in fragment.items():
        if key not in query or not query[key] or not (query[key][0] or "").strip():
            query[key] = values
    def get1(k: str) -> str:
        v = query.get(k, [""])
        return (v[0] or "").strip()
    code = get1("code"); state = get1("state")
    error = get1("error"); error_description = get1("error_description")
    if code and not state and "#" in code:
        code, state = code.split("#",1)
    if not error and error_description:
        error, error_description = error_description, ""
    return {"code": code,"state": state,"error": error,"error_description": error_description}

def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}

def _decode_jwt_segment(seg: str) -> Dict[str, Any]:
    raw = (seg or "").strip()
    if not raw: return {}
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    try:
        decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}

def _to_int(v: Any) -> int:
    try: return int(v)
    except (TypeError, ValueError): return 0

def _post_form(url: str, data: Dict[str, str], timeout: int = 30) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded","Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"token exchange failed: {resp.status}: {raw.decode('utf-8','replace')}")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(f"token exchange failed: {exc.code}: {raw.decode('utf-8','replace')}") from exc

@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str

def generate_oauth_url(*, redirect_uri: str = DEFAULT_REDIRECT_URI, scope: str = DEFAULT_SCOPE) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    return OAuthStart(auth_url=auth_url, state=state, code_verifier=code_verifier, redirect_uri=redirect_uri)

def fetch_sentinel_token(*, flow: str, did: str, proxies: Any = None) -> Optional[str]:
    try:
        body = json.dumps({"p": "", "id": did, "flow": flow})
        resp = requests.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers={
                "origin": "https://sentinel.openai.com",
                "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                "content-type": "text/plain;charset=UTF-8",
            },
            data=body,
            proxies=proxies,
            impersonate="chrome",
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[Error] Sentinel flow={flow} 状态码: {resp.status_code}")
            try:
                print(resp.text)
            except Exception:
                pass
            return None
        return resp.json().get("token")
    except Exception as e:
        print(f"[Error] Sentinel flow={flow} 获取失败: {e}")
        return None

def submit_callback_url(*, callback_url: str, expected_state: str, code_verifier: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
    cb = _parse_callback_url(callback_url)
    if cb["error"]:
        desc = cb["error_description"]
        raise RuntimeError(f"oauth error: {cb['error']}: {desc}".strip())
    if not cb["code"]:
        raise ValueError("callback url missing ?code=")
    if not cb["state"]:
        raise ValueError("callback url missing ?state=")
    if cb["state"] != expected_state:
        raise ValueError("state mismatch")

    token_resp = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": cb["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    access_token = (token_resp.get("access_token") or "").strip()
    refresh_token = (token_resp.get("refresh_token") or "").strip()
    id_token = (token_resp.get("id_token") or "").strip()
    expires_in = _to_int(token_resp.get("expires_in"))

    claims = _jwt_claims_no_verify(id_token)
    email = str(claims.get("email") or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()

    now = int(time.time())
    expired_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0)))
    now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    config = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "last_refresh": now_rfc3339,
        "email": email,
        "type": "codex",
        "expired": expired_rfc3339,
    }
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))

# ========== 注册逻辑 ==========

def run(proxy: Optional[str]) -> Optional[tuple[str, str, str]]:
    proxies: Any = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    s = requests.Session(proxies=proxies, impersonate="chrome")
    s.headers.update({"user-agent": UA})

    try:
        trace = s.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
        loc_re = re.search(r"^loc=(.+)$", trace.text, re.MULTILINE)
        loc = loc_re.group(1) if loc_re else None
        print(f"[*] 当前 IP 所在地: {loc}")
        if loc in ("CN", "HK"):
            raise RuntimeError("检查代理哦w - 所在地不支持")
    except Exception as e:
        print(f"[Error] 网络连接检查失败: {e}")
        return None
    print(f"[*] 请求头 UA: {s.headers.get('user-agent')}")

    try:
        email, password, code_fetcher = get_email_and_code_fetcher(proxies)
        if not email or not code_fetcher:
            return None
        print(f"[*] 成功获取 GPTMail 邮箱: {email}")
    except Exception as e:
        print(f"[Error] 获取 GPTMail 邮箱失败: {e}")
        return None

    oauth = generate_oauth_url()
    url = oauth.auth_url

    try:
        resp = s.get(url, timeout=15)
        did = s.cookies.get("oai-did")
        print(f"[*] Device ID: {did}")

        signup_body = json.dumps({"username": {"value": email, "kind": "email"}, "screen_hint": "signup"})
        sen_req_body = json.dumps({"p": "", "id": did, "flow": "authorize_continue"})

        sen_resp = requests.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers={
                "origin": "https://sentinel.openai.com",
                "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                "content-type": "text/plain;charset=UTF-8",
                "user-agent": UA,
            },
            data=sen_req_body,
            proxies=proxies,
            impersonate="chrome",
            timeout=15,
        )
        print(f"[*] sentinel authorize_continue 状态: {sen_resp.status_code}")
        if sen_resp.status_code != 200:
            print(f"[Error] Sentinel 异常拦截，状态码: {sen_resp.status_code}")
            try:
                print(sen_resp.text)
            except Exception:
                pass
            return None

        sen_token = sen_resp.json().get("token")
        print(f"[*] sentinel authorize_continue token: {bool(sen_token)}")
        sentinel = json.dumps({"p": "", "t": "", "c": sen_token, "id": did, "flow": "authorize_continue"}) if sen_token else None

        # so_token for create_account flow
        so_token = fetch_sentinel_token(flow="oauth_create_account", did=did, proxies=proxies)
        print(f"[*] sentinel oauth_create_account token: {bool(so_token)}")

        signup_headers = {
            "referer": "https://auth.openai.com/create-account",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if sentinel:
            signup_headers["openai-sentinel-token"] = sentinel
        signup_resp = s.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=signup_headers,
            data=signup_body,
        )
        print(f"[*] authorize/continue 状态: {signup_resp.status_code}, email={email}")
        if signup_resp.status_code != 200:
            print(f"[Error] 注册表单提交失败，状态码: {signup_resp.status_code}")
            try:
                print(signup_resp.text)
            except Exception:
                pass
            return None

        # 设置密码
        register_headers = {
            "referer": "https://auth.openai.com/create-account/password",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if sentinel:
            register_headers["openai-sentinel-token"] = sentinel
        reg_resp = s.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=register_headers,
            data=json.dumps({"password": password, "username": email}),
        )
        print(f"[*] 设置密码状态: {reg_resp.status_code}")
        if reg_resp.status_code != 200:
            print(f"[Error] 设置密码失败，状态码: {reg_resp.status_code}")
            try:
                print(reg_resp.text)
            except Exception:
                pass
            return None

        # 触发发送验证码（采用 send 而非 resend）
        try:
            send_headers = {
                "referer": "https://auth.openai.com/create-account/password",
                "accept": "application/json",
                "content-type": "application/json",
            }
            send_resp = s.get(
                "https://auth.openai.com/api/accounts/email-otp/send",
                headers=send_headers,
                timeout=15,
            )
            print(f"[*] 触发发送验证码状态: {send_resp.status_code}")
            if send_resp.status_code != 200:
                try:
                    print(send_resp.text)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Warn] send 调用异常: {e}")

        code = code_fetcher()
        if not code:
            print("[Error] 未能从 GPTMail 收到验证码")
            return None
        print(f"[*] 收到验证码: {code}")

        code_body = json.dumps({"code": code})
        validate_headers = {
            "referer": "https://auth.openai.com/email-verification",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if sentinel:
            validate_headers["openai-sentinel-token"] = sentinel

        code_resp = s.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=validate_headers,
            data=code_body,
        )
        print(f"[*] 验证码校验状态: {code_resp.status_code}")
        if code_resp.status_code != 200:
            try:
                print(code_resp.text)
            except Exception:
                pass
            return None

        create_account_body = json.dumps({"name": _random_name(), "birthdate": _random_birthdate()})
        create_headers = {
            "referer": "https://auth.openai.com/about-you",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if so_token:
            create_headers["openai-sentinel-so-token"] = so_token
        print(f"[*] create_account headers keys: {list(create_headers.keys())}")
        create_account_resp = s.post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=create_headers,
            data=create_account_body,
        )
        create_account_status = create_account_resp.status_code
        print(f"[*] 账户创建状态: {create_account_status}, so_token_used={bool(so_token)}")
        if create_account_status != 200:
            try:
                print(create_account_resp.text)
            except Exception:
                pass
            return None

        auth_cookie = s.cookies.get("oai-client-auth-session")
        if not auth_cookie:
            print("[Error] 未能获取到授权 Cookie")
            return None

        auth_json = _decode_jwt_segment(auth_cookie.split(".")[0])
        workspaces = auth_json.get("workspaces") or []
        if not workspaces:
            print("[Error] 授权 Cookie 里没有 workspace 信息")
            return None
        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        if not workspace_id:
            print("[Error] 无法解析 workspace_id")
            return None

        select_body = json.dumps({"workspace_id": workspace_id})
        select_resp = s.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers={
                "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "content-type": "application/json",
            },
            data=select_body,
        )
        if select_resp.status_code != 200:
            print(f"[Error] 选择 workspace 失败，状态码: {select_resp.status_code}")
            try:
                print(select_resp.text)
            except Exception:
                pass
            return None

        continue_url = str((select_resp.json() or {}).get("continue_url") or "").strip()
        if not continue_url:
            print("[Error] workspace/select 响应里缺少 continue_url")
            return None

        current_url = continue_url
        for _ in range(6):
            final_resp = s.get(current_url, allow_redirects=False, timeout=15)
            location = final_resp.headers.get("Location") or ""
            if final_resp.status_code not in [301, 302, 303, 307, 308]:
                break
            if not location:
                break
            next_url = urllib.parse.urljoin(current_url, location)
            if "code=" in next_url and "state=" in next_url:
                token_json = submit_callback_url(
                    callback_url=next_url,
                    code_verifier=oauth.code_verifier,
                    redirect_uri=oauth.redirect_uri,
                    expected_state=oauth.state,
                )
                return token_json, email, password
            current_url = next_url

        print("[Error] 未能在重定向链中捕获到最终 Callback URL")
        return None

    except Exception as e:
        print(f"[Error] 运行时发生错误: {e}")
        return None

def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI 自动注册脚本")
    parser.add_argument("--proxy", default=None, help="代理地址，如 http://127.0.0.1:7890")
    # 原始代码
    # parser.add_argument("--once", action="store_true", help="只运行一次")
    
    # 替换为：
    parser.add_argument("--count", type=int, default=1, help="指定运行注册的次数 (0为无限次)")
    parser.add_argument("--sleep-min", type=int, default=5, help="循环模式最短等待秒数")
    parser.add_argument("--sleep-max", type=int, default=30, help="循环模式最长等待秒数")
    args = parser.parse_args()

    sleep_min = max(1, args.sleep_min)
    sleep_max = max(sleep_min, args.sleep_max)

    count = 0
    print("[Info] MasterAlanLab OpenAI Registrar Started")
    # OUT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        count += 1
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] >>> 开始第 {count} 次注册流程 <<<")
        try:
            run_result = run(args.proxy)
            if run_result:
                token_json, email, password = run_result
                try:
                    t_data = json.loads(token_json)
                    fname_email = t_data.get("email", "unknown").replace("@", "_")
                except Exception:
                    fname_email = "unknown"

                # tokens_dir = OUT_DIR / "Json"
                try:
                    #原始代码
                    # tokens_dir.mkdir(parents=True, exist_ok=True)
                    #修改代码
                    os.makedirs("Json", exist_ok=True) # 确保 Json 文件夹存在
                except Exception:
                    pass
                #原始代码
                # file_path = tokens_dir / f"token_{fname_email}_{int(time.time())}.json"
                #修改代码
                file_name = f"token_{fname_email}_{int(time.time())}.json"
                file_path = os.path.join("Json", file_name)
                try:
                    file_path.write_text(token_json, encoding="utf-8")
                    print(f"[*] 成功! Token 已保存至: {file_path}")
                except Exception as e:
                    print(f"[Error] 保存 token 失败: {e}")

                try:
                    #原始代码
                    # acc_dir = OUT_DIR / "Json"
                    #修改代码
                    acc_dir =file_path
                    acc_dir.mkdir(parents=True, exist_ok=True)
                    acc_file = acc_dir / "accounts.txt"
                    acc_file.write_text("", encoding="utf-8", errors="ignore") if not acc_file.exists() else None
                except Exception:
                    pass
                try:
                    with open(acc_dir / "accounts.txt", "a", encoding="utf-8") as f:
                        f.write(f"{email}----{password}\n")
                except Exception as e:
                    print(f"[Error] 保存账号信息失败: {e}")
            else:
                print("[-] 本次注册失败。")
        except Exception as e:
            print(f"[Error] 发生未捕获异常: {e}")

        # 原始代码
        # if args.once:
        #     break

        # 替换为：
        if args.count > 0 and count >= args.count:
            print(f"[*] 已达到设定的执行次数 ({args.count})，任务结束。")
            break
        wait_time = random.randint(sleep_min, sleep_max)
        print(f"[*] 休息 {wait_time} 秒...")
        time.sleep(wait_time)

if __name__ == "__main__":
    main()
