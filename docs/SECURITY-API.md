# AquaControl Security / User-Management API

Reverse-engineered reference for the Ashly AquaControl REST endpoints under
`/v1.0-beta/security/users/...`, plus the recommended flow for the
Home Assistant integration to provision a dedicated service-account user
instead of running on factory-default `admin/secret` credentials.

Captured against an Ashly AQM1208 (firmware 1.1.8).

---

## 1. Security Model

### 1.1 Role types

`GET /security/users/roleTypes`:

| Role ID | Capabilities |
|---|---|
| `View Only` | Read-only. 20 permissions, all `locked` (cannot be edited). |
| `Operator` | DSP edit + DCA/Mixer Remote + Preset Recall. 27 permissions. |
| `Guest Admin` | Almost everything except firmware update. 40 permissions. |
| `Admin` | Everything including firmware. 41 permissions, all `locked`. |

### 1.2 Permission types

Each role type has its own permission set. `GET /security/users/permissionTypes`
returns 128 entries (20 + 27 + 40 + 41) with the shape:

```json
{
  "id": "<RoleType>.<PermissionName>",
  "name": "<PermissionName>",
  "enabledByDefault": true|false,
  "editable": true|false,
  "roleTypeId": "<RoleType>"
}
```

#### Important fields

- **`editable: false` (locked)** — the permission is fixed for the role.
  - Locked + `enabledByDefault: true` → always on.
  - Locked + `enabledByDefault: false` → always off.
- **`editable: true` + `enabledByDefault: true`** — on by default but can be turned off.
- **`editable: true` + `enabledByDefault: false`** — off by default but can be turned on.

#### Guest Admin permissions

(40 total; 25 locked, 15 editable)

**Locked-on** (always granted when role assigned): `View Accounts`,
`View Device Misc.`, `View All DSP settings`, `Event Log View`,
`Event Scheduler View`, `Front Panels Control View`, `Rear Panel Controls View`,
`Network Settings View`, `Paging Ducking View`, `Preset View`, `DCA Remote`,
`Mixer 1..8 Remote`, `System Time View`, `Trigger Settings View`,
`Change Password`, `View Remotes`, `Edit Remotes`, `Add Remotes`.

**Editable** (caller controls):
- `Edit Accounts` (default ON)
- `Remote Logout` (default ON)
- `Edit Device Misc.` (default ON)
- `Event Log Clear` (default ON)
- `Event Scheduler Edit` (default ON)
- `Front Panels Control Edit` (default ON)
- `Rear Panel Controls Edit` (default ON)
- `Network Settings Edit` (default ON)
- `Paging Ducking Edit` (default ON)
- `Preset Edit` (default ON)
- `Preset Recall` (default ON)
- `System Time Edit` (default ON)
- `Trigger Settings Edit` (default ON)
- `Edit Signal Chain` (default ON)
- `Import/Export Settings` (default ON)

For the other role types, full lists are obtained the same way; the
Operator role has 27 permissions (most locked-on view perms + edit-signal-chain
+ Preset Recall + DCA/Mixer Remotes + Change Password), and View Only has 20
all-locked read-only permissions.

### 1.3 Remote permissions

`GET /security/users/remotePermissions/permissionTypes` returns empty `[]`
on this device. The `remotePermissions` array in user create/update payloads
must still be present (even as `[]`); it's a required field for hardware-remote
permission grants on devices that support it.

### 1.4 User shape

```json
{
  "id": "<username>",
  "username": "<username>",
  "active": true|false,
  "lastLogin": "Thu May 14 2026, 09:21:40 GMT-0400 (Eastern Daylight Time)" | null,
  "system": false,                  // true only for built-in admin
  "keepLoggedIn": false,
  "role": {
    "id": "<username>.<RoleType>",
    "userId": "<username>",
    "roleTypeId": "<RoleType>",
    "roleType": {"id": "<RoleType>", "name": "<RoleType>"},
    "permissions": [
      {
        "id": "<username>.<RoleType>.<PermissionName>",
        "enabled": true|false,
        "roleId": "<username>.<RoleType>",
        "permissionTypeId": "<RoleType>.<PermissionName>",
        "permissionType": { /* full permissionType record */ }
      },
      ...
    ]
  }
}
```

The built-in `admin` user has `system: true` and cannot be deleted.

---

## 2. REST API

