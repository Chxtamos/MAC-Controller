# MAC Controller

Streamlit-based tool for managing MAC addresses in the Local User Database of Aruba Controllers through the ArubaOS REST API.

This project is designed primarily for Aruba wireless/controller environments. It provides a simple web interface for testing REST connectivity, adding or deleting MAC entries, managing controller settings, and performing bulk imports from CSV/XLSX files.

## Main Features

- Aruba REST API connectivity test
- TCP port check before API login
- REST login / logout handling
- Read-only controller test using show commands
- Add MAC address to Aruba Local User DB
- Delete MAC address from Aruba Local User DB
- Verify MAC presence after add/delete
- Support multiple MAC address formats
- Role-based MAC separator formatting
- Bulk import from CSV/XLSX
- Controller settings page
- Password loading from Streamlit Secrets or environment variables
- Local config separated from source code

## Target Platform

The project is intended mainly for Aruba Controller / ArubaOS environments that expose the Aruba REST API.

The REST client communicates with the controller using HTTPS and the ArubaOS REST endpoints on the configured REST port.

Default REST port used by the project:

```text
4343
```

The current implementation expects two Controller entries.

## Project Structure

```text
MAC-Controller/
├── app.py
├── manage_page.py
├── requirements.txt
├── conf.example.json
├── .gitignore
├── .streamlit/
│   └── secrets.example.toml
├── scripts/
│   └── scan_public_release.py
├── publish_to_github.ps1
├── SECURITY.md
├── README.md
└── PUBLIC_RELEASE_REVIEW.md
```

### File Description

#### `app.py`

Main Streamlit application.

Contains the Aruba REST API client and the main application workflow, including:

- Controller connectivity checks
- Aruba REST login/logout
- Show command requests
- MAC validation and formatting
- Add/Delete MAC operations
- Post-operation verification
- CSV/XLSX bulk processing
- Main Streamlit UI

#### `manage_page.py`

Handles configuration and settings.

Main responsibilities:

- Load and save `conf.json`
- Validate private Controller IP addresses
- Validate usernames and Aruba config paths
- Manage role names and MAC separators
- Load Controller passwords from Streamlit Secrets or environment variables
- Render Controller Settings UI

#### `conf.example.json`

Example configuration file.

Copy it to:

```text
conf.json
```

Then replace the placeholder values with the real Controller configuration.

Example:

```json
{
  "controllers": [
    {
      "name": "Controller 1",
      "ip": "xxx.xxx.xxx.xxx",
      "username": "admin",
      "config_path": "/md"
    },
    {
      "name": "Controller 2",
      "ip": "xxx.xxx.xxx.xxx",
      "username": "admin",
      "config_path": "/md"
    }
  ],
  "roles": [
    {
      "name": "example-role-1",
      "separator": "-"
    },
    {
      "name": "example-role-2",
      "separator": ":"
    }
  ]
}
```

Do not commit the real `conf.json` file.

#### `.streamlit/secrets.example.toml`

Example Streamlit Secrets file.

Copy it to:

```text
.streamlit/secrets.toml
```

Then insert the real Controller passwords:

```toml
[wlc_passwords]
controller_1 = "YOUR_CONTROLLER_1_PASSWORD"
controller_2 = "YOUR_CONTROLLER_2_PASSWORD"
```

The real `.streamlit/secrets.toml` file is ignored by Git.

#### `requirements.txt`

Python dependencies used by the project:

- Streamlit
- Requests
- Pandas
- OpenPyXL

#### `scripts/scan_public_release.py`

Reserved for a pre-push/public-release security scanner.

Its purpose is to detect sensitive values before publishing, such as:

- Real Controller IP addresses
- Passwords
- Tokens
- Private keys
- Local `conf.json`
- Real Streamlit secrets
- Organization-specific values that should not be public

#### `publish_to_github.ps1`

Publishing workflow helper for Windows/PowerShell.

It is intended to run security checks before Git commit/push.

#### `SECURITY.md`

Security notes and guidance for protecting Controller credentials and internal network information.

#### `PUBLIC_RELEASE_REVIEW.md`

Checklist to review before changing the repository from Private to Public.

## Requirements

