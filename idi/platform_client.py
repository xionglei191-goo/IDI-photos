from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit

import httpx

from .config import ROOT
from .db import now
from .domain import DomainError
from .platform_contract import BASE_URL, READ_ENDPOINTS, STAGES


ROW_KEYS = {"reportReceivingId", "reportCode", "reportNumber", "communityName", "communityDesc", "engName",
            "accidentDesc", "issueDescription", "siteSurveyId", "siteSurveyState", "repairMethod", "interceptFlag",
            "repairAcceptanceId", "repairAcceptanceState", "repairAcceptanceFlowStatus", "acceptanceDate",
            "policyNum", "flowStatus", "reportReceivingState", "passStatus", "siteSurveyPassStatus", "acceptanceResult",
            "endStatus", "reportDate", "createTime", "updateTime", "reportReceivingSubmitTime", "insuraceCompany",
            "repairPersonName", "surveyorUserName", "repairUnit", "reportContact", "reportContactPhone", "projectAddress"}
DETAIL_KEYS = ROW_KEYS | {"communityInfoId", "engineeringModelingParentId", "repairUnitId", "repairPersonName", "repairPersonPhone"}
for _stage in STAGES.values():
    for _field in _stage["fields"]:
        DETAIL_KEYS.add(_field["key"])
        if _field.get("name_key"):
            DETAIL_KEYS.add(_field["name_key"])


def pick(data, keys):
    return {k: v for k, v in data.items() if k in keys}


def external_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise DomainError("官网记录 ID 必须使用完整字符串，不能使用报修编号代替或转换为数字")
    return value