### 2.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET    | `/security/users` | List all users |
| POST   | `/security/users` | Create new user |
| POST   | `/security/users/{id}` | Update user (rename, role change, password reset) |
| DELETE | `/security/users/{id}` | Delete user |
| GET    | `/security/users/permission` | All permission rows across all users |
| GET    | `/security/users/permission/byUser/{userId}` | Permissions for one user |
| POST   | `/security/users/permission/{userId}` | Change permissions on existing user |
| GET    | `/security/users/permissionTypes` | All permission types per role |
| GET    | `/security/users/roleTypes` | List role types |
| GET    | `/security/users/roles` | Lists role assignments (returns `{role: {roleType, permissions}}` per assignment) |
| POST   | `/security/users/password/{username}` | Change user's own password (requires oldPassword) |
| GET    | `/security/users/remotePermissions/permission` | Remote permission rows |
| GET    | `/security/users/remotePermissions/permissionTypes` | Remote permission types |
| GET    | `/security/users/remotePermissions/byUser/{userId}` | Remote permissions for a user |
| GET    | `/session/checkCurrentLogin` | Validate current session |
| GET    | `/session/logout` | Clear current session |
| POST   | `/session/authorizedlogout` | Kick another logged-in user (requires `Remote Logout` permission) |
| POST   | `/session/login` | Establish session (returns `ashly-sid` cookie) |

### 2.2 Request bodies

#### `POST /security/users` (create)

```json
{
  "username": "<alphanumeric, 1..20 chars>",
  "roleTypeId": "View Only" | "Operator" | "Guest Admin" | "Admin",
  "password": "<alphanumeric, 4..20 chars>",
  "permissions": ["<bare permission name>", ...],
  "remotePermissions": []
}
```

**Critical format quirk**: the `permissions` array entries are **bare permission names** without the role prefix.

- ✅ `"Edit Signal Chain"` — works
- ❌ `"Guest Admin.Edit Signal Chain"` — returns 422 "not available for specified role"

This contradicts the format used in `permissionTypes` GET responses (`<RoleType>.<PermissionName>`) and in user-by-id permission lists. Devops oddity to remember.

#### `POST /security/users/{id}` (update)

```json
{
  "username": "<new username>",       // optional
  "roleTypeId": "<new role>",         // optional
  "password": "<new password>"        // optional — note: bypasses oldPassword check
}
```

#### `POST /security/users/permission/{userId}` (change permissions)

```json
{
  "permissions": ["<bare permission name>", ...],
  "remotePermissions": []
}
```

Same bare-name format as create. Existing permissions not in the array are turned OFF; named permissions are turned ON.

#### `POST /security/users/password/{username}` (change own password)

```json
{"oldPassword": "...", "newPassword": "..."}
```

Both alphanumeric, 4..20 chars.

### 2.3 Constraints

- `username`: 1–20 chars, alphanumeric only (`[a-zA-Z0-9]+`). No underscores, dots, dashes.
- `password`: 4–20 chars, alphanumeric only.
- `username` is also the `id` — they're identical strings.
- `roleTypeId` must be one of the four exact role names (with the space in `"Guest Admin"`).
- `permissions` and `remotePermissions` arrays must be present in create/permission-change bodies, even as `[]`.

### 2.4 Push events

User CRUD operations emit events on the `Security` topic via socket.io. See
`docs/WEBSOCKET-API.md` §3.9 for the event names and payload shapes. Note that
`api` field on Security events uses `"security/..."` (no leading slash),
unlike every other topic.

---

## 3. Recommended HA Integration Flow

### 3.1 Goals

1. Stop using `admin/secret` for HA's REST/socket.io traffic.
2. Provision a dedicated service-account user with the minimum permission set HA needs.
3. Let the user keep their admin credentials private (HA never re-uses or re-stores them after provisioning).
4. Make the dedicated user discoverable as such — easy to delete when the integration is removed.

### 3.2 Minimum useful permission set for HA

Role: **Guest Admin** (gives `View All DSP settings` + most edit perms; falls short of firmware/account/network admin).

Editable permissions to **enable** (everything HA actually uses today):

```
Edit Signal Chain              # mute, channel name, mixer routing
Preset Recall                  # service: ashly.recall_preset
Front Panels Control Edit      # power state, identify, frontPanelLEDEnable
Rear Panel Controls Edit       # GPO toggles
Preset Edit                    # if the integration ever exposes preset save
```

Editable permissions to **leave disabled** (HA never needs them):

