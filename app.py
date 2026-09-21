from __future__ import annotations

import html
import io
import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests
import streamlit as st

import manage_page

# App-level settings. Most values can be overridden with environment variables
# so the same code can be used safely in different environments.
APP_TITLE = "MAC Controller — REST API"
REST_PORT = int(os.getenv("WLC_REST_PORT", "4343"))
CONNECT_TIMEOUT = float(os.getenv("WLC_CONNECT_TIMEOUT", "3"))
REQUEST_TIMEOUT = float(os.getenv("WLC_REQUEST_TIMEOUT", "8"))
MAX_BULK_ROWS = int(os.getenv("MAX_BULK_ROWS", "500"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024)))


def get_tls_verify_setting() -> bool | str:
    # Prefer a CA bundle when one is provided. Otherwise fall back to the
    # WLC_VERIFY_TLS flag. Never hard-code certificate paths or secrets here.
    ca_bundle = os.getenv("WLC_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle
    return os.getenv("WLC_VERIFY_TLS", "false").strip().lower() in {"1", "true", "yes", "on"}


TLS_VERIFY = get_tls_verify_setting()
if TLS_VERIFY is False:
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)


@dataclass
class ApiResult:
    ok: bool
    step: str
    message: str
    http_status: int | None = None
    details: Any = None

    def to_row(self) -> dict[str, Any]:
        return {
            "ขั้นตอน": self.step,
            "ผล": "PASS" if self.ok else "FAIL",
            "HTTP": self.http_status if self.http_status is not None else "-",
            "รายละเอียด": self.message,
        }


class ArubaRestClient:
    """ArubaOS REST API client."""

    def __init__(self, ip: str, username: str, password: str, config_path: str):
        self.ip = ip
        self.username = username
        self.password = password
        self.config_path = config_path
        self.base_url = f"https://{ip}:{REST_PORT}/v1"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.token: str | None = None

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"raw_text": response.text[:4000]}

    @staticmethod
    def _global_result(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("_global_result"), dict):
            return payload["_global_result"]
        return {}

    @staticmethod
    def _status_is_success(payload: Any) -> bool:
        global_result = ArubaRestClient._global_result(payload)
        if not global_result:
            return True
        return str(global_result.get("status", "0")) == "0"

    @staticmethod
    def _status_message(payload: Any) -> str:
        global_result = ArubaRestClient._global_result(payload)
        if global_result:
            return str(global_result.get("status_str") or global_result)
        return "Response ไม่มี _global_result"

    def login(self) -> ApiResult:
        # Authenticate with ArubaOS and keep the UIDARUBA token only in memory.
        try:
            response = self.session.post(
                f"{self.base_url}/api/login",
                data={"username": self.username, "password": self.password},
                verify=TLS_VERIFY,
                timeout=REQUEST_TIMEOUT,
            )
            payload = self._safe_json(response)
            global_result = self._global_result(payload)
            token = global_result.get("UIDARUBA") or global_result.get("UIDARUUB")
            status_ok = str(global_result.get("status", "1")) == "0"
            if response.status_code == 200 and status_ok and token:
                self.token = str(token)
                return ApiResult(True, "REST Login", "Login สำเร็จและได้รับ UIDARUBA token", response.status_code)
            return ApiResult(
                False,
                "REST Login",
                f"Login ไม่สำเร็จ: {self._status_message(payload)}",
                response.status_code,
                payload,
            )
        except requests.RequestException as exc:
            return ApiResult(False, "REST Login", f"เชื่อมต่อ Login endpoint ไม่ได้: {exc}")

    def show_command(
        self,
        command: str,
        step: str,
        timeout: float | None = None,
    ) -> ApiResult:
        # Read-only REST endpoint used for health checks and Local User DB queries.
        if not self.token:
            return ApiResult(False, step, "ไม่มี Token กรุณา Login ก่อน")
        try:
            response = self.session.get(
                f"{self.base_url}/configuration/showcommand",
                params={"command": command, "UIDARUBA": self.token},
                verify=TLS_VERIFY,
                timeout=timeout if timeout is not None else REQUEST_TIMEOUT,
            )
            payload = self._safe_json(response)
            ok = response.status_code == 200 and self._status_is_success(payload)
            if ok:
                return ApiResult(True, step, f"เรียก `{command}` ผ่าน REST API สำเร็จ", response.status_code, payload)
            return ApiResult(
                False,
                step,
                f"เรียก `{command}` ไม่สำเร็จ: {self._status_message(payload)}",
                response.status_code,
                payload,
            )
        except requests.RequestException as exc:
            return ApiResult(False, step, f"Showcommand request ล้มเหลว: {exc}")

    def post_object(self, object_name: str, payload: dict[str, Any], step: str) -> ApiResult:
        # Configuration endpoint used by MAC add/delete operations.
        if not self.token:
            return ApiResult(False, step, "ไม่มี Token กรุณา Login ก่อน")
        try:
            response = self.session.post(
                f"{self.base_url}/configuration/object/{object_name}",
                params={"config_path": self.config_path, "UIDARUBA": self.token},
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                verify=TLS_VERIFY,
                timeout=REQUEST_TIMEOUT,
            )
            response_payload = self._safe_json(response)
            ok = response.status_code == 200 and self._status_is_success(response_payload)
            if ok:
                return ApiResult(True, step, f"POST object `{object_name}` สำเร็จ", response.status_code, response_payload)
            return ApiResult(
                False,
                step,
                f"POST object `{object_name}` ไม่สำเร็จ: {self._status_message(response_payload)}",
                response.status_code,
                response_payload,
            )
        except requests.RequestException as exc:
            return ApiResult(False, step, f"POST object request ล้มเหลว: {exc}")

    def logout(self) -> ApiResult:
        if not self.token:
            self.session.close()
            return ApiResult(True, "REST Logout", "ไม่มี Session ที่ต้อง Logout")
        try:
            response = self.session.post(
                f"{self.base_url}/api/logout",
                params={"UIDARUBA": self.token},
                verify=TLS_VERIFY,
                timeout=REQUEST_TIMEOUT,
            )
            payload = self._safe_json(response)
            ok = response.status_code == 200 and self._status_is_success(payload)
            return ApiResult(
                ok,
                "REST Logout",
                "Logout สำเร็จ" if ok else f"Logout มีปัญหา: {self._status_message(payload)}",
                response.status_code,
                payload,
            )
        except requests.RequestException as exc:
            return ApiResult(False, "REST Logout", f"Logout request ล้มเหลว: {exc}")
        finally:
            self.token = None
            self.session.close()


def validate_mac(value: str, separator: str = ":") -> tuple[str, str]:
    # Accept common MAC formats, normalize to 12 hex digits, then re-format
    # according to the separator required by the selected Aruba role.
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", cleaned):
        raise ValueError("MAC Address ต้องเป็นเลขฐานสิบหกครบ 12 หลัก")
    cleaned = cleaned.lower()
    octets = [cleaned[index : index + 2] for index in range(0, 12, 2)]
    formatted = separator.join(octets) if separator else cleaned
    return cleaned, formatted


