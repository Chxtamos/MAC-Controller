from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import streamlit as st

# Real deployment values belong in local conf.json / Streamlit Secrets.
# Defaults below are placeholders only and are safe to keep in Git.
CONFIG_FILE = Path(os.getenv("WLC_CONFIG_FILE", "conf.json"))
DEFAULT_CONFIG: dict[str, Any] = {
    "controllers": [
        {
            "name": "Controller 1",
            "ip": "xxx.xxx.xxx.xxx",
            "username": "xxx",
            "config_path": "/md",
        },
        {
            "name": "Controller 2",
            "ip": "xxx.xxx.xxx.xxx",
            "username": "xxx",
            "config_path": "/md",
        },
    ],
    "roles": [
        {"name": "example-role-1", "separator": "-"},
        {"name": "example-role-2", "separator": ":"},
    ],
}


def _clone_default() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def validate_private_ip(value: str) -> str:
    # Only private network addresses are accepted for Controller connections.
    value = str(value or "").strip()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("IP Controller ไม่ถูกต้อง") from exc
    if not ip.is_private:
        raise ValueError("Controller ต้องใช้ Private IP ภายในองค์กร")
    return value


def validate_username(value: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", value):
        raise ValueError("Username ใช้ได้เฉพาะตัวอักษร ตัวเลข และ _ . @ -")
    return value


def validate_config_path(value: str) -> str:
    value = str(value or "").strip()
    if not value.startswith("/"):
        raise ValueError("Config Path ต้องขึ้นต้นด้วย /")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,255}", value):
        raise ValueError("Config Path มีรูปแบบไม่ถูกต้อง")
    return value.rstrip("/") or "/"


def validate_role(value: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,63}", value):
        raise ValueError("Role ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษ ตัวเลข และ _ . : -")
    return value


def validate_separator(value: str) -> str:
    """อนุญาตไม่มีตัวคั่น หรืออักขระพิเศษที่พิมพ์ได้ 1 ตัว."""
    value = str(value if value is not None else "")
    if value == "":
        return ""
    if len(value) != 1:
        raise ValueError("ตัวคั่น MAC ต้องมีเพียง 1 ตัวอักษร หรือเว้นว่างเพื่อไม่ใช้ตัวคั่น")
    if not value.isprintable() or value.isspace():
        raise ValueError("ตัวคั่น MAC ต้องเป็นอักขระที่พิมพ์ได้และห้ามเป็นช่องว่าง")
    if value.isalnum():
        raise ValueError("ตัวคั่น MAC ต้องเป็นอักขระพิเศษ ไม่ใช่ตัวอักษรหรือตัวเลข")
    return value


def separator_label(value: str) -> str:
    return "ไม่มีตัวคั่น" if value == "" else value


