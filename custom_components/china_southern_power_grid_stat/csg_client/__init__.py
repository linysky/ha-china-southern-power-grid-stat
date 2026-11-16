# -*- coding: utf-8 -*-
# Modified 2026-11-05 by mintisan/home-assistant-shenzhen-utilities: daily query falls back to the calendar API on sta=02. Redistributed 2026-11-11 by linysky. See NOTICE.md; licensed GPL-3.0.
"""
Implementations of CSG's Web API
this library is synchronous - since the updates are not frequent (12h+)
and each update only contains a few requests
"""
from __future__ import annotations

import datetime
import json
import logging
import random
import time
from base64 import b64decode, b64encode
from copy import copy
from hashlib import md5
from typing import Any

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

from .const import (
    ALLOWED_BASE_PATHS,
    API_PROFILE_APP,
    API_PROFILE_TO_BASE_PATH,
    API_PROFILE_WEB,
    AREACODE_FALLBACK,
    ATTR_ACCOUNT_NUMBER,
    ATTR_ADDRESS,
    ATTR_API_PROFILE,
    ATTR_AREA_CODE,
    ATTR_AUTH_TOKEN,
    ATTR_ELE_CUSTOMER_ID,
    ATTR_METERING_POINT_ID,
    ATTR_METERING_POINT_NUMBER,
    ATTR_USER_NAME,
    BASE_PATH_QR_NEW,
    BASE_PATH_WEB,
    CREDENTIAL_PUBKEY,
    HEADER_CUST_NUMBER,
    HEADER_X_AUTH_TOKEN,
    JSON_KEY_ACCT_ID,
    JSON_KEY_AREA_CODE,
    JSON_KEY_CRED_TYPE,
    JSON_KEY_CUST_NUMBER,
    JSON_KEY_DATA,
    JSON_KEY_ELE_CUST_ID,
    JSON_KEY_LOGON_CHAN,
    JSON_KEY_MESSAGE,
    JSON_KEY_METERING_POINT_ID,
    JSON_KEY_METERING_POINT_NUMBER,
    JSON_KEY_PARAM,
    JSON_KEY_SMS_CODE,
    JSON_KEY_STA,
    JSON_KEY_YEAR_MONTH,
    LOGIN_TYPE_PHONE_CODE,
    LOGIN_TYPE_PHONE_PWD_CODE,
    LOGIN_TYPE_TO_QR_CODE_TYPE,
    LOGON_CHANNEL_HANDHELD_HALL,
    LOGON_CHANNEL_ONLINE_HALL,
    PARAM_IV_APP,
    PARAM_IV_WEB,
    PARAM_KEY_APP,
    PARAM_KEY_WEB,
    REQUEST_TIMEOUT,
    RESP_STA_LOGIN_WRONG_CREDENTIAL,
    RESP_STA_NO_LOGIN,
    RESP_STA_QR_NOT_SCANNED,
    RESP_STA_QR_TIMEOUT,
    RESP_STA_SUCCESS,
    SEND_MSG_TYPE_VERIFICATION_CODE,
    VERIFICATION_CODE_TYPE_LOGIN,
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    WF_ATTR_MONTH,
    LoginType,
)
from .const import (
    api_profile_for_login_type as api_profile_for_login_type,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSG 电费 / 阶梯数据恢复补丁 (2026-11-13)
# 南网已下线 charge/queryDayElectricByMPoint 的电费与阶梯字段（返回 sta=02），
# 原代码直接回退到只含电量的日历接口，导致 6 个传感器恒为 unknown。
# 本补丁：
#   1. 对每日明细接口尝试多种参数组合，若只是参数变更则完整恢复全部字段；
#   2. 仍失败时用官方账单接口 charge/getAnalyzeFeeDetails 补回电费
#      （上月电费 = 账单真实值；当月 / 每日 = 按账单实际单价估算）；
#   3. 阶梯三项按本地阶梯规则估算（南网已不再下发）。
# 只需替换本文件；实体 ID、配置项、仪表盘均不变。
# ---------------------------------------------------------------------------
ENABLE_BILL_RECONCILIATION = True   # 改为 False 则电费维持原样（unknown）
ENABLE_LADDER_ESTIMATE = True       # 改为 False 则三个阶梯传感器维持 unknown

# 阶梯按月用电量分档，单位 kWh。默认值 = 广东电网居民生活用电
# （夏季 5-10 月 / 非夏季 11-4 月）。若不在广东，请改成当地标准。
LADDER_THRESHOLDS_SUMMER = (260, 600)
LADDER_THRESHOLDS_NON_SUMMER = (200, 400)

# 各档电价（元/kWh）。None = 使用官方账单反推的综合单价，不编造电价；
# 需要精确分档电价时改成例如 (0.68, 0.73, 0.98)。
LADDER_TARIFFS = None

# 账单接口结果缓存秒数，避免一次更新内重复请求
BILL_CACHE_TTL = 1800
# ---------------------------------------------------------------------------


class CSGAPIError(Exception):
    """Generic API errors"""

    def __init__(self, sta: str, msg: str | None = None) -> None:
        """sta: status code, msg: message"""
        Exception.__init__(self)
        self.sta = sta
        self.msg = msg

    def __str__(self):
        return f"<CSGAPIError sta={self.sta} message={self.msg}>"


class CSGHTTPError(CSGAPIError):
    """Unexpected HTTP status code (!=200)"""

    def __init__(self, code: int) -> None:
        CSGAPIError.__init__(self, sta=f"HTTP{code}")
        self.status_code = code

    def __str__(self) -> str:
        return f"<CSGHTTPError code={self.status_code}>"


class InvalidCredentials(CSGAPIError):
    """Wrong username+password combination (RESP_STA_LOGIN_WRONG_CREDENTIAL)"""

    def __str__(self):
        return f"<CSGInvalidCredentials sta={self.sta} message={self.msg}>"


class NotLoggedIn(CSGAPIError):
    """Not logged in or login expired (RESP_STA_NO_LOGIN)"""

    def __str__(self):
        return f"<CSGNotLoggedIn sta={self.sta} message={self.msg}>"


class QrCodeExpired(Exception):
    """QR code has expired"""


def generate_qr_login_id():
    """
    Generate a unique id for qr code login
    word-by-word copied from js code
    """
    rand_str = f"{int(time.time() * 1000)}{random.random()}"
    return md5(rand_str.encode()).hexdigest()


def encrypt_credential(password: str) -> str:
    """Use RSA+pubkey to encrypt password"""
    rsa_key = RSA.import_key(b64decode(CREDENTIAL_PUBKEY))
    credential_cipher = PKCS1_v1_5.new(rsa_key)
    encrypted_pwd = credential_cipher.encrypt(password.encode("utf8"))
    return b64encode(encrypted_pwd).decode()


def encrypt_params(
    params: dict,
    key: bytes = PARAM_KEY_APP,
    iv: bytes = PARAM_IV_APP,
) -> str:
    """Encrypt a request payload using the selected API profile material."""
    json_cipher = AES.new(key, AES.MODE_CBC, iv)

    def pad(content: str) -> str:
        return content + (16 - len(content) % 16) * "\x00"

    json_str = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    encrypted = json_cipher.encrypt(pad(json_str).encode("utf8"))
    return b64encode(encrypted).decode()


def decrypt_params(
    encrypted: str,
    key: bytes = PARAM_KEY_APP,
    iv: bytes = PARAM_IV_APP,
) -> dict:
    """Decrypt a response payload using the selected API profile material."""
    json_cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = json_cipher.decrypt(b64decode(encrypted))
    # remove padding
    params = json.loads(decrypted.decode().strip("\x00"))
    return params


class CSGElectricityAccount:
    """Represents one electricity account, identified by account number (缴费号)"""

    def __init__(
        self,
        account_number: str | None = None,
        area_code: str | None = None,
        ele_customer_id: str | None = None,
        metering_point_id: str | None = None,
        metering_point_number: str | None = None,
        address: str | None = None,
        user_name: str | None = None,
    ) -> None:
        # the parameters are independent for each electricity account

        # the 16-digit billing number, as a unique identifier, not used in api for now
        self.account_number = account_number

        self.area_code = area_code

        # this may change on every login, alternative name in js code is `binding_id`
        self.ele_customer_id = ele_customer_id

        # in fact one account may have multiple metering points,
        # however for individual users there should only be one
        self.metering_point_id = metering_point_id
        self.metering_point_number = metering_point_number

        # for frontend display only
        self.address = address
        self.user_name = user_name

    def dump(self) -> dict[str, str]:
        """serialize this object"""
        return {
            ATTR_ACCOUNT_NUMBER: self.account_number,
            ATTR_AREA_CODE: self.area_code,
            ATTR_ELE_CUSTOMER_ID: self.ele_customer_id,
            ATTR_METERING_POINT_ID: self.metering_point_id,
            ATTR_METERING_POINT_NUMBER: self.metering_point_number,
            ATTR_ADDRESS: self.address,
            ATTR_USER_NAME: self.user_name,
        }

    @staticmethod
    def load(data: dict) -> CSGElectricityAccount:
        """deserialize this object"""
        for k in (
            ATTR_ACCOUNT_NUMBER,
            ATTR_AREA_CODE,
            ATTR_ELE_CUSTOMER_ID,
            ATTR_METERING_POINT_ID,
            ATTR_ADDRESS,
            ATTR_USER_NAME,
        ):
            if k not in data:
                raise ValueError(f"Missing key {k}")
        # ATTR_METERING_POINT_NUMBER is added in later version, skip check here
        # TODO: add ATTR_METERING_POINT_NUMBER to the check in the future
        account = CSGElectricityAccount(
            account_number=data[ATTR_ACCOUNT_NUMBER],
            area_code=data[ATTR_AREA_CODE],
            ele_customer_id=data[ATTR_ELE_CUSTOMER_ID],
            metering_point_id=data[ATTR_METERING_POINT_ID],
            metering_point_number=data.get(ATTR_METERING_POINT_NUMBER),
            address=data[ATTR_ADDRESS],
            user_name=data[ATTR_USER_NAME],
        )
        return account


class CSGClient:
    """
    Implementation of APIs from CSG iOS app interface.
    Parameters and consts are from web app js, however, these interfaces are virtually the same

    Do not call any functions starts with _api unless you are certain about what you're doing

    How to use:
    First call one of the functions to login (see example code)
    Then call `CSGClient.initialize` *important
    To get all linked electricity accounts, call `get_all_electricity_accounts`
    Use the account objects to call the utility functions and wrapped api functions
    """

    def __init__(
        self,
        auth_token: str | None = None,
        api_profile: str = API_PROFILE_APP,
    ) -> None:
        if api_profile not in API_PROFILE_TO_BASE_PATH:
            raise ValueError(f"Unsupported API profile: {api_profile}")
        self._session: requests.Session = requests.Session()
        self.api_profile = api_profile
        self._api_base_path = API_PROFILE_TO_BASE_PATH[api_profile]
        self._common_headers = {
            "Host": "95598.csg.cn",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": (
                "https://95598.csg.cn"
                if self._api_base_path == BASE_PATH_WEB
                else "file://"
            ),
            HEADER_X_AUTH_TOKEN: "",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko)",
            HEADER_CUST_NUMBER: "",
            "Accept-Language": "zh-CN,cn;q=0.9",
        }

        self.auth_token = auth_token

        # identifier, need to be set in initialize()
        self.customer_number = None

    # begin internal utility functions
    def _make_request(
        self,
        path: str,
        payload: dict | None,
        with_auth: bool = True,
        method: str = "POST",
        custom_headers: dict | None = None,
        base_path: str | None = None,
        encrypt_payload: bool = False,
    ):
        """
        Function to make the http request to api endpoints
        can automatically add authentication header(s)
        """
        resolved_base_path = base_path or self._api_base_path
        if resolved_base_path not in ALLOWED_BASE_PATHS:
            raise ValueError("Refusing request to an untrusted API base path")
        _LOGGER.debug(
            "CSG API request path=%s profile=%s auth=%s method=%s",
            path,
            self.api_profile,
            with_auth,
            method,
        )
        url = resolved_base_path + path
        headers = copy(self._common_headers)
        if custom_headers:
            for _k, _v in custom_headers.items():
                headers[_k] = _v
        if with_auth:
            headers[HEADER_X_AUTH_TOKEN] = self.auth_token
            headers[HEADER_CUST_NUMBER] = self.customer_number
        if encrypt_payload:
            key, iv = self._crypto_material()
            payload = {JSON_KEY_PARAM: encrypt_params(payload or {}, key, iv)}
            headers["need-crypto"] = "true"
        if method == "POST":
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                _LOGGER.error(
                    "API call %s returned status code %d", path, response.status_code
                )
                raise CSGHTTPError(response.status_code)

            json_str = response.content.decode("utf-8", errors="ignore")
            json_str = json_str[json_str.find("{") : json_str.rfind("}") + 1]
            json_data = json.loads(json_str)
            response_data = json_data
            if (
                response.headers.get("need-decrypto")
                and isinstance(response_data.get(JSON_KEY_DATA), str)
            ):
                key, iv = self._crypto_material()
                response_data[JSON_KEY_DATA] = decrypt_params(
                    response_data[JSON_KEY_DATA], key, iv
                )
            _LOGGER.debug(
                "CSG API response path=%s status=%s",
                path,
                response_data.get(JSON_KEY_STA),
            )

            # headers need to be returned since they may contain additional data
            return response.headers, response_data

        raise NotImplementedError()

    def _crypto_material(self) -> tuple[bytes, bytes]:
        """Return crypto material for the authenticated API channel."""
        if self.api_profile == API_PROFILE_WEB:
            return PARAM_KEY_WEB, PARAM_IV_WEB
        return PARAM_KEY_APP, PARAM_IV_APP

    def _handle_unsuccessful_response(self, api_path: str, response_data: dict):
        """Handles sta=!RESP_STA_SUCCESS"""
        _LOGGER.debug(
            "CSG API unsuccessful path=%s status=%s",
            api_path,
            response_data.get(JSON_KEY_STA),
        )

        if response_data[JSON_KEY_STA] == RESP_STA_NO_LOGIN:
            raise NotLoggedIn(
                response_data[JSON_KEY_STA], response_data.get(JSON_KEY_MESSAGE)
            )
        raise CSGAPIError(
            response_data[JSON_KEY_STA], response_data.get(JSON_KEY_MESSAGE)
        )

    # end internal utility functions

    # begin raw api functions
    def api_send_login_sms(self, phone_no: str):
        """Send SMS verification code to phone_no
        Note this is not the function for login with SMS, it only requests to send the code
        """
        path = "center/sendMsg"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            "phoneNumber": phone_no,
            "vcType": VERIFICATION_CODE_TYPE_LOGIN,
            "msgType": SEND_MSG_TYPE_VERIFICATION_CODE,
        }
        _, resp_data = self._make_request(path, payload, with_auth=False)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return True
        self._handle_unsuccessful_response(path, resp_data)

    def api_create_login_qr_code(
        self, login_type: LoginType, login_id: str | None = None
    ) -> tuple[str, str]:
        """Request API to create a QR code for login
        Returns login_id and link to QR code image
        """
        login_type = LoginType(login_type)
        channel = LOGIN_TYPE_TO_QR_CODE_TYPE[login_type]
        login_id = login_id or generate_qr_login_id()
        custom_headers = None
        if login_type == LoginType.LOGIN_TYPE_WX_QR:
            path = "center/createLoginQrcode"
            base_path = BASE_PATH_WEB
            payload = {
                JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
                "channel": channel,
                "channelCode": LOGON_CHANNEL_HANDHELD_HALL,
                # NOTE: this spelling error is required by the legacy endpoint.
                "lgoinId": login_id,
            }
        else:
            path = "user/manage/createLoginQrcode"
            base_path = BASE_PATH_QR_NEW
            payload = {
                "channel": channel,
                "channelCode": LOGON_CHANNEL_HANDHELD_HALL,
                "loginId": login_id,
            }
            custom_headers = {"channelCode": LOGON_CHANNEL_ONLINE_HALL}
        _, resp_data = self._make_request(
            path,
            payload,
            with_auth=False,
            custom_headers=custom_headers,
            base_path=base_path,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return login_id, resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_qr_login_status(
        self, login_id: str, login_type: LoginType
    ) -> tuple[bool, str]:
        """Get login status of the QR code"""
        login_type = LoginType(login_type)
        custom_headers = None
        if login_type == LoginType.LOGIN_TYPE_WX_QR:
            path = "center/getLoginInfo"
            base_path = BASE_PATH_WEB
            payload = {
                JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
                "loginId": login_id,
            }
        else:
            path = "user/manage/getLoginInfo"
            base_path = BASE_PATH_QR_NEW
            payload = {
                "channelCode": LOGON_CHANNEL_HANDHELD_HALL,
                "loginId": login_id,
            }
            custom_headers = {"channelCode": LOGON_CHANNEL_ONLINE_HALL}
        resp_header, resp_data = self._make_request(
            path,
            payload,
            with_auth=False,
            custom_headers=custom_headers,
            base_path=base_path,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            auth_token = resp_header.get(HEADER_X_AUTH_TOKEN)
            if not auth_token:
                raise CSGAPIError("missing_auth_token", "Login response had no token")
            return True, auth_token
        if resp_data[JSON_KEY_STA] == RESP_STA_QR_NOT_SCANNED:
            return False, ""
        if resp_data[JSON_KEY_STA] == RESP_STA_QR_TIMEOUT:
            raise QrCodeExpired()
        self._handle_unsuccessful_response(path, resp_data)

    def api_login_with_sms_code(self, phone_no: str, sms_code: str):
        """Login with phone number and SMS code"""
        path = "center/login"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            JSON_KEY_ACCT_ID: phone_no,
            JSON_KEY_LOGON_CHAN: LOGON_CHANNEL_HANDHELD_HALL,
            JSON_KEY_CRED_TYPE: LOGIN_TYPE_PHONE_CODE,
            JSON_KEY_SMS_CODE: sms_code,
        }
        payload = {JSON_KEY_PARAM: encrypt_params(payload)}
        resp_header, resp_data = self._make_request(
            path, payload, with_auth=False, custom_headers={"need-crypto": "true"}
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_header[HEADER_X_AUTH_TOKEN]
        self._handle_unsuccessful_response(path, resp_data)

    def api_login_with_password_and_sms_code(
        self, phone_no: str, password: str, sms_code: str
    ):
        """Login with phone number, SMS code and password"""
        path = "center/loginByPwdAndMsg"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            JSON_KEY_ACCT_ID: phone_no,
            JSON_KEY_LOGON_CHAN: LOGON_CHANNEL_HANDHELD_HALL,
            JSON_KEY_CRED_TYPE: LOGIN_TYPE_PHONE_PWD_CODE,
            "credentials": encrypt_credential(password),
            JSON_KEY_SMS_CODE: sms_code,
            "checkPwd": True,
        }
        payload = {JSON_KEY_PARAM: encrypt_params(payload)}
        resp_header, resp_data = self._make_request(
            path, payload, with_auth=False, custom_headers={"need-crypto": "true"}
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_header[HEADER_X_AUTH_TOKEN]
        if resp_data[JSON_KEY_STA] == RESP_STA_LOGIN_WRONG_CREDENTIAL:
            raise InvalidCredentials(
                resp_data[JSON_KEY_STA], resp_data.get(JSON_KEY_MESSAGE)
            )
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_authentication_result(self) -> dict[str, Any]:
        """Contains custNumber, used to verify login"""
        path = "user/queryAuthenticationResult"
        payload = {}
        _, resp_data = self._make_request(
            path,
            payload,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_user_info(self) -> dict[str, Any]:
        """Get account info"""
        path = "user/getUserInfo"
        payload = {}
        _, resp_data = self._make_request(
            path,
            payload,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_all_linked_electricity_accounts(self) -> list[dict[str, Any]]:
        """List all linked electricity accounts under this account"""
        path = "eleCustNumber/queryBindEleUsers"
        _, resp_data = self._make_request(path, {})
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            _LOGGER.debug(
                "Total %d users under this account", len(resp_data[JSON_KEY_DATA])
            )
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_metering_point(
        self,
        area_code: str,
        ele_customer_id: str,
    ) -> dict:
        """Get metering point id"""
        path = "charge/queryMeteringPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "eleCustNumberList": [
                {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
            ],
        }
        # custom_headers = {"funid": "100t002"}
        custom_headers = {}
        _, resp_data = self._make_request(
            path,
            payload,
            custom_headers=custom_headers,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_day_electric_by_m_point(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get usage(kWh) by day in the given month"""
        path = "charge/queryDayElectricByMPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        # custom_headers = {"funid": "100t002"}
        custom_headers = {}
        _, resp_data = self._make_request(
            path,
            payload,
            custom_headers=custom_headers,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_day_electric_and_temperature(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get power in kWh, hi/lo temperature by day in the given month"""
        path = "charge/queryDayElectricAndTemperature"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_electricity_calender(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
        metering_point_number: str,
    ) -> dict:
        """get power in kWh, hi/lo/avg temperature by day in the given month"""
        path = "charge/queryElectricityCalendar"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
            "deviceIdentif": metering_point_number,
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_account_surplus(self, area_code: str, ele_customer_id: str):
        """Contains: balance and arrears"""
        path = "charge/queryUserAccountNumberSurplus"
        payload = {JSON_KEY_AREA_CODE: area_code, JSON_KEY_ELE_CUST_ID: ele_customer_id}
        _, resp_data = self._make_request(
            path,
            payload,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_fee_analyze_details(
        self,
        year: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_number: str | None,
    ):
        """
        Contains: year total kWh, year total charge, kWh/charge by month in current year
        """
        path = "charge/getAnalyzeFeeDetails"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "electricityBillYear": year,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_METERING_POINT_ID: metering_point_number,
        }
        _, resp_data = self._make_request(
            path,
            payload,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_charges(self, area_code: str, ele_customer_id: str, _type="0"):
        """Contains: balance and arrears, metering points"""
        path = "charge/queryCharges"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "eleModels": [
                {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
            ],
            "type": _type,
        }
        _, resp_data = self._make_request(
            path,
            payload,
            encrypt_payload=self.api_profile == API_PROFILE_WEB,
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_logout(self, logon_chan: str, cred_type: LoginType) -> None:
        """logout"""
        path = "center/logout"
        payload = {JSON_KEY_LOGON_CHAN: logon_chan, JSON_KEY_CRED_TYPE: cred_type}
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    # end raw api functions

    # begin utility functions
    @staticmethod
    def load(data: dict[str, str]) -> CSGClient:
        """
        Restore the session info to client object
        The validity of the session won't be checked
        `initialize()` needs to be called for the client to be usable
        """
        for k in (ATTR_AUTH_TOKEN,):
            if not data.get(k):
                raise ValueError(f"missing parameter: {k}")
        client = CSGClient(
            auth_token=data[ATTR_AUTH_TOKEN],
            api_profile=data.get(ATTR_API_PROFILE, API_PROFILE_APP),
        )
        return client

    def dump(self) -> dict[str, Any]:
        """Dump the session to dict"""
        return {
            ATTR_AUTH_TOKEN: self.auth_token,
            ATTR_API_PROFILE: self.api_profile,
        }

    def set_authentication_params(self, auth_token: str):
        """Set self.auth_token and client generated cookies"""
        self.auth_token = auth_token

    def initialize(self):
        """Initialize the client"""
        resp_data = self.api_get_user_info()
        self.customer_number = resp_data[JSON_KEY_CUST_NUMBER]

    def verify_login(self) -> bool:
        """Verify validity of the session"""
        try:
            self.api_query_authentication_result()
        except NotLoggedIn:
            return False
        return True

    def logout(self, login_type: LoginType):
        """Logout and reset identifier, token etc."""
        logon_channel = (
            LOGON_CHANNEL_ONLINE_HALL
            if self.api_profile == API_PROFILE_WEB
            else LOGON_CHANNEL_HANDHELD_HALL
        )
        self.api_logout(logon_channel, login_type)
        self.auth_token = None
        self.customer_number = None

    # end utility functions

    # begin high-level api wrappers

    def get_all_electricity_accounts(self) -> list[CSGElectricityAccount]:
        """Get all electricity accounts linked to current account"""
        result = []
        ele_user_resp_data = self.api_get_all_linked_electricity_accounts()

        for item in ele_user_resp_data:
            metering_point_data = self.api_get_metering_point(
                item[JSON_KEY_AREA_CODE], item["bindingId"]
            )
            metering_point_id = metering_point_data[0][JSON_KEY_METERING_POINT_ID]
            metering_point_number = metering_point_data[0][
                JSON_KEY_METERING_POINT_NUMBER
            ]
            account = CSGElectricityAccount(
                account_number=item["eleCustNumber"],
                area_code=item[JSON_KEY_AREA_CODE],
                ele_customer_id=item["bindingId"],
                metering_point_id=metering_point_id,
                metering_point_number=metering_point_number,
                address=item["eleAddress"],
                user_name=item["userName"],
            )
            result.append(account)
        return result

    def _official_monthly_bills(
        self, account: CSGElectricityAccount, year: int
    ) -> dict[str, tuple[float, float]]:
        """Return {"YYYYMM": (charge, kWh)} from the maintained official bill API.

        charge/getAnalyzeFeeDetails still works even though the daily-charge
        endpoint was withdrawn. Result is cached briefly because the coordinator
        asks for this month, last month and yesterday in the same update pass.
        """
        cache = getattr(self, "_bill_cache", None)
        if cache is None:
            cache = {}
            self._bill_cache = cache
        key = (year, account.metering_point_number or account.account_number)
        cached = cache.get(key)
        now = time.time()
        if cached and now - cached[0] < BILL_CACHE_TTL:
            return cached[1]

        rows: dict[str, tuple[float, float]] = {}
        try:
            data = self.api_get_fee_analyze_details(
                year,
                account.area_code,
                account.ele_customer_id,
                account.metering_point_number,
            )
            for item in (data or {}).get("electricAndChargeList") or []:
                raw_ym = str(item.get(JSON_KEY_YEAR_MONTH) or "")
                digits = "".join(ch for ch in raw_ym if ch.isdigit())
                if len(digits) >= 6:
                    ym = digits[:6]
                elif 0 < len(digits) <= 2:
                    ym = f"{year}{int(digits):02d}"
                else:
                    continue
                try:
                    charge = float(item.get("actualTotalAmount") or 0)
                    kwh = float(item.get("billingElectricity") or 0)
                except (TypeError, ValueError):
                    continue
                if charge > 0:
                    rows[ym] = (charge, kwh)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Official bill lookup failed for year %s: %s", year, err)
            if cached:
                return cached[1]
            return {}

        cache[key] = (now, rows)
        return rows

    @staticmethod
    def _bill_unit_price(
        rows: dict[str, tuple[float, float]], target_ym: str
    ) -> float | None:
        """Pick a real average price (CNY/kWh) from the newest settled bill."""
        if target_ym in rows and rows[target_ym][1] > 0:
            return rows[target_ym][0] / rows[target_ym][1]
        for ym in sorted(rows, reverse=True):
            charge, kwh = rows[ym]
            if kwh > 0 and charge > 0:
                return charge / kwh
        return None

    def _reconcile_cost_data(
        self,
        account: CSGElectricityAccount,
        year: int,
        month: int,
        month_cost: float | None,
        month_kwh: float | None,
        by_day: list[dict],
    ) -> tuple[float | None, float | None]:
        """Fill in missing charge values using the official bill API."""
        if not ENABLE_BILL_RECONCILIATION:
            return month_cost, month_kwh
        if month_cost is not None and by_day and all(
            item.get(WF_ATTR_CHARGE) is not None for item in by_day
        ):
            # Full official payload: nothing to reconstruct, skip the extra call.
            return month_cost, month_kwh

        rows = self._official_monthly_bills(account, year)
        target_ym = f"{year}{month:02d}"
        official = rows.get(target_ym)
        if official and official[0] > 0 and month_cost is None:
            # Settled month: use the exact billed amount, not an estimate.
            month_cost = round(official[0], 2)
            _LOGGER.debug(
                "Using official billed charge %s for %s", official[0], target_ym
            )

        unit_price = self._bill_unit_price(rows, target_ym)
        if not unit_price or unit_price <= 0:
            _LOGGER.debug("No usable bill price for %s", target_ym)
            return month_cost, month_kwh

        if month_kwh is None and by_day:
            month_kwh = sum(
                float(item.get(WF_ATTR_KWH) or 0) for item in by_day
            )
        if month_cost is None and month_kwh is not None:
            month_cost = round(month_kwh * unit_price, 2)
            _LOGGER.debug(
                "Estimated charge %s for %s from %.4f CNY/kWh",
                month_cost, target_ym, unit_price,
            )

        for item in by_day:
            if item.get(WF_ATTR_CHARGE) is None:
                item[WF_ATTR_CHARGE] = round(
                    float(item.get(WF_ATTR_KWH) or 0) * unit_price, 2
                )
        return month_cost, month_kwh

    def _estimate_ladder(
        self,
        year: int,
        month: int,
        month_kwh: float | None,
        unit_price: float | None,
        ladder: dict,
    ) -> dict:
        """Derive ladder values locally when CSG no longer publishes them."""
        if (
            not ENABLE_LADDER_ESTIMATE
            or ladder.get(WF_ATTR_LADDER) is not None
            or month_kwh is None
        ):
            return ladder

        first, second = (
            LADDER_THRESHOLDS_SUMMER
            if 5 <= month <= 10
            else LADDER_THRESHOLDS_NON_SUMMER
        )
        if month_kwh <= first:
            stage, remaining = 1, first - month_kwh
        elif month_kwh <= second:
            stage, remaining = 2, second - month_kwh
        else:
            stage, remaining = 3, 0.0

        tariff = LADDER_TARIFFS[stage - 1] if LADDER_TARIFFS else unit_price
        ladder[WF_ATTR_LADDER] = stage
        ladder[WF_ATTR_LADDER_REMAINING_KWH] = round(max(0.0, remaining), 2)
        ladder[WF_ATTR_LADDER_TARIFF] = round(tariff, 4) if tariff else None
        ladder[WF_ATTR_LADDER_START_DATE] = datetime.datetime(year, month, 1)
        _LOGGER.debug(
            "Estimated ladder stage %s, remaining %.2f kWh, tariff %s for %s-%02d",
            stage, remaining, tariff, year, month,
        )
        return ladder

    def get_month_daily_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float | None, float | None, dict, list[dict[str, str | float]]]:
        """Get current month totals and daily usage from the maintained endpoint.

        The current web application exposes totalElectricity and totalPower from
        queryDayElectricByMPoint. The former daily-charge endpoint was removed.
        Daily charge is therefore included only when the maintained response
        happens to provide it; callers must treat it as optional.
        """

        year, month = year_month
        resp_data = self._get_daily_data(account, year, month)

        def optional_float(value: Any) -> float | None:
            return None if value in (None, "") else float(value)

        by_day = []
        for d_data in resp_data.get("result") or []:
            item = {
                WF_ATTR_DATE: d_data["date"],
                WF_ATTR_KWH: float(d_data["power"]),
            }
            if d_data.get("charge") not in (None, ""):
                item[WF_ATTR_CHARGE] = float(d_data["charge"])
            by_day.append(item)

        ladder_start_date = None
        raw_start_date = resp_data.get("ladderEleStartDate")
        if raw_start_date:
            try:
                ladder_start_date = datetime.datetime.fromisoformat(raw_start_date)
            except ValueError:
                _LOGGER.debug("Ignoring an unparseable ladder start date")

        ladder = {
            WF_ATTR_LADDER: (
                int(resp_data["ladderEle"])
                if resp_data.get("ladderEle") not in (None, "")
                else None
            ),
            WF_ATTR_LADDER_START_DATE: ladder_start_date,
            WF_ATTR_LADDER_REMAINING_KWH: optional_float(
                resp_data.get("ladderEleSurplus")
            ),
            WF_ATTR_LADDER_TARIFF: optional_float(resp_data.get("ladderEleTariff")),
        }

        month_cost = optional_float(resp_data.get("totalElectricity"))
        month_kwh = optional_float(resp_data.get("totalPower"))
        month_cost, month_kwh = self._reconcile_cost_data(
            account, year, month, month_cost, month_kwh, by_day
        )
        if ENABLE_LADDER_ESTIMATE and ladder.get(WF_ATTR_LADDER) is None:
            rows = (
                self._official_monthly_bills(account, year)
                if ENABLE_BILL_RECONCILIATION
                else {}
            )
            unit_price = self._bill_unit_price(rows, f"{year}{month:02d}")
            ladder = self._estimate_ladder(
                year, month, month_kwh, unit_price, ladder
            )

        return (month_cost, month_kwh, ladder, by_day)

    def get_month_daily_usage_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float | None, list[dict[str, str | float]]]:
        """Get daily usage of a month using the maintained combined endpoint."""
        _, month_total_kwh, _, by_day = self.get_month_daily_detail(
            account, year_month
        )
        return month_total_kwh, by_day

    def get_month_daily_cost_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float | None, float | None, dict, list[dict[str, str | float]]]:
        """Get monthly cost plus any available daily cost values."""
        return self.get_month_daily_detail(account, year_month)

    def get_balance_and_arrears(
        self, account: CSGElectricityAccount
    ) -> tuple[float, float]:
        """Get account balance and arrears"""

        resp_data = self.api_query_account_surplus(
            account.area_code, account.ele_customer_id
        )
        # The current web API returns an object; the legacy mobile API returned
        # a one-item list. Accept both during the migration window.
        account_data = resp_data[0] if isinstance(resp_data, list) else resp_data
        balance = account_data["balance"]
        arrears = account_data["arrears"]
        return float(balance), float(arrears)

    def get_year_month_stats(
        self, account: CSGElectricityAccount, year
    ) -> tuple[float, float, list[dict[str, str | float]]]:
        """Get year total kWh, year total charge, kWh/charge by month in current year"""

        resp_data = self.api_get_fee_analyze_details(
            year,
            account.area_code,
            account.ele_customer_id,
            account.metering_point_number,
        )

        total_year_kwh = resp_data["totalBillingElectricity"]
        total_year_charge = resp_data["totalActualAmount"]
        by_month = []
        for m_data in resp_data["electricAndChargeList"]:
            by_month.append(
                {
                    WF_ATTR_MONTH: m_data[JSON_KEY_YEAR_MONTH],
                    WF_ATTR_CHARGE: float(m_data["actualTotalAmount"]),
                    WF_ATTR_KWH: float(m_data["billingElectricity"]),
                }
            )
        return float(total_year_charge), float(total_year_kwh), by_month

    def get_years_month_stats(
        self, account: CSGElectricityAccount, years: list[int]
    ) -> list[dict[str, str | float]]:
        """Return official monthly usage and charge rows for multiple years."""
        by_month = []
        for year in years:
            _, _, year_rows = self.get_year_month_stats(account, year)
            by_month.extend(year_rows)
        return sorted(by_month, key=lambda item: item[WF_ATTR_MONTH])

    def _get_daily_data(self, account, year, month):
        """Fetch daily detail, tolerating CSG's withdrawn daily-charge endpoint.

        The web app moved to queryElectricityCalendar, but that response only
        carries kWh. Try a few parameter variants of the old endpoint first:
        when CSG merely changed the metering-point identifier, the full payload
        (charge + ladder) comes back and no estimation is needed.
        """
        year_month = f"{year}{month:02d}"
        common = {
            JSON_KEY_AREA_CODE: account.area_code,
            JSON_KEY_ELE_CUST_ID: account.ele_customer_id,
            JSON_KEY_YEAR_MONTH: year_month,
        }
        number = account.metering_point_number
        candidates = [
            {**common, JSON_KEY_METERING_POINT_ID: account.metering_point_id},
            {
                **common,
                JSON_KEY_METERING_POINT_ID: account.metering_point_id,
                "deviceIdentif": number or account.metering_point_id,
            },
        ]
        if number and number != account.metering_point_id:
            candidates.append({**common, JSON_KEY_METERING_POINT_ID: number})
            candidates.append(
                {
                    **common,
                    JSON_KEY_METERING_POINT_ID: number,
                    "deviceIdentif": number,
                }
            )

        def calendar_fallback():
            return self.api_query_electricity_calender(
                year, month, account.area_code, account.ele_customer_id,
                account.metering_point_id, number,
            )

        if getattr(self, "_daily_endpoint_dead", False):
            return calendar_fallback()

        known_good = getattr(self, "_daily_variant_index", None)
        order = (
            list(range(known_good, len(candidates)))
            if known_good is not None
            else list(range(len(candidates)))
        )
        for index in order:
            try:
                _, resp_data = self._make_request(
                    "charge/queryDayElectricByMPoint",
                    candidates[index],
                    custom_headers={},
                    encrypt_payload=self.api_profile == API_PROFILE_WEB,
                )
                sta = resp_data.get(JSON_KEY_STA)
                if sta == RESP_STA_NO_LOGIN:
                    raise NotLoggedIn(sta, resp_data.get(JSON_KEY_MESSAGE))
                if sta == RESP_STA_SUCCESS:
                    data = resp_data.get(JSON_KEY_DATA) or {}
                    if data.get("result"):
                        if getattr(self, "_daily_variant_index", None) != index:
                            _LOGGER.info(
                                "CSG daily detail works with payload variant %s "
                                "(full charge/ladder data restored)", index
                            )
                        self._daily_variant_index = index
                        return data
            except NotLoggedIn:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug(
                    "CSG daily detail variant %s failed: %s", index, err
                )

        self._daily_endpoint_dead = True
        _LOGGER.info(
            "CSG daily-charge endpoint unavailable (sta=02); using calendar "
            "data plus official monthly bills to reconstruct charges"
        )
        return calendar_fallback()

    def get_yesterday_kwh(self, account: CSGElectricityAccount) -> float | None:
        """Derive yesterday's usage from the maintained monthly detail API."""
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        resp_data = self._get_daily_data(account, yesterday.year, yesterday.month)
        target_date = yesterday.isoformat()
        for item in resp_data.get("result") or []:
            if item.get("date") == target_date and item.get("power") not in (None, ""):
                return float(item["power"])
        # CSG documents a T+3 publication window, so an absent value is normal.
        return None

    # end high-level api wrappers