def mac_candidates(cleaned_mac: str, preferred: str | None = None) -> list[str]:
    """สร้าง username ที่เป็นไปได้ โดยให้รูปแบบตาม Role ที่เลือกมาก่อน."""
    octets = [cleaned_mac[index : index + 2] for index in range(0, 12, 2)]
    candidates = [preferred or "", cleaned_mac.lower(), ":".join(octets).lower(), "-".join(octets).lower()]
    unique: list[str] = []
    for value in candidates:
        value = str(value or "").strip().lower()
        if value and value not in unique:
            unique.append(value)
    return unique


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _normalize_possible_mac(value: Any) -> str | None:
    text = str(value or "").strip()
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", text).lower()
    return cleaned if re.fullmatch(r"[0-9a-f]{12}", cleaned) else None


def response_contains_mac(payload: Any, cleaned_mac: str) -> bool:
    """ตรวจ MAC จากฟิลด์ชื่อผู้ใช้จริงเท่านั้น เพื่อลด false positive จากข้อความ/คำสั่ง echo."""
    target = cleaned_mac.lower()
    username_keys = {"name", "username", "user", "useraccount", "userid"}

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = _normalized_key(key)
                if normalized_key in username_keys and _normalize_possible_mac(value) == target:
                    return True
                if walk(value):
                    return True
        elif isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(payload)


def find_matching_username(payload: Any, cleaned_mac: str) -> str | None:
    """คืน username ที่ Controller เก็บจริง โดยไม่สนใจว่าจะคั่นด้วย -, : หรือไม่คั่น."""
    target = cleaned_mac.lower()
    username_keys = {"name", "username", "user", "useraccount", "userid"}

    def walk(node: Any) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = _normalized_key(key)
                if normalized_key in username_keys and _normalize_possible_mac(value) == target:
                    return str(value).strip()
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(payload)