```
Edit Accounts                  # HA must not manage users
Remote Logout
Edit Device Misc.
Event Log Clear
Event Scheduler Edit
Network Settings Edit          # could lock HA out of the device
Paging Ducking Edit            # not currently exposed
System Time Edit
Trigger Settings Edit
Import/Export Settings
```

This gives the dedicated user enough to drive every entity in the integration plus `recall_preset`, but locks it out of anything that could brick the connection or surprise the operator.

### 3.3 Provisioning flow

```python
DEDICATED_USERNAME = "haassistant"        # alphanumeric only
HA_PERMS = [
    "Edit Signal Chain",
    "Preset Recall",
    "Front Panels Control Edit",
    "Rear Panel Controls Edit",
    "Preset Edit",
]

async def provision_service_account(host, port, admin_user, admin_pw) -> tuple[str, str]:
    """Create the dedicated user. Returns (username, generated_password)."""
    password = secrets.token_hex(8)  # 16 alphanumeric chars, fits 4..20 constraint
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        # 1) admin login
        async with s.post(f"http://{host}:{port}/v1.0-beta/session/login",
                          json={"username": admin_user, "password": admin_pw, "keepLoggedIn": True}) as r:
            r.raise_for_status()

        # 2) cleanup any prior provisioning attempt (idempotent)
        await s.delete(f"http://{host}:{port}/v1.0-beta/security/users/{DEDICATED_USERNAME}")

        # 3) create new user
        async with s.post(
            f"http://{host}:{port}/v1.0-beta/security/users",
            json={
                "username": DEDICATED_USERNAME,
                "roleTypeId": "Guest Admin",
                "password": password,
                "permissions": HA_PERMS,
                "remotePermissions": [],
            },
        ) as r:
            r.raise_for_status()

        # 4) admin logout (we never use admin creds again)
        await s.get(f"http://{host}:{port}/v1.0-beta/session/logout")

    return DEDICATED_USERNAME, password
```

### 3.4 Config-flow integration

Suggested `config_flow.py` step ordering:

1. **User step** — collect `host`, `port`, `username`, `password`. Test login. If fails → re-prompt.
2. **Detect factory defaults**: if `username == "admin"` and `password == "secret"`, transition to a new "Service account" step.
3. **Service-account step** — show a checkbox: *"Create a dedicated AquaControl user for HA (recommended)"*. Default ON. Body text explains: the integration will create a `haassistant` user with the permissions it needs, store a randomly generated password, and never re-use the `admin` credentials.
4. On submit:
   - Call `provision_service_account(...)` with the admin credentials the user just typed.
   - Store `username = "haassistant"` and `password = <generated>` in the config entry's `data`.
   - **Do not** store the admin credentials anywhere.
   - Show a final confirmation step: "Service account created. Your `admin` password is unchanged. You can delete the `haassistant` user from the AquaControl Portal at any time to remove HA's access."
5. If the user declines the service account, store their typed credentials as before. Surface a `repair issue` warning that running as `admin` is discouraged — link to the docs.

### 3.5 Repair issue: `default_credentials`

Existing `repairs.py` already raises a `default_credentials` issue when the entry uses admin/secret. The fix flow should add a one-click option: *"Provision a dedicated service account now"* — which runs `provision_service_account` and rewrites the entry's `username`/`password`.

This makes the migration path for existing users a single click.

### 3.6 Cleanup on uninstall

When the user removes the integration (`async_remove_entry`), best-effort delete
the dedicated user:

```python
async def async_remove_entry(hass, entry):
    if entry.data.get(CONF_USERNAME) == DEDICATED_USERNAME:
        try:
            # We're about to delete our own account; this works because
            # delete requires Edit Accounts which Guest Admin doesn't have.
            # So we can't actually self-delete. The user must clean up manually,
            # OR we ask for admin creds one more time at remove-time.
            pass
        except Exception:
            pass
```

**Self-delete is not possible** because Guest Admin lacks `Edit Accounts`.
Two practical options:

1. **Leave the user behind, document it.** When the user removes the integration, show a final dialog: *"The `haassistant` user remains on your device. To remove it, log into AquaControl Portal → Settings → Security and delete it manually."*
2. **Prompt for admin creds at remove-time.** During the integration's removal flow, ask the user once more for admin credentials and use them to delete the service account. Skipping the prompt = silent failure (user is left behind).

Option 1 is simpler and safer. Option 2 is cleaner UX but requires an additional auth prompt at the worst possible moment (during an uninstall). Recommend option 1 with a clear post-removal repair issue.

### 3.7 Why Guest Admin and not Operator