def normalize_config(raw: Any) -> dict[str, Any]:
    # Keep the config shape predictable even when fields are missing or an
    # older conf.json is loaded.
    config = raw if isinstance(raw, dict) else {}
    source_controllers = config.get("controllers") if isinstance(config.get("controllers"), list) else []
    default_controllers = _clone_default()["controllers"]

    controllers: list[dict[str, str]] = []
    for index in range(2):
        source = (
            source_controllers[index]
            if index < len(source_controllers) and isinstance(source_controllers[index], dict)
            else {}
        )
        default = default_controllers[index]
        controllers.append(
            {
                "name": str(source.get("name") or default["name"]),
                "ip": str(source.get("ip") or default["ip"]),
                "username": str(source.get("username") or default["username"]),
                "config_path": str(source.get("config_path") or default["config_path"]),
            }
        )

    roles: list[dict[str, str]] = []
    for item in config.get("roles", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        separator = str(item.get("separator") if item.get("separator") is not None else ":")
        try:
            safe_separator = validate_separator(separator)
        except ValueError:
            continue
        if name:
            roles.append({"name": name, "separator": safe_separator})
    if not roles:
        roles = _clone_default()["roles"]

    return {"controllers": controllers, "roles": roles}


def load_config() -> dict[str, Any]:
    # conf.json is a local runtime file and must not be committed to Git.
    if not CONFIG_FILE.exists() or CONFIG_FILE.stat().st_size == 0:
        config = _clone_default()
        save_config(config)
        return config

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            return normalize_config(json.load(file))
    except (OSError, json.JSONDecodeError):
        config = _clone_default()
        save_config(config)
        return config


def save_config(config: dict[str, Any]) -> None:
    # Write via a temporary file and os.replace() to reduce the chance of
    # leaving a partially written configuration file.
    normalized = normalize_config(config)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=CONFIG_FILE.parent,
        prefix=f".{CONFIG_FILE.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        json.dump(normalized, temp_file, indent=4, ensure_ascii=False)
        temp_name = temp_file.name
    os.replace(temp_name, CONFIG_FILE)


def password_secret_hints(controller: dict[str, str], index: int) -> list[str]:
    ip_key = controller["ip"].replace(".", "_")
    return [
        f"controller_{index + 1}",
        ip_key,
        f"WLC_CONTROLLER_{index + 1}_PASS",
        f"WLC_{ip_key}_PASS",
    ]


def get_saved_password(controller: dict[str, str], index: int) -> str:
    """อ่าน Password จาก Streamlit secrets หรือ ENV โดยไม่เก็บลง conf.json"""
    # Passwords are intentionally excluded from conf.json and source code.
    ip_key = controller["ip"].replace(".", "_")
    try:
        secrets = st.secrets.get("wlc_passwords", {})
        for key in (f"controller_{index + 1}", ip_key):
            if key in secrets and str(secrets[key]).strip():
                return str(secrets[key])
    except Exception:
        pass

    for env_name in (f"WLC_CONTROLLER_{index + 1}_PASS", f"WLC_{ip_key}_PASS"):
        value = os.getenv(env_name)
        if value:
            return value
    return ""


def _render_controller_settings(config: dict[str, Any]) -> None:
    # UI for the two Aruba Controllers. Values saved here go to local conf.json.
    st.subheader("ตั้งค่า REST API Controller")
    st.caption("ระบบกำหนด Controller จำนวน 2 ตัวตายตัว และไม่บันทึกรหัสผ่านลง conf.json")

    edited_controllers: list[dict[str, str]] = []
    for index, controller in enumerate(config["controllers"]):
        with st.container(border=True):
            st.markdown(f"### {controller['name']}")
            col_ip, col_user = st.columns(2)
            with col_ip:
                ip_value = st.text_input(
                    "IP Address",
                    value=controller["ip"],
                    key=f"settings_ip_{index}",
                )
            with col_user:
                username_value = st.text_input(
                    "REST API Username",
                    value=controller["username"],
                    key=f"settings_username_{index}",
                )
            config_path_value = st.text_input(
                "Config Path",
                value=controller.get("config_path", "/md"),
                help="Standalone มักใช้ /md ส่วน Mobility Conductor อาจเป็น /md/<group> หรือ /mm/mynode",
                key=f"settings_config_path_{index}",
            )
            st.caption(
                "Password key ที่รองรับ: "
                + ", ".join(f"`{item}`" for item in password_secret_hints(controller, index))
            )
            edited_controllers.append(
                {
                    "name": f"Controller {index + 1}",
                    "ip": ip_value,
                    "username": username_value,
                    "config_path": config_path_value,
                }
            )

    if st.button("บันทึกการตั้งค่า Controller", type="primary", use_container_width=True):
        try:
            validated = []
            seen_ips: set[str] = set()
            for index, item in enumerate(edited_controllers):
                safe_ip = validate_private_ip(item["ip"])
                if safe_ip in seen_ips:
                    raise ValueError("Controller 1 และ Controller 2 ห้ามใช้ IP เดียวกัน")
                seen_ips.add(safe_ip)
                validated.append(
                    {
                        "name": f"Controller {index + 1}",
                        "ip": safe_ip,
                        "username": validate_username(item["username"]),
                        "config_path": validate_config_path(item["config_path"]),
                    }
                )
            config["controllers"] = validated
            save_config(config)
            st.success("บันทึก Controller ทั้งสองตัวแล้ว")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def _render_role_settings(config: dict[str, Any]) -> None:
    # Roles control both the Aruba role name and how MAC addresses are formatted.
    st.divider()
    st.subheader("จัดการ Role และตัวคั่น MAC")
    st.caption("Role ที่เพิ่มไว้จะนำไปใช้ได้ทั้งหน้า Add/Del MAC และ Bulk Add")

    roles = config.get("roles", [])
    if roles:
        for index, role in enumerate(roles):
            col_name, col_separator, col_delete = st.columns([3, 2, 1])
            with col_name:
                st.text_input(
                    "Role",
                    value=role["name"],
                    disabled=True,
                    key=f"role_display_name_{index}",
                )
            with col_separator:
                st.text_input(
                    "ตัวคั่น MAC",
                    value=separator_label(role.get("separator", ":")),
                    disabled=True,
                    key=f"role_display_separator_{index}",
                )
            with col_delete:
                st.markdown("<div style='height: 29px'></div>", unsafe_allow_html=True)
                delete_pressed = st.button(
                    "ลบ",
                    key=f"delete_role_{index}",
                    use_container_width=True,
                    disabled=len(roles) <= 1,
                )
                if delete_pressed:
                    config["roles"].pop(index)
                    save_config(config)
                    st.success(f"ลบ Role `{role['name']}` แล้ว")
                    st.rerun()

    st.markdown("#### เพิ่ม Role ใหม่")
    col_role, col_separator = st.columns(2)
    with col_role:
        new_role = st.text_input(
            "ชื่อ Role",
            placeholder="เช่น example-role",
            key="new_role_name",
        )
    with col_separator:
        new_separator = st.text_input(
            "ตัวคั่น MAC (อักขระพิเศษ 1 ตัว)",
            max_chars=1,
            placeholder="เช่น -  :  *  /  #  หรือเว้นว่าง",
            key="new_role_separator",
            help="ใส่อักขระพิเศษได้ 1 ตัว เช่น -, :, *, /, # หรือเว้นว่างเพื่อไม่ใช้ตัวคั่น",
        )

    if st.button("เพิ่ม Role", use_container_width=True, key="add_role_button"):
        try:
            safe_role = validate_role(new_role)
            safe_separator = validate_separator(new_separator)
            if any(item["name"].lower() == safe_role.lower() for item in roles):
                raise ValueError("Role นี้มีอยู่แล้ว")
            config.setdefault("roles", []).append(
                {"name": safe_role, "separator": safe_separator}
            )
            save_config(config)
            st.success(f"เพิ่ม Role `{safe_role}` แล้ว")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def render_settings(config: dict[str, Any]) -> dict[str, Any]:
    _render_controller_settings(config)
    _render_role_settings(config)
    return config