def verify_user_presence(
    client: ArubaRestClient,
    formatted_mac: str,
    cleaned_mac: str,
    should_exist: bool,
    step: str,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> ApiResult:
    # Aruba's runtime Local User DB may take a short moment to reflect changes,
    # so verification is retried before reporting ADD/DEL as failed.
    """ตรวจแบบเจาะจง username และ retry เพื่อรอ Controller อัปเดต Local User DB."""
    last_result: ApiResult | None = None
    for attempt in range(1, attempts + 1):
        result = client.show_command(f"show local-userdb username {formatted_mac}", step)
        last_result = result
        if result.ok:
            found = response_contains_mac(result.details, cleaned_mac)
            if found == should_exist:
                result.message = (
                    f"ตรวจสอบสำเร็จในรอบ {attempt}/{attempts}: พบ MAC ใน Local User DB"
                    if should_exist
                    else f"ตรวจสอบสำเร็จในรอบ {attempt}/{attempts}: ไม่พบ MAC ใน Local User DB แล้ว"
                )
                return result
        if attempt < attempts:
            time.sleep(delay_seconds)

    if last_result is None:
        return ApiResult(False, step, "ไม่สามารถเริ่มขั้นตอนตรวจสอบได้")

    if last_result.ok:
        found = response_contains_mac(last_result.details, cleaned_mac)
        last_result.ok = False
        if should_exist and not found:
            last_result.message = (
                f"REST ตอบสำเร็จ แต่ตรวจ {attempts} รอบแล้วยังไม่พบ MAC ใน Local User DB"
            )
        elif not should_exist and found:
            last_result.message = (
                f"REST ตอบสำเร็จ แต่ตรวจ {attempts} รอบแล้วยังพบ MAC อยู่ใน Local User DB "
                "โปรดตรวจ Config Path/ตำแหน่งฐานข้อมูลของ Controller"
            )
        else:
            last_result.message = f"ตรวจสอบสถานะ MAC ไม่สำเร็จหลังลอง {attempts} รอบ"
    return last_result


def password_for_controller(controller: dict[str, str], index: int) -> str:
    # Runtime password entered in Streamlit has priority. If absent, read from
    # Streamlit Secrets / environment variables via manage_page.py.
    widget_value = st.session_state.get(f"runtime_password_{index}", "")
    if widget_value:
        return str(widget_value)
    return manage_page.get_saved_password(controller, index)


def tcp_test(ip: str) -> ApiResult:
    started = time.perf_counter()
    try:
        with socket.create_connection((ip, REST_PORT), timeout=CONNECT_TIMEOUT):
            elapsed_ms = (time.perf_counter() - started) * 1000
            return ApiResult(True, f"TCP {REST_PORT}", f"เปิด Port ได้ ({elapsed_ms:.1f} ms)")
    except OSError as exc:
        return ApiResult(False, f"TCP {REST_PORT}", f"ติดต่อ Port ไม่ได้: {exc}")


def run_read_only_test(controller: dict[str, str], password: str) -> tuple[list[ApiResult], dict[str, Any]]:
    # Connectivity test flow: TCP -> REST login -> show version -> sample user DB.
    results: list[ApiResult] = []
    debug: dict[str, Any] = {}

    port_result = tcp_test(controller["ip"])
    results.append(port_result)
    if not port_result.ok:
        return results, debug

    client = ArubaRestClient(
        controller["ip"],
        controller["username"],
        password,
        controller.get("config_path", "/md"),
    )
    try:
        login_result = client.login()
        results.append(login_result)
        if not login_result.ok:
            debug["login"] = login_result.details
            return results, debug

        show_version = client.show_command("show version", "Show Version")
        results.append(show_version)
        debug["show_version"] = show_version.details

        # อ่านเพียง 1 รายการเพื่อทดสอบสิทธิ์และลดโอกาส timeout จากฐานข้อมูลขนาดใหญ่
        show_userdb = client.show_command(
            "show local-userdb start 0 page 1",
            "Read Local User DB (Sample)",
            timeout=max(REQUEST_TIMEOUT, 15.0),
        )
        if show_userdb.ok:
            show_userdb.message = "อ่าน Local User DB ตัวอย่าง 1 รายการผ่าน REST API สำเร็จ"
        results.append(show_userdb)
        debug["show_local_userdb_sample"] = show_userdb.details
    finally:
        results.append(client.logout())

    return results, debug


def run_mac_operation(
    controller: dict[str, str],
    password: str,
    cleaned_mac: str,
    formatted_mac: str,
    role: str,
    action: str,
) -> tuple[list[ApiResult], dict[str, Any]]:
    # Single MAC workflow. ADD writes userdb_add; DELETE first finds the exact
    # stored username so separators (:, -, none) do not cause a wrong deletion.
    results: list[ApiResult] = []
    debug: dict[str, Any] = {"config_path": controller.get("config_path", "/md")}
    client = ArubaRestClient(
        controller["ip"],
        controller["username"],
        password,
        controller.get("config_path", "/md"),
    )

    try:
        login_result = client.login()
        results.append(login_result)
        if not login_result.ok:
            debug["login"] = login_result.details
            return results, debug

        username_for_verify = formatted_mac
        if action == "Add":
            payload: dict[str, Any] = {"name": formatted_mac, "passwd": formatted_mac, "role": role}
            operation_result = client.post_object("userdb_add", payload, "ADD MAC")
            verify_step = "ตรวจสอบหลัง ADD"
        elif action == "Delete":
            # ค้นหาแบบเจาะจงทีละ username แทนการดึง Local User DB ทั้งหมด
            # ช่วยลด timeout เมื่อฐานข้อมูลมีรายการจำนวนมาก
            lookup_debug: list[dict[str, Any]] = []
            stored_username: str | None = None
            lookup_had_success = False
            last_lookup_error = ""
            last_http_status: int | None = None

            for candidate in mac_candidates(cleaned_mac, formatted_mac):
                lookup_result = client.show_command(
                    f"show local-userdb username {candidate}",
                    "ค้นหา MAC ก่อน DEL",
                    timeout=max(REQUEST_TIMEOUT, 15.0),
                )
                last_http_status = lookup_result.http_status
                lookup_debug.append(
                    {
                        "candidate": candidate,
                        "ok": lookup_result.ok,
                        "http": lookup_result.http_status,
                        "message": lookup_result.message,
                        "response": lookup_result.details,
                    }
                )
                if not lookup_result.ok:
                    last_lookup_error = lookup_result.message
                    continue
                lookup_had_success = True
                stored_username = find_matching_username(lookup_result.details, cleaned_mac)
                if stored_username:
                    break

            debug["delete_lookup"] = lookup_debug
            if not stored_username:
                if lookup_had_success:
                    operation_result = ApiResult(
                        False,
                        "DEL MAC",
                        "ไม่พบ MAC นี้ใน Local User DB ของ Controller จึงไม่ได้ส่งคำสั่งลบ",
                        last_http_status,
                        lookup_debug,
                    )
                else:
                    operation_result = ApiResult(
                        False,
                        "DEL MAC",
                        "ตรวจสอบ Local User DB ก่อนลบไม่สำเร็จ: " + (last_lookup_error or "ไม่ทราบสาเหตุ"),
                        last_http_status,
                        lookup_debug,
                    )
            else:
                username_for_verify = stored_username
                operation_result = client.post_object("userdb_del", {"name": stored_username}, "DEL MAC")
                if operation_result.ok:
                    operation_result.message += f" (ลบ username ที่พบจริง: {stored_username})"
            verify_step = "ตรวจสอบหลัง DEL"
        else:
            results.append(ApiResult(False, "Action", "Action ไม่ถูกต้อง"))
            return results, debug

        results.append(operation_result)
        debug[action.lower()] = operation_result.details

        # Local user database เป็น Run-Time DB: ADD/DEL มีผลทันที จึงไม่เรียก write_memory
        if operation_result.ok:
            verify_result = verify_user_presence(
                client=client,
                formatted_mac=username_for_verify,
                cleaned_mac=cleaned_mac,
                should_exist=(action == "Add"),
                step=verify_step,
            )
            results.append(verify_result)
            debug["verify"] = verify_result.details
    finally:
        results.append(client.logout())

    return results, debug


def render_results(title: str, results: list[ApiResult], debug: dict[str, Any]) -> None:
    st.markdown(f"#### {title}")
    st.dataframe([result.to_row() for result in results], hide_index=True, use_container_width=True)
    if all(result.ok for result in results):
        st.success("การทำงานชุดนี้ผ่านทั้งหมด")
    else:
        st.error("พบอย่างน้อยหนึ่งขั้นตอนที่ไม่ผ่าน")
    if debug:
        with st.expander("Response สำหรับวิเคราะห์ปัญหา"):
            st.json(debug)


def new_manual_entry(default_role: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "role": default_role,
        "octets": ["", "", "", "", "", ""],
    }


def ensure_manual_entries(default_role: str) -> None:
    entries = st.session_state.get("manual_mac_entries")
    if not isinstance(entries, list) or not entries:
        st.session_state.manual_mac_entries = [new_manual_entry(default_role)]


def reset_manual_entries(default_role: str) -> None:
    st.session_state.manual_mac_entries = [new_manual_entry(default_role)]


def render_manual_mac_entry(
    entry: dict[str, Any],
    row_number: int,
    role_options: list[str],
    role_map: dict[str, str],
) -> tuple[dict[str, Any] | None, bool]:
    entry_id = str(entry.get("id") or uuid.uuid4().hex[:12])
    entry["id"] = entry_id

    current_role = str(entry.get("role") or role_options[0])
    if current_role not in role_options:
        current_role = role_options[0]
        entry["role"] = current_role

    with st.container(border=True):
        heading_col, role_col, remove_col = st.columns([1.4, 2.4, 0.8])
        with heading_col:
            st.markdown(f"**รายการที่ {row_number}**")
        with role_col:
            role_key = f"manual_role_{entry_id}"
            if role_key not in st.session_state:
                st.session_state[role_key] = current_role
            selected_role = st.selectbox(
                "Role",
                role_options,
                key=role_key,
                label_visibility="collapsed",
            )
            entry["role"] = selected_role
        with remove_col:
            remove_pressed = st.button(
                "ลบแถว",
                key=f"remove_manual_row_{entry_id}",
                use_container_width=True,
                disabled=len(st.session_state.manual_mac_entries) <= 1,
            )
            if remove_pressed:
                st.session_state.manual_mac_entries = [
                    item for item in st.session_state.manual_mac_entries if item.get("id") != entry_id
                ]
                st.rerun()

        separator = role_map[selected_role]
        st.text(
            f"Role {selected_role} ใช้ตัวคั่น: "
            f"{separator if separator else 'ไม่มีตัวคั่น'}"
        )

        octets = entry.get("octets")
        if not isinstance(octets, list) or len(octets) != 6:
            octets = ["", "", "", "", "", ""]
            entry["octets"] = octets

        if separator:
            widths: list[float] = []
            for octet_index in range(6):
                widths.append(2.0)
                if octet_index < 5:
                    widths.append(0.45)
            columns = st.columns(widths)
        else:
            columns = st.columns(6)

        values: list[str] = []
        position = 0
        for octet_index in range(6):
            widget_key = f"manual_octet_{entry_id}_{octet_index}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = str(octets[octet_index] or "")
            with columns[position]:
                value = st.text_input(
                    f"MAC รายการ {row_number} ช่อง {octet_index + 1}",
                    max_chars=2,
                    placeholder="00",
                    key=widget_key,
                    label_visibility="collapsed",
                ).strip()
            octets[octet_index] = value
            values.append(value)
            position += 1
            if separator and octet_index < 5:
                with columns[position]:
                    safe_separator_html = html.escape(separator)
                    st.markdown(
                        f"<div style='text-align:center;font-size:24px;font-weight:700;padding-top:3px'>{safe_separator_html}</div>",
                        unsafe_allow_html=True,
                    )
                position += 1

        entry["octets"] = octets
        is_blank = all(not value for value in values)
        invalid_positions = [
            index + 1
            for index, value in enumerate(values)
            if value and not re.fullmatch(r"[0-9A-Fa-f]{1,2}", value)
        ]
        incomplete_positions = [
            index + 1
            for index, value in enumerate(values)
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", value or "")
        ]

        if invalid_positions:
            st.error(
                "MAC format ผิด: ช่องที่ "
                + ", ".join(str(value) for value in invalid_positions)
                + " ต้องมีเฉพาะ 0-9 และ A-F"
            )
            return None, False

        if not is_blank and incomplete_positions:
            st.warning(
                "กรอก MAC ให้ครบช่องละ 2 ตัว — ยังไม่ครบที่ช่อง "
                + ", ".join(str(value) for value in incomplete_positions)
            )
            return None, False

        if is_blank:
            return None, False

        cleaned_mac, formatted_mac = validate_mac("".join(values), separator)
        st.info(f"MAC ที่จะส่ง: `{formatted_mac}`")
        return {
            "line": row_number,
            "cleaned_mac": cleaned_mac,
            "mac": formatted_mac,
            "role": selected_role,
        }, True

def bulk_template_dataframe(role_names: list[str]) -> pd.DataFrame:
    # Documentation-only sample values. Keep these as non-production examples.
    first_role = role_names[0] if role_names else "example-role-1"
    second_role = role_names[1] if len(role_names) > 1 else first_role
    return pd.DataFrame(
        {
            "MAC": ["02:00:00:00:00:01", "02:00:00:00:00:02"],
            "Role": [first_role, second_role],
        }
    )


def build_xlsx_bytes(dataframe: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name="MAC Import")
        worksheet = writer.sheets["MAC Import"]
        worksheet.column_dimensions["A"].width = 24
        worksheet.column_dimensions["B"].width = 24
    return output.getvalue()


def _read_csv(file_bytes: bytes) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp874"):
        try:
            return pd.read_csv(
                io.BytesIO(file_bytes),
                dtype=str,
                keep_default_na=False,
                encoding=encoding,
            )
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"อ่านไฟล์ CSV ไม่สำเร็จ: {last_error}")