The `Operator` role:
- Has `Edit Signal Chain` ✓
- Has `Preset Recall` ✓
- Has `DCA Remote` + `Mixer N Remote` ✓
- **Missing `Front Panels Control Edit`** — cannot toggle power state or front-panel LEDs. Would break the integration's power switch.
- **Missing `Rear Panel Controls Edit`** — cannot toggle GPOs.

So Operator is too restrictive for the current integration. If you ever drop the power/LED switch and GPO entities, Operator becomes viable and would be the better choice (smaller blast radius if compromised). For now, Guest Admin with editable permissions trimmed is the right balance.

### 3.8 Permission verification at runtime

Before assuming a dedicated user can do something, the integration should call
`GET /security/users/permission/byUser/{username}` once at setup and cache
the granted permissions. If a critical permission is missing, surface a
repair issue with a one-click flow to add it (via
`POST /security/users/permission/{username}` with admin creds).

This handles the edge case where a user manually edits the dedicated account's
permissions in the Portal and accidentally revokes something.

---

## 4. Test Verification (one-shot)

The following E2E sequence was run against the AQM1208 and all assertions
passed:

1. Create `haashlyprobe` (Guest Admin, alphanumeric password, 5 enabled perms).
2. Verify the user appears in `GET /security/users`.
3. Log in as `haashlyprobe` via `POST /session/login` in a separate session.
4. Confirm allowed operations all return 2xx:
   - `GET /workingsettings/dsp/chain`, `/system/frontPanel/info`, `/preset`, `/micPreamp`, `/workingsettings/virtualDVCA/parameters`, `/workingsettings/dsp/mixer/config/parameter`
   - `POST /workingsettings/dsp/chain/mute/InputChannel.1`
   - `POST /system/frontPanel/info` with `{frontPanelLEDEnable}`
   - `POST /micPreamp/1`
   - `POST /preset/recall/Baseline`
5. Confirm denied operations all return 403:
   - `POST /security/users` (Edit Accounts)
   - `DELETE /security/users/admin` (Edit Accounts)
   - `POST /system/log/clear` (Event Log Clear)
   - `POST /network` (Network Settings Edit)
   - `POST /system/time` (System Time Edit)
6. `DELETE /security/users/haashlyprobe` cleans up; admin remains.

---

## 5. Gotchas

1. **Bare permission names** — `permissions` array contains `"Edit Signal Chain"`, NOT `"Guest Admin.Edit Signal Chain"`. The full ID format is used in `GET` responses but rejected in `POST` payloads.

2. **Username and password are alphanumeric only.** No underscores, dots, dashes, spaces, or special chars. Min 4 (password) / 1 (username), max 20 each.

3. **Empty `permissions`/`remotePermissions` arrays must be present.** Omitting them returns 400. Use `[]`.

4. **`permissions` is the OPT-IN list.** Editable permissions not listed are turned OFF (even if `enabledByDefault: true` in the type catalog). Locked permissions are always on regardless.

5. **`POST /security/users/{id}` (update) does NOT accept `permissions`.** Use `POST /security/users/permission/{userId}` for permission changes.

6. **`POST /security/users/{id}` with a new `password` bypasses `oldPassword`.** This is the admin-reset path (any user with `Edit Accounts`). Distinguish from `POST /security/users/password/{username}` which is the self-service path requiring `oldPassword`.

7. **Self-delete is not possible** for Guest Admin or below — `DELETE /security/users/{id}` requires `Edit Accounts`. Only Admin can delete users.

8. **System log audit trail.** Every user CRUD action also fires `System Log` and `System Log Entry` push events with `eventType` like `userAccountCreated`, `userAccountDeleted`. Useful for security telemetry.

9. **Push event `api` paths for Security are unusual.** They use `"security/users"`, `"security/users/roles"`, `"security/users/permission"` — without the leading slash, unlike every other topic where `api` starts with `/`.

10. **Event names have literal trailing commas.** `New User,`, `Update password,`, `Delete User,` are firmware quirks — match exactly. Only `Permissions Updated` lacks the trailing comma.

11. **`/session/checkCurrentLogin` returns 404 on this firmware** despite being in the swagger. Use `GET /preset` or another known endpoint as a session-validity probe.

12. **Cannot rename `admin`.** The built-in admin user has `system: true` and most operations on it return errors. Don't attempt to remove or rename it.

13. **Session cookie expires; re-auth is silent.** The integration's existing 401-retry path handles this. The socket.io connection must be reconnected with the new cookie after a re-auth.