def attachment_url(value):
    """Only the official file area may receive the platform session token."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise DomainError('官网附件缺少有效文件地址', 422)
    parsed, base = urlsplit(value), urlsplit(BASE_URL)
    if (parsed.netloc and (parsed.scheme != base.scheme or parsed.netloc != base.netloc)
            or parsed.scheme and not parsed.netloc or parsed.fragment):
        raise DomainError('官网附件地址不属于已配置的文件服务', 422)
    path = parsed.path
    for _ in range(3):
        path = unquote(path)
    if ('\\' in path or '%' in path or any(ord(c) < 32 for c in path)
            or any(p in ('.', '..') for p in path.split('/'))):
        raise DomainError('官网附件地址无效', 422)
    if path.startswith(base.path + '/profile/'):
        path = path[len(base.path):]
    if not path.startswith('/profile/') or path.endswith('/'):
        raise DomainError('官网附件地址不属于已配置的文件服务', 422)
    return BASE_URL + path + ('?' + parsed.query if parsed.query else '')


def unsubmitted_report(row, scope):
    # The official intake page defines reportReceivingState 0 as saved, 1 as submitted.
    # Legacy saved reports have no flowStatus and no completed stage details yet.
    return (scope == 'intake' and str(row.get('reportReceivingState')) == '0'
            and row.get('flowStatus') in (None, '', 0, '0')
            and str(row.get('endStatus')) != '1')


class PlatformWriteError(DomainError):
    def __init__(self, message, status=502, *, rejected=False):
        super().__init__(message, status)
        self.rejected = rejected


class PlatformReadPaused(DomainError):
    def __init__(self, message, retry_after=60, status=503):
        super().__init__(message, status)
        self.retry_after = max(1, retry_after)


class PlatformBudgetReached(DomainError):
    def __init__(self):
        super().__init__('本批官网请求已达上限，已保留进度等待继续', 429)


class PlatformLoginError(DomainError):
    def __init__(self, message, status=502, *, retryable=True):
        super().__init__(message, status)
        self.retryable = retryable


class PlatformClient:
    """Read-only by default; reviewed writes require explicit runtime configuration."""

    def __init__(self, transport=None, write_enabled=False, *, permanently_read_only=False):
        self._http = httpx.Client(base_url=BASE_URL, timeout=25, follow_redirects=False, transport=transport)
        self._token = ""
        self._account = ""
        self._lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._session_generation = 0
        self._connected_at = None
        self.write_enabled = bool(write_enabled)
        self._permanently_read_only = permanently_read_only
        self.request_interval = 0.0 if transport is not None else 1.0
        self._next_request = 0.0
        self._read_pause_until = 0.0
        self._read_failures = 0
        self._budget = threading.local()
        self._closing = threading.Event()

    @contextmanager
    def request_budget(self, limit, on_request=None):
        previous = getattr(self._budget, 'value', None)
        value = {'limit': max(0, limit), 'used': 0, 'on_request': on_request}
        self._budget.value = value
        try:
            yield value
        finally:
            self._budget.value = previous

    def _before_request(self):
        if self._closing.is_set():
            raise PlatformReadPaused('服务正在停止，官网同步进度已保留', 30)
        delay = self._read_pause_until - time.monotonic()
        if delay > 0:
            raise PlatformReadPaused('官网请求暂缓，稍后继续', delay)
        budget = getattr(self._budget, 'value', None)
        if budget is not None and budget['used'] >= budget['limit']:
            raise PlatformBudgetReached()
        delay = max(0, self._next_request - time.monotonic())
        if delay and self._closing.wait(delay):
            raise PlatformReadPaused('服务正在停止，官网同步进度已保留', 30)
        if budget is not None:
            if budget['on_request']:
                budget['on_request']()
            budget['used'] += 1
        self._next_request = time.monotonic() + self.request_interval

    def _pause_reads(self, response=None):
        self._read_failures += 1
        delay = min(300, 15 * 2 ** min(self._read_failures - 1, 5))
        if response is not None and response.status_code == 429:
            raw = response.headers.get('Retry-After', '')
            try:
                delay = max(1, float(raw))
                if not math.isfinite(delay):
                    raise ValueError()
            except ValueError:
                try:
                    delay = max(1, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = max(delay, 60)
        self._read_pause_until = time.monotonic() + delay
        return PlatformReadPaused('官网限流或暂不可用，已暂停请求并保留同步进度', delay)

    def status(self):
        with self._lock:
            return {"connected": bool(self._token), "account": self._account,
                    "connected_at": self._connected_at, "mode": "reviewed_writes" if self.write_enabled else "read_only", "write_enabled": self.write_enabled}

    def _write(self, path, **kwargs):
        if self._permanently_read_only:
            raise DomainError('公司账号仅用于读取清单和下载资料；官网写入必须使用本项目账号', 403)
        if not self.write_enabled:
            raise DomainError("官网当前为只读模式，可保存核对记录和下载资料；尚未启用官网上传提交", 403)
        allowed = {stage['endpoint'] for stage in STAGES.values()} | {'/insurance/sysFileInfo/upload'}
        if path not in allowed:
            raise DomainError('此官网写入接口未配置', 403)
        with self._lock:
            if not self._token:
                raise PlatformWriteError('请先连接官网', 401, rejected=True)
            try:
                self._before_request()
                response = self._http.post(path, headers={'Authorization': 'Bearer ' + self._token}, **kwargs)
                if response.status_code == 429:
                    paused = self._pause_reads(response)
                    raise PlatformWriteError('官网限流，未继续操作，请稍后回读核对', 429, rejected=True) from paused
                if response.status_code >= 500:
                    self._pause_reads(response)
                if response.status_code in (401, 403):
                    if response.status_code == 401:
                        self.logout()
                    raise PlatformWriteError('官网登录已失效或没有提交权限，请重新连接官网后核对', response.status_code, rejected=True)
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise PlatformWriteError('官网操作未取得有效回执，结果未知；请回读核对，勿直接重复提交', 502) from error
            if not isinstance(result, dict) or result.get('code') != 0:
                if isinstance(result, dict) and result.get('code') == 2:
                    self.logout()
                    raise PlatformWriteError('官网登录已过期，请重新连接官网后提交', 401, rejected=True)
                if isinstance(result, dict) and isinstance(result.get('code'), (int, str)):
                    detail = result.get('msg') or result.get('message')
                    detail = ' '.join(detail.split())[:300] if isinstance(detail, str) else ''
                    raise PlatformWriteError('官网拒绝本次操作' + ('：' + detail if detail else '，请核对资料或账号权限'), 422, rejected=True)
                raise PlatformWriteError('官网未返回有效操作回执，请回读核对', 502)
            return result

    def upload(self, path, filename, mime_type):
        # The original archive is never replaced or recompressed.
        with path.open('rb') as source:
            result = self._write('/insurance/sysFileInfo/upload', files={'file': (filename, source, mime_type)}, timeout=httpx.Timeout(120, connect=15))
        records = result.get('data')
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict) or not records[0].get('id') or not records[0].get('fileUrl'):
            raise DomainError('官网上传回执缺少文件记录，未继续业务提交', 502)
        return records[0]

    def download_attachment(self, url, max_bytes, account=''):
        url = attachment_url(url)
        with self._lock:
            if not self._token:
                raise DomainError('请先连接官网，再同步附件原件', 401)
            if account and account != self._account:
                raise DomainError('官网登录账号已变化，请刷新案件后再同步附件', 409)
            self._before_request()
            try:
                with self._http.stream('GET', url, headers={'Authorization': 'Bearer ' + self._token},
                                       timeout=httpx.Timeout(60, connect=15)) as response:
                    if response.status_code == 429:
                        raise self._pause_reads(response)
                    if response.status_code in (401, 403):
                        if response.status_code == 401:
                            self.logout()
                        raise DomainError('官网登录已失效或没有附件读取权限，请重新连接官网', response.status_code)
                    if response.is_redirect:
                        raise DomainError('官网附件地址发生跳转，请刷新案件资料后重试', 502)
                    response.raise_for_status()
                    if 'text/html' in response.headers.get('content-type', '').lower():
                        raise DomainError('官网未返回附件文件，请刷新案件资料后重试', 502)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise DomainError('官网附件超过本地单文件大小限制', 413)
                        chunks.append(chunk)
                    data = b''.join(chunks)
                    if 'json' in response.headers.get('content-type', '').lower():
                        try:
                            result = json.loads(data)
                        except ValueError:
                            result = {}
                        if isinstance(result, dict) and result.get('code') == 2:
                            self.logout()
                            raise DomainError('官网登录已过期，请重新连接后同步附件', 401)
                        raise DomainError('官网未返回附件文件，请刷新案件资料后重试', 502)
                    if not data:
                        raise DomainError('官网附件为空，未保存到本地', 502)
                    return data
            except httpx.HTTPError as error:
                raise DomainError('官网附件暂时无法下载，请稍后重试', 502) from error

    def submit(self, stage, payload):
        if stage not in STAGES:
            raise DomainError('官网提交环节不存在')
        return self._write(STAGES[stage]['endpoint'], json=payload)

    def save_draft_only(self, stage, payload):
        """Capability used by the group Agent: no caller-selectable submit action."""
        if stage not in ('survey', 'repair') or not isinstance(payload, dict) or type(payload.get('state')) is not int or payload['state'] != 0:
            raise DomainError('Agent 仅可保存查勘或修复成果草稿，没有正式提交权限', 403)
        return self._write(STAGES[stage]['endpoint'], json=payload)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
            raise DomainError("请填写官网账号和密码")
        if len(username) > 160 or len(password) > 512:
            raise DomainError("账号或密码过长")
        node = shutil.which("node")
        if not node or not (ROOT / "frontend/node_modules/playwright/index.mjs").is_file():
            raise PlatformLoginError("官网登录需要 Node.js 和项目浏览器依赖，请先运行安装脚本", 503, retryable=False)
        with self._login_lock:
            if self._closing.is_set():
                raise DomainError('服务正在停止，未继续官网登录。', 409)
            self.logout()
            with self._lock:
                generation = self._session_generation
            try:
                result = subprocess.run(
                    [node, str(ROOT / "scripts/platform-login.mjs")],
                    input=json.dumps({"username": username.strip(), "password": password}),
                    capture_output=True, text=True, encoding="utf-8", timeout=100,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                response = json.loads(result.stdout)
                if not isinstance(response, dict):
                    raise ValueError("invalid login receipt")
                token = response.get("token")
                if result.returncode or not isinstance(token, str) or not token.strip() or token.strip() in ("undefined", "null"):
                    failure = 'login_failed'
                    phase = response.get("phase")
                    if phase in ("start", "browser", "page", "account_field", "login_response", "token_response"):
                        diagnostic = response.get("diagnostics", {})
                        diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
                        failure = diagnostic.get("failure")
                        failure = failure if failure in ("login_rejected", "http_rejected", "http_failure", "rate_limited", "browser_unavailable", "token_missing", "timeout", "ambiguous_login_control", "login_failed") else "login_failed"
                        logging.getLogger(__name__).warning("Website login incomplete: phase=%s sent=%s http=%s code=%s script_error=%s failure=%s", phase,
                            bool(diagnostic.get("requestSent")), diagnostic.get("httpStatus") if type(diagnostic.get("httpStatus")) is int else None,
                            diagnostic.get("businessCode") if type(diagnostic.get("businessCode")) is int else None, bool(diagnostic.get("scriptError")), failure)
                    status = response.get("status")
                    status = status if type(status) is int and status in (400, 401, 403, 429, 502, 503, 504) else 502
                    detail = response.get("error")
                    detail = detail.replace(password, "[已隐藏]")[:400] if isinstance(detail, str) and detail.strip() else "官网登录未完成，请稍后重试或先在官网确认登录要求。"
                    raise PlatformLoginError(detail, status, retryable=status not in (400, 401, 403)
                                             and failure not in ('token_missing', 'browser_unavailable', 'ambiguous_login_control'))
            except subprocess.TimeoutExpired as error:
                raise DomainError("官网登录等待超时，请稍后重试或先打开官网检查网络。", 504) from error
            except OSError as error:
                raise PlatformLoginError("官网登录程序未能启动，请检查本机浏览器和运行环境。", 503, retryable=False) from error
            except ValueError as error:
                raise PlatformLoginError("官网登录程序未返回有效结果，请稍后重试。", 502, retryable=False) from error
            with self._lock:
                if self._closing.is_set() or generation != self._session_generation:
                    raise DomainError('本次连接已取消，请按当前设置重新连接。', 409)
                self._token = token
                self._account = username.strip()
                self._connected_at = now()
                self._read_pause_until = 0.0
                self._read_failures = 0
            return self.status()

    def logout(self):
        with self._lock:
            self._session_generation += 1
            self._token = ""
            self._account = ""
            self._connected_at = None
        return self.status()

    def close(self):
        self._closing.set()
        self.logout()
        self._http.close()

    def read(self, kind, body=None, record_id=None):
        if kind not in READ_ENDPOINTS:
            raise DomainError("当前连接仅支持已核对的官网只读接口", 403)
        path = READ_ENDPOINTS[kind]
        if kind in ("claim", "survey", "repair"):
            path += external_id(record_id)
        elif record_id is not None:
            raise DomainError("只读请求参数不匹配")
        with self._lock:
            if not self._token:
                raise DomainError("请先连接官网；可在系统设置中配置系统账号和自动连接。", 401)
            try:
                self._before_request()
                response = self._http.post(path, json=body or {}, headers={"Authorization": "Bearer " + self._token})
                if response.status_code in (401, 403):
                    if response.status_code == 401:
                        self.logout()
                    raise DomainError("官网登录已失效或此账号没有查看权限", response.status_code)
                if response.status_code == 429 or response.status_code >= 500:
                    raise self._pause_reads(response)
                if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                    raise DomainError("官网查询未成功，请稍后重试", 502)
                data = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise self._pause_reads() from error
            if not isinstance(data, dict) or data.get("code") != 0:
                if isinstance(data, dict) and data.get("code") == 2:
                    self.logout()
                    raise DomainError("官网登录已过期，请重新连接", 401)
                raise DomainError("官网未允许本次查询，或返回格式已变化", 502)
            self._read_failures = 0
            return data

    def search(self, query="", page=1, scope="repair", page_size=12, end_status=None, report_start=None, report_end=None):
        if scope not in ("repair", "intake") or not isinstance(page, int) or not 1 <= page <= 10000:
            raise DomainError("官网查询范围或页码无效")
        if not isinstance(query, str) or len(query) > 200:
            raise DomainError("查询内容过长")
        if page_size not in (12, 20, 30, 40) or end_status not in (None, "0", "1"):
            raise DomainError("官网分页或归档筛选无效")
        payload = {"page": page, "limit": page_size}
        if scope == "intake":
            payload["reportSubmitState"] = 1
        if end_status is not None:
            payload["endStatus"] = end_status
        if report_start is not None or report_end is not None:
            try:
                if (not isinstance(report_start, str) or not isinstance(report_end, str)
                        or len(report_start) != 10 or len(report_end) != 10
                        or date.fromisoformat(report_start) > date.fromisoformat(report_end)):
                    raise ValueError()
            except (TypeError, ValueError):
                raise DomainError('官网报修日期范围无效')
            payload.update(reportDateStart=report_start, reportDateEnd=report_end)
        if query.strip():
            payload["vagueSearch"] = query.strip()
        result = self.read(scope + "_list", payload)
        if not isinstance(result.get("data"), list):
            raise DomainError("官网案件列表格式已变化", 502)
        total = result.get("count")
        if isinstance(total, bool) or not isinstance(total, (int, str)) or not str(total).isdigit():
            raise DomainError("官网列表缺少有效总数，不能确认分页完整性", 502)
        for row in result["data"]:
            if not isinstance(row, dict):
                raise DomainError("官网列表行格式已变化", 502)
            external_id(row.get("reportReceivingId"))
            if report_start is not None:
                value = str(row.get('reportDate') or '')[:10]
                if not report_start <= value <= report_end:
                    raise DomainError('官网返回了日期范围之外的案件，日期筛选尚未通过核验，未扩大查询范围', 409)
            # Repair-list rows do not always echo the archive flag. The actual
            # filtered membership is persisted separately by the synchronizer.
            if end_status is not None and row.get('endStatus') is not None and str(row['endStatus']) != end_status:
                raise DomainError('官网返回了归档筛选之外的案件，未将本次清单标为完整', 409)
        return {"items": [pick(row, ROW_KEYS) for row in result["data"]], "total": int(total), "page": page, "page_size": page_size}

    def all_rows(self, scope, end_status, progress=None):
        """Read every page, rejecting repeated pages and changing totals."""
        rows, seen, expected, page = [], set(), None, 1
        while page <= 10000:
            result = self.search(page=page, scope=scope, page_size=40, end_status=end_status)
            if expected is None:
                expected = result["total"]
            elif result["total"] != expected:
                raise DomainError("同步期间官网台账总数发生变化，请重新同步；未将本次列表标为完整", 409)
            if expected > 100000:
                raise DomainError("官网台账超过本次同步上限，未标为同步完成", 409)
            for row in result["items"]:
                record_id = row["reportReceivingId"]
                if record_id in seen:
                    raise DomainError("官网分页返回重复案件，请重新同步；未将本次列表标为完整", 409)
                seen.add(record_id)
                rows.append(row)
            if progress:
                progress(len(rows), expected)
            if len(rows) == expected:
                return rows
            if not result["items"] or len(rows) > expected:
                raise DomainError("官网分页数量与总数不一致，本次同步未完成", 502)
            page += 1
        raise DomainError("官网分页超过同步上限", 409)

    def snapshot(self, record_id, scope="repair"):
        with self._lock:
            return self._snapshot(record_id, scope)

    def _snapshot(self, record_id, scope):
        row = self.lookup_row(record_id, scope)
        if row is None:
            raise DomainError("此记录未在当前账号的对应列表中找到，请重新查询并选择案件", 404)
        return self.snapshot_from_row(row, scope)

    def lookup_row(self, record_id, scope):
        record_id = external_id(record_id)
        if scope not in ("repair", "intake"):
            raise DomainError("官网查询范围无效")
        result = self.read(scope + "_list", {"reportReceivingId": record_id, "page": 1, "limit": 12})
        rows = result.get("data")
        if not isinstance(rows, list):
            raise DomainError('官网单案查询格式已变化', 502)
        if any(not isinstance(v, dict) or v.get('reportReceivingId') != record_id for v in rows):
            raise DomainError('官网未按案件 ID 返回记录，未保存本次查询', 502)
        row = next(iter(rows), None)
        return pick(row, ROW_KEYS) if row else None

    def snapshot_from_row(self, row, scope):
        with self._lock:
            return self._snapshot_from_row(row, scope)

    def _snapshot_from_row(self, row, scope):
        record_id = external_id(row.get("reportReceivingId"))
        if unsubmitted_report(row, scope):
            return {"row": pick(row, ROW_KEYS), "claim": {}, "survey": {}, "repair": {}, "personnel": [],
                    "scope": scope, "fetched_at": now(), "account": self._account,
                    "details_deferred": "unsubmitted_report"}
        claim = self.read("claim", record_id=record_id).get("data")
        if not isinstance(claim, dict) or str(claim.get("reportReceivingId", "")) != record_id:
            raise DomainError("官网案件详情 ID 与所选案件不一致，未保存关联", 502)
        survey = self.read("survey", record_id=external_id(str(row["siteSurveyId"]))).get("data") if row.get("siteSurveyId") else {}
        repair = self.read("repair", record_id=external_id(str(row["repairAcceptanceId"]))).get("data") if row.get("repairAcceptanceId") else {}
        if not isinstance(survey, dict) or not isinstance(repair, dict):
            raise DomainError("官网阶段详情格式已变化，未保存关联", 502)
        # Verify foreign keys whenever the platform returns them.
        for detail, key in ((survey, "siteSurveyId"), (repair, "repairAcceptanceId")):
            if detail and ((detail.get("reportReceivingId") and str(detail["reportReceivingId"]) != record_id)
                           or (detail.get(key) and str(detail[key]) != str(row.get(key)))):
                raise DomainError("官网阶段详情与案件关联不一致，未保存关联", 502)
        personnel = []
        context = {**claim, **survey}
        if all(context.get(k) for k in ("communityInfoId", "engineeringModelingParentId", "repairUnitId")):
            values = self.read("personnel", {"communityInfoId": context["communityInfoId"],
                "engineeringModelingParentId": context["engineeringModelingParentId"],
                "unitCompanyId": context["repairUnitId"], "dutyCode": "maintenance_duty_02"}).get("data")
            if not isinstance(values, list):
                raise DomainError("官网维修人员列表格式已变化", 502)
            personnel = [{"userId": str(v.get("userId", "")), "name": v.get("name", ""),
                          "phone": v.get("phone") or v.get("mobilePhone") or ""} for v in values if v.get("userId")]
        return {"row": pick(row, ROW_KEYS), "claim": pick(claim, DETAIL_KEYS), "survey": pick(survey, DETAIL_KEYS), "repair": pick(repair, DETAIL_KEYS), "personnel": personnel,
                "scope": scope, "fetched_at": now(), "account": self._account}

    def dictionaries(self):
        with self._lock:
            return self._dictionaries()

    def _dictionaries(self):
        result = {}
        for name in ("part_engineering", "defect_type", "estimate_restoration_cost"):
            data = self.read("dictionary", {"category": name}).get("data", {})
            records = data.get("records") if isinstance(data, dict) else None
            if not isinstance(records, list) or not records:
                raise DomainError("官网选项列表为空或格式已变化", 502)
            result[name] = [{"code": r["code"], "name": r["name"]} for r in records
                            if isinstance(r.get("code"), str) and isinstance(r.get("name"), str)]
            if not result[name]:
                raise DomainError("官网选项列表格式已变化", 502)
        return result