def read_bulk_file(uploaded_file: Any) -> pd.DataFrame:
    # Limit upload size and allow only CSV/XLSX before parsing user input.
    file_bytes = uploaded_file.getvalue()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(f"ไฟล์ใหญ่เกินกำหนด ระบบรับได้ไม่เกิน {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    file_name = str(uploaded_file.name or "").lower()
    if file_name.endswith(".csv"):
        dataframe = _read_csv(file_bytes)
    elif file_name.endswith(".xlsx"):
        dataframe = pd.read_excel(io.BytesIO(file_bytes), dtype=str, keep_default_na=False, engine="openpyxl")
    else:
        raise ValueError("รองรับเฉพาะไฟล์ .csv และ .xlsx")

    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    dataframe = dataframe.fillna("")
    nonempty_column_mask = [
        any(str(value).strip() for value in dataframe.iloc[:, index])
        for index in range(len(dataframe.columns))
    ]
    dataframe = dataframe.loc[:, nonempty_column_mask]
    dataframe = dataframe[
        dataframe.apply(lambda row: any(str(value).strip() for value in row), axis=1)
    ].reset_index(drop=True)
    return dataframe


def validate_bulk_dataframe(
    dataframe: pd.DataFrame,
    role_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Validate the whole file first. No Controller request is sent while
    # formatting/role errors still exist.
    if dataframe.empty:
        return [], [{"บรรทัด": "-", "คอลัมน์": "-", "ค่าที่พบ": "-", "สาเหตุ": "ไฟล์ไม่มีข้อมูล"}]

    normalized_columns: dict[str, str] = {}
    for column in dataframe.columns:
        key = str(column).strip().lower()
        if key == "mac":
            normalized_columns[column] = "MAC"
        elif key == "role":
            normalized_columns[column] = "Role"
        else:
            normalized_columns[column] = str(column).strip()
    dataframe = dataframe.rename(columns=normalized_columns)

    if dataframe.columns.duplicated().any():
        duplicate_names = sorted(set(dataframe.columns[dataframe.columns.duplicated()].tolist()))
        return [], [
            {
                "บรรทัด": "Header",
                "คอลัมน์": ", ".join(duplicate_names),
                "ค่าที่พบ": "หัวคอลัมน์ซ้ำ",
                "สาเหตุ": "ห้ามมีคอลัมน์ MAC หรือ Role ซ้ำกัน",
            }
        ]

    required_columns = {"MAC", "Role"}
    actual_columns = set(dataframe.columns)
    missing = sorted(required_columns - actual_columns)
    unexpected = sorted(actual_columns - required_columns)
    format_errors: list[dict[str, Any]] = []

    if missing:
        format_errors.append(
            {
                "บรรทัด": "Header",
                "คอลัมน์": ", ".join(missing),
                "ค่าที่พบ": "ไม่มีคอลัมน์",
                "สาเหตุ": "ต้องมีหัวคอลัมน์ MAC และ Role",
            }
        )
    if unexpected:
        format_errors.append(
            {
                "บรรทัด": "Header",
                "คอลัมน์": ", ".join(unexpected),
                "ค่าที่พบ": "คอลัมน์เกิน",
                "สาเหตุ": "รูปแบบไฟล์อนุญาตเฉพาะคอลัมน์ MAC และ Role",
            }
        )
    if format_errors:
        return [], format_errors

    if len(dataframe) > MAX_BULK_ROWS:
        return [], [
            {
                "บรรทัด": "-",
                "คอลัมน์": "-",
                "ค่าที่พบ": len(dataframe),
                "สาเหตุ": f"จำนวนรายการเกิน {MAX_BULK_ROWS} รายการต่อรอบ",
            }
        ]

    role_lookup = {name.lower(): name for name in role_map}
    seen_macs: dict[str, int] = {}
    tasks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, row in dataframe.iterrows():
        line_number = index + 2
        mac_raw = str(row.get("MAC", "")).strip()
        role_raw = str(row.get("Role", "")).strip()

        if not mac_raw:
            errors.append(
                {
                    "บรรทัด": line_number,
                    "คอลัมน์": "MAC",
                    "ค่าที่พบ": "ว่าง",
                    "สาเหตุ": "ต้องระบุ MAC",
                }
            )
            continue
        if not role_raw:
            errors.append(
                {
                    "บรรทัด": line_number,
                    "คอลัมน์": "Role",
                    "ค่าที่พบ": "ว่าง",
                    "สาเหตุ": "ต้องระบุ Role",
                }
            )
            continue

        canonical_role = role_lookup.get(role_raw.lower())
        if not canonical_role:
            errors.append(
                {
                    "บรรทัด": line_number,
                    "คอลัมน์": "Role",
                    "ค่าที่พบ": role_raw,
                    "สาเหตุ": "Role ไม่มีในระบบ: " + ", ".join(role_map.keys()),
                }
            )
            continue

        try:
            cleaned_mac, formatted_mac = validate_mac(mac_raw, role_map[canonical_role])
        except ValueError as exc:
            errors.append(
                {
                    "บรรทัด": line_number,
                    "คอลัมน์": "MAC",
                    "ค่าที่พบ": mac_raw,
                    "สาเหตุ": str(exc),
                }
            )
            continue

        if cleaned_mac in seen_macs:
            errors.append(
                {
                    "บรรทัด": line_number,
                    "คอลัมน์": "MAC",
                    "ค่าที่พบ": mac_raw,
                    "สาเหตุ": f"MAC ซ้ำกับบรรทัด {seen_macs[cleaned_mac]}",
                }
            )
            continue

        seen_macs[cleaned_mac] = line_number
        tasks.append(
            {
                "line": line_number,
                "raw_mac": mac_raw,
                "cleaned_mac": cleaned_mac,
                "mac": formatted_mac,
                "role": canonical_role,
            }
        )

    return tasks, errors


def run_bulk_add(
    controller: dict[str, str],
    password: str,
    tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Reuse one authenticated REST session per Controller for the whole batch
    # to reduce login overhead and keep bulk operations predictable.
    client = ArubaRestClient(
        controller["ip"],
        controller["username"],
        password,
        controller.get("config_path", "/md"),
    )
    rows: list[dict[str, Any]] = []
    debug: dict[str, Any] = {"failures": []}

    try:
        login_result = client.login()
        if not login_result.ok:
            for task in tasks:
                rows.append(
                    {
                        "บรรทัด": task["line"],
                        "MAC": task["mac"],
                        "Role": task["role"],
                        "ADD": "FAIL",
                        "Verify": "SKIPPED",
                        "HTTP": login_result.http_status or "-",
                        "รายละเอียด": login_result.message,
                    }
                )
            debug["login"] = login_result.details
            return rows, debug

        operation_results: dict[str, ApiResult] = {}
        for task in tasks:
            result = client.post_object(
                "userdb_add",
                {"name": task["mac"], "passwd": task["mac"], "role": task["role"]},
                f"ADD {task['mac']}",
            )
            operation_results[task["cleaned_mac"]] = result
            if not result.ok:
                debug["failures"].append(
                    {
                        "line": task["line"],
                        "mac": task["mac"],
                        "role": task["role"],
                        "response": result.details,
                    }
                )

        # Local user database เป็น Run-Time DB: ไม่ต้อง write_memory
        verify_result = client.show_command("show local-userdb", "Verify Bulk ADD")
        debug["verify_error"] = None if verify_result.ok else verify_result.details

        for task in tasks:
            operation_result = operation_results[task["cleaned_mac"]]
            if not operation_result.ok:
                verify_text = "SKIPPED"
                detail = operation_result.message
            elif not verify_result.ok:
                verify_text = "FAIL"
                detail = verify_result.message
            elif response_contains_mac(verify_result.details, task["cleaned_mac"]):
                verify_text = "PASS"
                detail = "เพิ่มสำเร็จและตรวจพบใน Local User DB"
            else:
                verify_text = "FAIL"
                detail = "REST ตอบสำเร็จ แต่ตรวจไม่พบ MAC ใน Local User DB"

            rows.append(
                {
                    "บรรทัด": task["line"],
                    "MAC": task["mac"],
                    "Role": task["role"],
                    "ADD": "PASS" if operation_result.ok else "FAIL",
                    "Verify": verify_text,
                    "HTTP": operation_result.http_status or "-",
                    "รายละเอียด": detail,
                }
            )
    finally:
        logout_result = client.logout()
        if not logout_result.ok:
            debug["logout"] = logout_result.details

    if not debug["failures"]:
        debug.pop("failures", None)
    return rows, debug


def run_multi_mac_operation(
    controller: dict[str, str],
    password: str,
    tasks: list[dict[str, Any]],
    action: str,
) -> tuple[list[ApiResult], list[dict[str, Any]], dict[str, Any]]:
    summary: list[ApiResult] = []
    rows: list[dict[str, Any]] = []
    debug: dict[str, Any] = {"failures": [], "config_path": controller.get("config_path", "/md")}
    client = ArubaRestClient(
        controller["ip"],
        controller["username"],
        password,
        controller.get("config_path", "/md"),
    )

    try:
        login_result = client.login()
        summary.append(login_result)
        if not login_result.ok:
            debug["login"] = login_result.details
            for task in tasks:
                rows.append(
                    {
                        "รายการ": task["line"],
                        "MAC": task["mac"],
                        "Role": task["role"],
                        "คำสั่ง": "FAIL",
                        "ตรวจสอบ": "SKIPPED",
                        "HTTP": login_result.http_status or "-",
                        "รายละเอียด": login_result.message,
                    }
                )
            return summary, rows, debug

        delete_database_payload: Any = None
        delete_lookup_error: ApiResult | None = None
        if action == "Delete":
            lookup_result = client.show_command("show local-userdb", "ค้นหา MAC ก่อน DEL")
            if lookup_result.ok:
                delete_database_payload = lookup_result.details
            else:
                delete_lookup_error = lookup_result
                debug["delete_lookup"] = lookup_result.details

        operation_results: list[tuple[dict[str, Any], ApiResult, str]] = []
        for task in tasks:
            username_for_verify = task["mac"]
            if action == "Add":
                operation_result = client.post_object(
                    "userdb_add",
                    {"name": task["mac"], "passwd": task["mac"], "role": task["role"]},
                    f"ADD {task['mac']}",
                )
            else:
                if delete_lookup_error is not None:
                    operation_result = ApiResult(
                        False,
                        f"DEL {task['mac']}",
                        f"อ่าน Local User DB ก่อนลบไม่สำเร็จ: {delete_lookup_error.message}",
                        delete_lookup_error.http_status,
                        delete_lookup_error.details,
                    )
                else:
                    stored_username = find_matching_username(delete_database_payload, task["cleaned_mac"])
                    if not stored_username:
                        operation_result = ApiResult(
                            False,
                            f"DEL {task['mac']}",
                            "ไม่พบ MAC นี้ใน Local User DB ของ Controller จึงไม่ได้ส่งคำสั่งลบ",
                        )
                    else:
                        username_for_verify = stored_username
                        operation_result = client.post_object(
                            "userdb_del",
                            {"name": stored_username},
                            f"DEL {stored_username}",
                        )
                        if operation_result.ok:
                            operation_result.message += f" (ลบ username ที่พบจริง: {stored_username})"

            operation_results.append((task, operation_result, username_for_verify))
            if not operation_result.ok:
                debug["failures"].append(
                    {
                        "line": task["line"],
                        "mac": task["mac"],
                        "role": task["role"],
                        "response": operation_result.details,
                        "message": operation_result.message,
                    }
                )

        # Local user database เป็น Run-Time DB: ADD/DEL มีผลทันที จึงไม่เรียก write_memory
        for task, operation_result, username_for_verify in operation_results:
            if not operation_result.ok:
                verify_text = "SKIPPED"
                detail = operation_result.message
            else:
                verify_result = verify_user_presence(
                    client=client,
                    formatted_mac=username_for_verify,
                    cleaned_mac=task["cleaned_mac"],
                    should_exist=(action == "Add"),
                    step=f"ตรวจสอบ {task['mac']}",
                )
                verify_text = "PASS" if verify_result.ok else "FAIL"
                detail = verify_result.message
                if not verify_result.ok:
                    debug.setdefault("verify_failures", []).append(
                        {
                            "line": task["line"],
                            "mac": task["mac"],
                            "response": verify_result.details,
                        }
                    )

            rows.append(
                {
                    "รายการ": task["line"],
                    "MAC": task["mac"],
                    "Role": task["role"],
                    "คำสั่ง": "PASS" if operation_result.ok else "FAIL",
                    "ตรวจสอบ": verify_text,
                    "HTTP": operation_result.http_status or "-",
                    "รายละเอียด": detail,
                }
            )
    finally:
        logout_result = client.logout()
        summary.append(logout_result)
        if not logout_result.ok:
            debug["logout"] = logout_result.details

    if not debug.get("failures"):
        debug.pop("failures", None)
    return summary, rows, debug


def multi_operation_succeeded(
    dialog_results: dict[int, tuple[list[ApiResult], list[dict[str, Any]], dict[str, Any]]]
) -> bool:
    if not dialog_results:
        return False
    for summary, rows, _debug in dialog_results.values():
        if not all(result.ok for result in summary):
            return False
        if not rows:
            return False
        for row in rows:
            if row.get("คำสั่ง") != "PASS" or row.get("ตรวจสอบ") != "PASS":
                return False
    return True


@st.dialog("ผลการทำรายการ MAC")
def render_mac_operation_dialog(
    config_data: dict[str, Any],
    action: str,
    tasks: list[dict[str, Any]],
    operation_id: str,
) -> None:
    action_label = "ADD" if action == "Add" else "DEL"
    st.markdown(f"### {action_label} MAC จำนวน {len(tasks)} รายการ")
    st.caption("ผลลัพธ์จะแสดงเฉพาะในหน้าต่างนี้ เมื่อกดปิดจะไม่ค้างอยู่บนหน้าหลัก")

    cached_id = st.session_state.get("dialog_operation_id")
    if cached_id != operation_id:
        st.session_state.dialog_operation_id = operation_id
        st.session_state.dialog_operation_results = {}
        progress = st.progress(0, text="กำลังเตรียมส่งคำสั่ง")

        for index, controller in enumerate(config_data["controllers"]):
            password = password_for_controller(controller, index)
            progress.progress(index / 2, text=f"กำลัง {action_label} ที่ {controller['name']}")
            if not password:
                credential_result = ApiResult(
                    False,
                    "Credentials",
                    "ไม่พบ Password กรุณากำหนดใน secrets/ENV หรือหน้า REST API TEST",
                )
                rows = [
                    {
                        "รายการ": task["line"],
                        "MAC": task["mac"],
                        "Role": task["role"],
                        "คำสั่ง": "FAIL",
                        "ตรวจสอบ": "SKIPPED",
                        "HTTP": "-",
                        "รายละเอียด": credential_result.message,
                    }
                    for task in tasks
                ]
                st.session_state.dialog_operation_results[index] = ([credential_result], rows, {})
                continue

            st.session_state.dialog_operation_results[index] = run_multi_mac_operation(
                controller,
                password,
                tasks,
                action,
            )
            progress.progress((index + 1) / 2, text=f"เสร็จสิ้น {controller['name']}")
        progress.empty()

    dialog_results = st.session_state.get("dialog_operation_results", {})
    for index, controller in enumerate(config_data["controllers"]):
        if index not in dialog_results:
            continue
        summary, rows, debug = dialog_results[index]
        st.markdown(f"#### {controller['name']} — {controller['ip']}")
        st.dataframe(
            [result.to_row() for result in summary],
            hide_index=True,
            use_container_width=True,
        )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        passed = sum(
            1
            for row in rows
            if row.get("คำสั่ง") == "PASS" and row.get("ตรวจสอบ") == "PASS"
        )
        failed = len(rows) - passed
        metric_ok, metric_fail = st.columns(2)
        metric_ok.metric("สำเร็จ", passed)
        metric_fail.metric("ไม่สำเร็จ", failed)
        if failed:
            st.error(f"{controller['name']} มีรายการไม่สำเร็จ {failed} รายการ")
        else:
            st.success(f"{controller['name']} ทำรายการครบทุก MAC")
        if debug:
            with st.expander(f"Response สำหรับวิเคราะห์ปัญหา — {controller['name']}"):
                st.json(debug)
        st.divider()

    all_success = multi_operation_succeeded(dialog_results)
    if all_success:
        st.success("ทำรายการสำเร็จครบทั้งสอง Controller")
    else:
        st.error("พบอย่างน้อยหนึ่งรายการหรือหนึ่ง Controller ที่ไม่สำเร็จ")

    if st.button("ปิดหน้าต่าง", type="primary", use_container_width=True, key="close_mac_dialog"):
        st.session_state.pop("dialog_operation_id", None)
        st.session_state.pop("dialog_operation_results", None)
        if all_success:
            default_role = config_data.get("roles", [{}])[0].get("name", "")
            reset_manual_entries(default_role)
        st.rerun()

def render_bulk_results(controller: dict[str, str], rows: list[dict[str, Any]], debug: dict[str, Any]) -> None:
    st.markdown(f"#### {controller['name']} — {controller['ip']}")
    result_dataframe = pd.DataFrame(rows)
    st.dataframe(result_dataframe, hide_index=True, use_container_width=True)

    passed = sum(
        1
        for row in rows
        if row.get("ADD") == "PASS" and row.get("Verify") == "PASS"
    )
    failed = len(rows) - passed
    metric_pass, metric_fail = st.columns(2)
    metric_pass.metric("สำเร็จ", passed)
    metric_fail.metric("ไม่สำเร็จ", failed)

    if failed:
        st.error(f"{controller['name']} มีรายการไม่สำเร็จ {failed} รายการ")
        failed_dataframe = result_dataframe[
            (result_dataframe["ADD"] != "PASS") | (result_dataframe["Verify"] != "PASS")
        ]
        st.download_button(
            "ดาวน์โหลดรายการที่ไม่สำเร็จเป็น CSV",
            data=failed_dataframe.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"bulk_failed_{controller['name'].lower().replace(' ', '_')}.csv",
            mime="text/csv",
            use_container_width=True,
            key=f"download_failed_{controller['name']}",
        )
    else:
        st.success(f"{controller['name']} เพิ่มครบทุก MAC")

    if debug:
        with st.expander("Response ที่เกี่ยวข้องกับข้อผิดพลาด"):
            st.json(debug)


st.set_page_config(page_title=APP_TITLE, page_icon="🔌", layout="wide")
st.markdown(
    """
    <style>
    div[data-testid="stTextInput"] input { text-align: center; font-weight: 600; }
    div[data-testid="stDialog"] div[role="dialog"] {
        width: min(1180px, 96vw) !important;
        max-width: min(1180px, 96vw) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title(APP_TITLE)
st.caption("ระบบจัดการ MAC ผ่าน ArubaOS REST API บน HTTPS Port 4343 เท่านั้น")

config = manage_page.load_config()

if "rest_test_results" not in st.session_state:
    st.session_state.rest_test_results = {}
if "bulk_operation_results" not in st.session_state:
    st.session_state.bulk_operation_results = {}

rest_tab, settings_tab, mac_tab = st.tabs(
    ["1. REST API TEST", "2. Controller Settings", "3. Add/Del MAC"]
)

with rest_tab:
    st.subheader("REST API TEST")
    st.info(
        "ระบบจะตรวจ TCP 4343 → Login → show version → อ่าน Local User DB ตัวอย่าง 1 รายการ → Logout "
        "โดยไม่เปลี่ยน Configuration และไม่ดึงฐานข้อมูลทั้งหมด"
    )

    password_columns = st.columns(2)
    for index, controller in enumerate(config["controllers"]):
        with password_columns[index]:
            st.markdown(f"### {controller['name']}")
            st.code(
                f"IP: {controller['ip']}\nUsername: {controller['username']}\nConfig Path: {controller.get('config_path', '/md')}"
            )
            saved_exists = bool(manage_page.get_saved_password(controller, index))
            st.text_input(
                "Password สำหรับ Controller",
                type="password",
                key=f"runtime_password_{index}",
                placeholder="เว้นว่างเพื่ออ่านจาก secrets/ENV" if saved_exists else "กรอกรหัสผ่าน",
            )
            if saved_exists:
                st.caption("พบ Password ใน secrets/ENV แล้ว สามารถเว้นช่องนี้ว่างได้")

    c1, c2, c3 = st.columns(3)
    with c1:
        run_c1 = st.button("ทดสอบ Controller 1", use_container_width=True)
    with c2:
        run_c2 = st.button("ทดสอบ Controller 2", use_container_width=True)
    with c3:
        run_all = st.button("ทดสอบทั้งสอง Controller", type="primary", use_container_width=True)

    indexes_to_run: list[int] = []
    if run_c1 or run_all:
        indexes_to_run.append(0)
    if run_c2 or run_all:
        indexes_to_run.append(1)

    for index in indexes_to_run:
        controller = config["controllers"][index]
        password = password_for_controller(controller, index)
        if not password:
            st.session_state.rest_test_results[index] = (
                [ApiResult(False, "Credentials", "ไม่พบ Password ในหน้าจอ, Streamlit secrets หรือ ENV")],
                {},
            )
            continue
        with st.spinner(f"กำลังทดสอบ {controller['name']} ({controller['ip']})..."):
            st.session_state.rest_test_results[index] = run_read_only_test(controller, password)

    for index in range(2):
        if index in st.session_state.rest_test_results:
            results, debug = st.session_state.rest_test_results[index]
            render_results(
                f"{config['controllers'][index]['name']} — {config['controllers'][index]['ip']}",
                results,
                debug,
            )

with settings_tab:
    manage_page.render_settings(config)
    st.divider()
    if TLS_VERIFY is False:
        st.warning(
            "TLS Certificate Verification ยังปิดอยู่ ตั้ง WLC_CA_BUNDLE=/path/to/ca.pem "
            "หรือ WLC_VERIFY_TLS=true เมื่อ Controller ใช้ Certificate ที่เชื่อถือได้"
        )
    else:
        st.success("TLS Certificate Verification เปิดใช้งานอยู่")

    st.markdown("### ตำแหน่งไฟล์ Password")
    st.code(".streamlit/secrets.toml", language="text")
    st.code(
        """[wlc_passwords]
controller_1 = "PASSWORD_CONTROLLER_1"
controller_2 = "PASSWORD_CONTROLLER_2""",
        language="toml",
    )

with mac_tab:
    st.subheader("Add/Del MAC")
    st.caption("คำสั่งจะถูกส่งไปยัง Controller ทั้งสองตัวผ่าน REST API เท่านั้น")

    role_options = [item["name"] for item in config.get("roles", [])]
    role_map = {item["name"]: item.get("separator", ":") for item in config.get("roles", [])}

    single_tab, bulk_tab = st.tabs(["Add/Del รายเครื่อง", "Bulk Add จาก CSV/XLSX"])

    with single_tab:
        if not role_options:
            st.error("ยังไม่มี Role กรุณาเพิ่ม Role ที่หน้า Controller Settings")
        else:
            ensure_manual_entries(role_options[0])
            st.markdown("### รายการ MAC ที่ต้องการดำเนินการ")
            st.caption(
                "แต่ละรายการเลือก Role ได้เอง และระบบจะแสดงตัวคั่นตาม Role โดยอัตโนมัติ"
            )

            manual_tasks: list[dict[str, Any]] = []
            all_rows_valid = True
            for row_index, entry in enumerate(list(st.session_state.manual_mac_entries), start=1):
                task, is_valid = render_manual_mac_entry(
                    entry,
                    row_index,
                    role_options,
                    role_map,
                )
                if task is not None:
                    manual_tasks.append(task)
                if not is_valid:
                    all_rows_valid = False

            cleaned_values = [task["cleaned_mac"] for task in manual_tasks]
            duplicate_cleaned = sorted(
                {value for value in cleaned_values if cleaned_values.count(value) > 1}
            )
            if duplicate_cleaned:
                all_rows_valid = False
                st.error(
                    "พบ MAC ซ้ำในรายการ: "
                    + ", ".join(duplicate_cleaned)
                    + " กรุณาลบรายการซ้ำก่อนส่ง"
                )

            add_row_col, clear_col = st.columns([2, 1])
            with add_row_col:
                if st.button(
                    "➕ เพิ่ม MAC อีก 1 รายการ",
                    use_container_width=True,
                    disabled=len(st.session_state.manual_mac_entries) >= 20,
                    key="add_manual_mac_row",
                ):
                    st.session_state.manual_mac_entries.append(new_manual_entry(role_options[0]))
                    st.rerun()
            with clear_col:
                if st.button("ล้างทุกรายการ", use_container_width=True, key="clear_manual_rows"):
                    reset_manual_entries(role_options[0])
                    st.rerun()

            if manual_tasks:
                st.markdown("#### Preview ก่อนส่ง")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "รายการ": task["line"],
                                "MAC ที่จะส่ง": task["mac"],
                                "Role": task["role"],
                            }
                            for task in manual_tasks
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                )

            ready_to_submit = bool(manual_tasks) and all_rows_valid and (
                len(manual_tasks) == len(st.session_state.manual_mac_entries)
            )
            add_col, del_col = st.columns(2)
            with add_col:
                add_pressed = st.button(
                    f"ADD MAC {len(manual_tasks)} รายการ เข้า Controller ทั้งสองตัว",
                    type="primary",
                    use_container_width=True,
                    disabled=not ready_to_submit,
                    key="manual_add_button",
                )
            with del_col:
                delete_pressed = st.button(
                    f"DEL MAC {len(manual_tasks)} รายการ ออกจาก Controller ทั้งสองตัว",
                    use_container_width=True,
                    disabled=not ready_to_submit,
                    key="manual_delete_button",
                )

            if add_pressed or delete_pressed:
                selected_action = "Add" if add_pressed else "Delete"
                operation_id = uuid.uuid4().hex
                render_mac_operation_dialog(
                    config,
                    selected_action,
                    manual_tasks,
                    operation_id,
                )

    with bulk_tab:
        st.markdown("### รูปแบบไฟล์ Bulk Import")
        st.info(
            "ไฟล์ต้องมีหัวคอลัมน์เพียง 2 คอลัมน์ คือ `MAC` และ `Role` "
            "โดยชื่อ Role ต้องตรงกับ Role ที่เพิ่มไว้ในระบบ"
        )

        sample_dataframe = bulk_template_dataframe(role_options)
        st.dataframe(sample_dataframe, hide_index=True, use_container_width=True)
        st.caption(
            "ตัวอย่าง: Role แรกจะถูกจัดรูปแบบ MAC ด้วยตัวคั่นที่ตั้งไว้ และ Role ที่สองจะใช้ตัวคั่นของตัวเอง "
            "ตามค่าที่ตั้งไว้ใน Controller Settings"
        )

        download_csv, download_xlsx = st.columns(2)
        with download_csv:
            st.download_button(
                "ดาวน์โหลด Template CSV",
                data=sample_dataframe.to_csv(index=False).encode("utf-8-sig"),
                file_name="mac_bulk_template.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with download_xlsx:
            st.download_button(
                "ดาวน์โหลด Template XLSX",
                data=build_xlsx_bytes(sample_dataframe),
                file_name="mac_bulk_template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        st.markdown("#### อัปโหลดไฟล์")
        uploaded_file = st.file_uploader(
            "เลือกไฟล์ CSV หรือ XLSX",
            type=["csv", "xlsx"],
            accept_multiple_files=False,
            key="bulk_upload",
        )

        bulk_tasks: list[dict[str, Any]] = []
        bulk_errors: list[dict[str, Any]] = []
        if uploaded_file is not None:
            try:
                bulk_dataframe = read_bulk_file(uploaded_file)
                bulk_tasks, bulk_errors = validate_bulk_dataframe(bulk_dataframe, role_map)

                if bulk_errors:
                    st.error("Error: รูปแบบไฟล์หรือข้อมูลภายในไฟล์ไม่ถูกต้อง ระบบยังไม่ส่งข้อมูลเข้า Controller")
                    st.dataframe(pd.DataFrame(bulk_errors), hide_index=True, use_container_width=True)
                elif not bulk_tasks:
                    st.warning("ไม่พบ MAC ที่พร้อม Add")
                else:
                    preview_dataframe = pd.DataFrame(
                        [
                            {
                                "บรรทัด": task["line"],
                                "MAC ต้นฉบับ": task["raw_mac"],
                                "MAC ที่จะส่ง": task["mac"],
                                "Role": task["role"],
                            }
                            for task in bulk_tasks
                        ]
                    )
                    st.success(f"ตรวจ Format ผ่าน พบข้อมูลพร้อม Add {len(bulk_tasks)} รายการ")
                    st.dataframe(preview_dataframe, hide_index=True, use_container_width=True)
            except (ValueError, OSError, ImportError) as exc:
                bulk_errors = [
                    {
                        "บรรทัด": "-",
                        "คอลัมน์": "-",
                        "ค่าที่พบ": uploaded_file.name,
                        "สาเหตุ": str(exc),
                    }
                ]
                st.error(f"Error: ไม่สามารถอ่านไฟล์ได้ — {exc}")

        bulk_add_pressed = st.button(
            "ยืนยัน BULK ADD เข้า Controller ทั้งสองตัว",
            type="primary",
            use_container_width=True,
            disabled=not bool(bulk_tasks) or bool(bulk_errors),
            key="bulk_add_button",
        )

        if bulk_add_pressed:
            st.session_state.bulk_operation_results = {}
            progress = st.progress(0, text="กำลังเริ่ม Bulk Add")
            for index, controller in enumerate(config["controllers"]):
                password = password_for_controller(controller, index)
                if not password:
                    missing_rows = [
                        {
                            "บรรทัด": task["line"],
                            "MAC": task["mac"],
                            "Role": task["role"],
                            "ADD": "FAIL",
                            "Verify": "SKIPPED",
                            "HTTP": "-",
                            "รายละเอียด": "ไม่พบ Password ใน secrets/ENV หรือหน้า REST API TEST",
                        }
                        for task in bulk_tasks
                    ]
                    st.session_state.bulk_operation_results[index] = (missing_rows, {})
                    progress.progress((index + 1) / 2, text=f"ข้าม {controller['name']} เพราะไม่พบ Password")
                    continue

                progress.progress(index / 2, text=f"กำลัง Add ที่ {controller['name']}")
                st.session_state.bulk_operation_results[index] = run_bulk_add(
                    controller,
                    password,
                    bulk_tasks,
                )
                progress.progress((index + 1) / 2, text=f"เสร็จสิ้น {controller['name']}")
            progress.empty()

        for index in range(2):
            if index in st.session_state.bulk_operation_results:
                rows, debug = st.session_state.bulk_operation_results[index]
                render_bulk_results(config["controllers"][index], rows, debug)