- Python 3.10+ recommended
- Network access to the Aruba Controller
- Aruba REST API enabled and reachable
- Valid REST API username/password
- Access to the Aruba Local User Database

## Installation

Clone the repository:

```bash
git clone https://github.com/Chxtamos/MAC-Controller.git
cd MAC-Controller
```

Create a virtual environment:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

### 1. Create local Controller configuration

Copy:

```text
conf.example.json
```

to:

```text
conf.json
```

Then replace:

```text
xxx.xxx.xxx.xxx
```

with the real private IP addresses of the Aruba Controllers.

Update the username, config path, roles, and separators as required by the environment.

### 2. Configure Controller passwords

Copy:

```text
.streamlit/secrets.example.toml
```

to:

```text
.streamlit/secrets.toml
```

Then set the real passwords.

Passwords can also be supplied through environment variables:

```text
WLC_CONTROLLER_1_PASS
WLC_CONTROLLER_2_PASS
```

## Running the Application

Start Streamlit:

```bash
streamlit run app.py
```

Streamlit will display a local URL in the terminal, typically:

```text
http://localhost:8501
```

Open that address in a web browser.

## How to Use

### REST API TEST

Use this section before performing MAC operations.

The application will:

1. Test TCP connectivity to the configured Aruba REST port
2. Login to the Aruba REST API
3. Run a `show version` request
4. Read a small sample from the Local User DB
5. Logout from the Controller

If this test fails, verify:

- Controller IP address
- REST API username/password
- Network routing/firewall
- REST API service
- REST port
- Aruba permissions

### Controller Settings

Use this section to configure:

- Controller IP addresses
- REST API usernames
- Aruba config paths
- Roles
- MAC separators

Changes are stored in local `conf.json`.

Passwords are not stored in `conf.json`.

### Add MAC

Enter a MAC address and select the Aruba role.

The application:

1. Normalizes the MAC address
2. Applies the separator configured for the selected role
3. Logs in to the Controller
4. Calls the Aruba `userdb_add` REST object
5. Verifies that the MAC exists in the Local User DB
6. Logs out

### Delete MAC

The Delete operation first searches for the real stored username/MAC representation on the Controller.

This helps support MAC addresses stored with different separators such as:

```text
001122334455
00:11:22:33:44:55
00-11-22-33-44-55
```

After deletion, the application verifies that the entry is no longer present.

### Bulk Add

Bulk Add supports:

```text
CSV
XLSX
```

The expected columns are:

```text
MAC
Role
```

Example:

```csv
MAC,Role
02:00:00:00:00:01,example-role-1
02:00:00:00:00:02,example-role-2
```

The uploaded file is validated before any request is sent to the Controllers.

## Environment Variables

Optional environment variables supported by the application include:

```text
WLC_REST_PORT
WLC_CONNECT_TIMEOUT
WLC_REQUEST_TIMEOUT
WLC_VERIFY_TLS
WLC_CA_BUNDLE
WLC_CONFIG_FILE
WLC_CONTROLLER_1_PASS
WLC_CONTROLLER_2_PASS
MAX_BULK_ROWS
MAX_UPLOAD_BYTES
```

## TLS

For production use, certificate verification should be enabled whenever possible.

You can provide a CA bundle with:

```text
WLC_CA_BUNDLE
```

or enable TLS verification with:

```text
WLC_VERIFY_TLS=true
```

## Security Notes

Never commit:

```text
conf.json
.streamlit/secrets.toml
.env
passwords
API tokens
private keys
real internal credentials
```

The provided `.gitignore` excludes common local secrets and runtime configuration files.

Before making the repository public, review:

```text
PUBLIC_RELEASE_REVIEW.md
```

and run the public-release scanner when implemented.

## Aruba Notes

This project is built around Aruba Controller / ArubaOS REST behavior and Aruba Local User DB operations.

Depending on the Aruba deployment, the config path can differ.

Examples may include:

```text
/md
/md/<group>
/mm/mynode
```

Use the config path appropriate for your Aruba environment.

## Disclaimer

Test the application in a lab or non-production Aruba environment before using it against production Controllers.

Review Aruba permissions, REST API configuration, and Local User DB behavior for the specific ArubaOS version in use.
