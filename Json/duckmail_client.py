"""
DuckMail API client (standalone).

Based on: https://www.chatgpt.org.uk/2025/11/gptmailapiapi.html

Supports:
  - Generate a temp email:        GET/POST /api/generate-email
  - List mailbox emails:          GET /api/emails?email=...
  - Fetch an email by id:         GET /api/email/{id}
  - Delete an email by id:        DELETE /api/email/{id}
  - Clear mailbox:               DELETE /api/emails/clear?email=...
"""

from __future__ import annotations

import io
import re
import sys
import time
import random
import string
import secrets
from dataclasses import dataclass
from typing import Any

import requests


# Windows console output can be GBK; keep logs readable.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    except Exception:
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
        except Exception:
            pass

    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    except Exception:
        try:
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
        except Exception:
            pass


@dataclass(frozen=True)
class GPTMailAPIError(RuntimeError):
    status_code: int | None
    message: str
    response: Any | None = None
    url: str | None = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"(status={self.status_code})")
        if self.url:
            parts.append(f"url={self.url}")
        return " ".join(parts)


class GPTMailClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        # DuckMail state
        self._tokens: dict[str, str] = {}  # email -> token
        self._account_ids: dict[str, str] = {}  # email -> account_id
        self._passwords: dict[str, str] = {}  # email -> password
        
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "tavily-register/duckmail-client",
            }
        )
        # If API Key is provided, use it for domain fetching
        if api_key and api_key.startswith("dk_"):
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def __enter__(self) -> "GPTMailClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth_token: str | None = None,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path

        url = f"{self.base_url}{path}"
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        try:
            resp = self._session.request(
                method, url, params=params, json=json_body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise GPTMailAPIError(None, f"Request failed: {e}", url=url) from e

        if resp.status_code == 204:
            return None

        try:
            payload = resp.json()
        except ValueError:
            raise GPTMailAPIError(resp.status_code, "Non-JSON response", response=resp.text, url=url)

        if resp.status_code >= 400:
            message = payload.get("message") or payload.get("error") or "API request failed"
            raise GPTMailAPIError(resp.status_code, str(message), response=payload, url=url)

        return payload

    def generate_email(self, *, prefix: str | None = None, domain: str | None = None) -> str:
        """
        Generate a new DuckMail account.
        """
        # 1. Get domains
        domains_data = self._request("GET", "/domains")
        available_domains = [d["domain"] for d in domains_data.get("hydra:member", []) if d.get("isVerified")]
        
        if not available_domains:
            raise GPTMailAPIError(None, "No available domains found", response=domains_data)

        if domain and domain in available_domains:
            selected_domain = domain
        else:
            selected_domain = random.choice(available_domains)

        # 2. Prepare account
        name = prefix or "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        address = f"{name}@{selected_domain}"
        password = "".join(random.choices(string.ascii_letters + string.digits, k=12))

        # 3. Create account
        account_data = self._request("POST", "/accounts", json_body={
            "address": address,
            "password": password
        })
        
        account_id = account_data["id"]
        self._account_ids[address] = account_id
        self._passwords[address] = password

        # 4. Get token
        token_data = self._request("POST", "/token", json_body={
            "address": address,
            "password": password
        })
        
        self._tokens[address] = token_data["token"]
        return address

    def list_emails(self, email: str) -> list[dict[str, Any]]:
        """List messages for the account (GET /messages)."""
        token = self._tokens.get(email)
        if not token:
            raise GPTMailAPIError(None, f"No token for {email}. Did you call generate_email()?")

        data = self._request("GET", "/messages", auth_token=token)
        return data.get("hydra:member", [])

    def get_email(self, message_id: str) -> dict[str, Any]:
        """Fetch message detail (GET /messages/{id})."""
        # We need a token to fetch message. Since message_id doesn't tell us which email it belongs to,
        # we try the most recent token or all tokens if necessary.
        if not self._tokens:
            raise GPTMailAPIError(None, "No active email tokens available")

        # In most use cases, we are working with the latest generated email
        latest_email = list(self._tokens.keys())[-1]
        token = self._tokens[latest_email]

        try:
            return self._request("GET", f"/messages/{message_id}", auth_token=token)
        except GPTMailAPIError as e:
            if e.status_code == 401 or e.status_code == 404:
                # Try other tokens if the latest one fails
                for email_addr, t in self._tokens.items():
                    if email_addr == latest_email: continue
                    try:
                        return self._request("GET", f"/messages/{message_id}", auth_token=t)
                    except: continue
            raise e

    def delete_email(self, message_id: str) -> None:
        """Delete a message."""
        # Similar token lookup as get_email
        if not self._tokens:
            return
        latest_email = list(self._tokens.keys())[-1]
        token = self._tokens[latest_email]
        self._request("DELETE", f"/messages/{message_id}", auth_token=token)

    def clear_mailbox(self, email: str) -> None:
        """Delete the entire account."""
        token = self._tokens.get(email)
        account_id = self._account_ids.get(email)
        if token and account_id:
            self._request("DELETE", f"/accounts/{account_id}", auth_token=token)
            self._tokens.pop(email, None)
            self._account_ids.pop(email, None)
            self._passwords.pop(email, None)

    def wait_for_verification_link(
        self,
        email: str,
        *,
        timeout: int = 180,
        poll_interval: float = 5.0,
    ) -> str | None:
        """
        Poll the mailbox until a Tavily/Auth0 verification link is found.

        Returns:
            Verification link URL, or None on timeout.
        """
        patterns = [
            r'https://auth\.tavily\.com/u/email-verification\?ticket=[A-Za-z0-9_\-]+',
            r'https://auth\.tavily\.com/u/email-verification\?ticket=[^\s\"\'\<\>]+',
            r'https://auth\.tavily\.com[^\s\"\'\<\>]+ticket=[^\s\"\'\<\>]+',
            r'href=["\']?(https://auth\.tavily\.com[^"\'\s\<\>]+)',
        ]

        seen_ids: set[str] = set()
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            try:
                summaries = self.list_emails(email)
            except GPTMailAPIError:
                summaries = []

            for summary in summaries:
                email_id = _extract_email_id(summary)
                if not email_id or email_id in seen_ids:
                    continue
                seen_ids.add(email_id)

                try:
                    detail = self.get_email(email_id)
                except GPTMailAPIError:
                    continue

                blob = "\n".join(_iter_strings(summary)) + "\n" + "\n".join(_iter_strings(detail))
                for pattern in patterns:
                    matches = re.findall(pattern, blob, flags=re.IGNORECASE)
                    if matches:
                        link = matches[0]
                        link = link.replace("&amp;", "&")
                        link = re.sub(r'["\'\<\>#]+$', "", link)
                        return link

            time.sleep(poll_interval)

        return None


def _iter_strings(obj: Any) -> list[str]:
    out: list[str] = []

    def _walk(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str):
            if v:
                out.append(v)
            return
        if isinstance(v, bytes):
            try:
                s = v.decode("utf-8", errors="replace")
            except Exception:
                return
            if s:
                out.append(s)
            return
        if isinstance(v, dict):
            for vv in v.values():
                _walk(vv)
            return
        if isinstance(v, (list, tuple)):
            for vv in v:
                _walk(vv)
            return

    _walk(obj)
    return out


def _extract_email_id(summary: dict[str, Any]) -> str | None:
    for key in ("id", "_id", "email_id", "emailId", "message_id", "messageId", "mail_id", "mailId"):
        v = summary.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None
