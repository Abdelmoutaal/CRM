"""Earshot → Twenty CRM bridge + RLS Admin UI.

Configure via environment variables:
  TWENTY_API_URL    – e.g. http://twenty-server:3000
  TWENTY_API_KEY    – a Twenty API key (Settings → API keys)
  BRIDGE_SECRET     – shared secret for webhook auth + admin page
  PG_DATABASE_URL   – PostgreSQL URL (default: postgres://twenty:twenty@twenty-db:5432/twenty)
  PORT              – listen port (default 8100)
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
import psycopg2.pool
import redis as redis_lib
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TWENTY_API_URL = os.environ.get("TWENTY_API_URL", "http://twenty-server:3000")
TWENTY_API_KEY = os.environ.get("TWENTY_API_KEY", "")
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
PG_DATABASE_URL = os.environ.get("PG_DATABASE_URL", "postgres://twenty:twenty@twenty-db:5432/twenty")
TWENTY_SERVER_CONTAINER = os.environ.get("TWENTY_SERVER_CONTAINER", "odoo19-twenty-server-1")
REDIS_URL = os.environ.get("REDIS_URL", "redis://odoo19-twenty-redis-1:6379")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("earshot-bridge")

app = FastAPI(title="Earshot → Twenty Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Database pool
# ---------------------------------------------------------------------------
db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, PG_DATABASE_URL)

# ---------------------------------------------------------------------------
# Redis connection (for cache invalidation)
# ---------------------------------------------------------------------------
redis_client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)


def db_query(sql: str, params: tuple | None = None) -> list[dict]:
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        cur.close()
        return [dict(zip(cols, row)) for row in rows]
    finally:
        db_pool.putconn(conn)


def db_execute(sql: str, params: tuple | None = None) -> None:
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.close()
    finally:
        db_pool.putconn(conn)


def restart_twenty_server() -> None:
    """Restart the Twenty server container to rebuild the permissions cache."""
    import httpx
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds="/var/run/docker.sock")) as client:
            resp = client.post(
                f"http://localhost/containers/{TWENTY_SERVER_CONTAINER}/restart",
                params={"t": "5"},
                timeout=30.0,
            )
        log.info("Restarted Twenty server: %s", resp.status_code)
    except Exception as e:
        log.warning("Failed to restart Twenty server via Docker: %s", e)


# ---------------------------------------------------------------------------
# Cache invalidation — flush workspace cache from Redis
# ---------------------------------------------------------------------------

WORKSPACE_CACHE_KEYS_TO_FLUSH = [
    "rolesPermissions",
    "flatObjectPermissionMaps",
    "flatRolePermissionFlagMaps",
    "flatFieldPermissionMaps",
    "flatRowLevelPermissionPredicateMaps",
    "flatRowLevelPermissionPredicateGroupMaps",
    "userWorkspaceRoleMap",
    "flatRoleMaps",
    "flatRoleTargetMaps",
    "apiKeyRoleMap",
]


def flush_workspace_cache(workspace_id: str) -> None:
    """Delete workspace permission caches from Redis.

    After SQL writes, the in-memory NestJS cache is stale.
    Deleting the Redis keys forces Twenty to recompute from DB on the next
    permission check (latency: ~1s memoizer TTL, vs 30s full restart).
    """
    keys_to_delete = []
    for cache_key in WORKSPACE_CACHE_KEYS_TO_FLUSH:
        keys_to_delete.append(
            f"engine:workspace:{cache_key}:{workspace_id}:data"
        )
        keys_to_delete.append(
            f"engine:workspace:{cache_key}:{workspace_id}:hash"
        )

    deleted = redis_client.delete(*keys_to_delete)
    log.info(
        "Flushed %d Redis cache keys for workspace %s",
        deleted, workspace_id,
    )


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_auth(authorization: str | None = None, token: str | None = None) -> None:
    if not BRIDGE_SECRET:
        return
    if authorization:
        bearer = authorization.removeprefix("Bearer ").strip()
        if bearer == BRIDGE_SECRET:
            return
    if token and token == BRIDGE_SECRET:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing authentication")


# ---------------------------------------------------------------------------
# Models (Earshot webhook payload)
# ---------------------------------------------------------------------------

class ActionItem(BaseModel):
    task: str
    owner: Optional[str] = None
    done: bool = False
    due: Optional[str] = None

class Section(BaseModel):
    heading: str
    bullets: list[str] = []

class FolderInfo(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    color: Optional[str] = None

class EarshotMeeting(BaseModel):
    id: str
    title: str
    date: Optional[str] = None
    date_iso: Optional[str] = None
    attendees: list[str] = []
    agenda: Optional[str] = None
    template: Optional[str] = None
    folder: Optional[FolderInfo] = None
    duration_secs: Optional[int] = None
    status: Optional[str] = None
    transcript: Optional[str] = None
    notes: Optional[dict] = None
    bookmarks: Optional[list] = None


# ---------------------------------------------------------------------------
# Twenty CRM GraphQL helpers
# ---------------------------------------------------------------------------

def twenty_gql(query: str, variables: dict | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TWENTY_API_KEY}",
    }
    body: dict = {"query": query}
    if variables:
        body["variables"] = variables

    resp = httpx.post(
        f"{TWENTY_API_URL}/graphql",
        json=body,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        log.error("Twenty GraphQL errors: %s", data["errors"])
    return data


def find_person_by_email(email: str) -> Optional[str]:
    q = """
    query FindPerson($email: String!) {
      people(filter: { emails: { primaryEmail: { eq: $email } } }) {
        edges { node { id } }
      }
    }
    """
    data = twenty_gql(q, {"email": email})
    edges = data.get("data", {}).get("people", {}).get("edges", [])
    return edges[0]["node"]["id"] if edges else None


def find_person_by_name(name: str) -> Optional[str]:
    q = """
    query FindPersonByName($name: String!) {
      people(filter: { name: { firstName: { contains: $name } } }) {
        edges { node { id } }
      }
    }
    """
    data = twenty_gql(q, {"name": name})
    edges = data.get("data", {}).get("people", {}).get("edges", [])
    return edges[0]["node"]["id"] if edges else None


def create_note(title: str, body: str, person_ids: list[str]) -> Optional[str]:
    q = """
    mutation CreateNote($body: String!, $title: String!) {
      createNote(data: { body: $body, title: $title }) { note { id } }
    }
    """
    data = twenty_gql(q, {"body": body, "title": title})
    note_id = data.get("data", {}).get("createNote", {}).get("note", {}).get("id")
    if not note_id:
        return None

    for pid in person_ids:
        link_q = """
        mutation LinkNoteToPerson($noteId: ID!, $personId: ID!) {
          createNoteTarget(data: { note: { id: $noteId }, person: { id: $personId } }) {
            noteTarget { id }
          }
        }
        """
        twenty_gql(link_q, {"noteId": note_id, "personId": pid})

    return note_id


def create_call_recording(
    title: str, summary: str, transcript: Optional[str],
    duration_secs: Optional[int], start_date: Optional[str],
    person_ids: list[str],
) -> Optional[str]:
    q = """
    mutation CreateCall($input: CreateCallRecordingInput!) {
      createCallRecording(data: $input) { callRecording { id } }
    }
    """
    call_data: dict = {"title": title, "summary": summary or ""}
    if transcript:
        call_data["transcript"] = transcript
    if duration_secs:
        call_data["durationInSeconds"] = duration_secs
    if start_date:
        call_data["startDateTime"] = start_date

    data = twenty_gql(q, {"input": call_data})
    call_id = data.get("data", {}).get("createCallRecording", {}).get("callRecording", {}).get("id")
    if not call_id:
        return None

    for pid in person_ids:
        link_q = """
        mutation LinkCallToPerson($callId: ID!, $personId: ID!) {
          createCallParticipant(data: { callRecording: { id: $callId }, person: { id: $personId } }) {
            callParticipant { id }
          }
        }
        """
        twenty_gql(link_q, {"callId": call_id, "personId": pid})

    return call_id


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def receive_meeting(meeting: EarshotMeeting, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    log.info("Received meeting: %s (%s)", meeting.title, meeting.id)

    notes_data = meeting.notes or {}
    summary = notes_data.get("summary", "")
    note_sections = notes_data.get("sections", [])
    action_items = notes_data.get("action_items", [])
    note_attendees = notes_data.get("attendees", meeting.attendees)

    body_parts = []
    if summary:
        body_parts.append(f"## Summary\n\n{summary}")
    if note_sections:
        body_parts.append("## Discussion")
        for section in note_sections:
            heading = section.get("heading", "")
            bullets = section.get("bullets", [])
            if heading:
                body_parts.append(f"\n### {heading}")
            for b in bullets:
                body_parts.append(f"- {b}")
    if action_items:
        body_parts.append("\n## Action Items")
        for item in action_items:
            checkbox = "[x]" if item.get("done") else "[ ]"
            owner = f" ({item['owner']})" if item.get("owner") else ""
            due = f" — due {item['due']}" if item.get("due") else ""
            body_parts.append(f"- {checkbox} {item['task']}{owner}{due}")
    if meeting.agenda:
        body_parts.append(f"\n## Agenda\n\n{meeting.agenda}")

    body_parts.append(f"\n---\n*Earshot ID: {meeting.id}*")
    if meeting.folder:
        body_parts.append(f"*Folder: {meeting.folder.name or 'Uncategorized'}*")

    note_body = "\n".join(body_parts)
    note_title = f"Meeting: {meeting.title}"

    resolved_person_ids: list[str] = []
    for attendee in note_attendees:
        person_id = find_person_by_email(attendee) if "@" in attendee else None
        if not person_id:
            person_id = find_person_by_name(attendee)
        if person_id and person_id not in resolved_person_ids:
            resolved_person_ids.append(person_id)

    log.info("Resolved %d / %d attendees", len(resolved_person_ids), len(note_attendees))

    note_id = create_note(note_title, note_body, resolved_person_ids)
    log.info("Created Note: %s", note_id)

    call_id = None
    if meeting.transcript:
        call_id = create_call_recording(
            title=meeting.title, summary=summary, transcript=meeting.transcript,
            duration_secs=meeting.duration_secs, start_date=meeting.date_iso,
            person_ids=resolved_person_ids,
        )
        log.info("Created CallRecording: %s", call_id)

    return {"ok": True, "meeting_id": meeting.id, "twenty_note_id": note_id,
            "twenty_call_recording_id": call_id, "persons_linked": len(resolved_person_ids)}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "earshot-twenty-bridge"}


# ---------------------------------------------------------------------------
# RLS Admin — API
# ---------------------------------------------------------------------------

@app.get("/api/rls/config")
async def get_rls_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    _check_auth(authorization, token)

    roles = db_query(
        '''SELECT id, label, "canUpdateAllSettings" AS is_admin,
                  "canReadAllObjectRecords", "canUpdateAllObjectRecords",
                  "canSoftDeleteAllObjectRecords", "canDestroyAllObjectRecords"
           FROM core."role" ORDER BY "canUpdateAllSettings" DESC, label'''
    )
    objects = db_query(
        'SELECT id, "nameSingular", "labelSingular" FROM core."objectMetadata" WHERE "isSystem" = false ORDER BY "nameSingular"'
    )
    object_perms = db_query(
        '''SELECT "roleId", "objectMetadataId",
                  "canReadObjectRecords", "canUpdateObjectRecords",
                  "canSoftDeleteObjectRecords", "canDestroyObjectRecords"
           FROM core."objectPermission"'''
    )
    predicates = db_query(
        '''SELECT "roleId", "objectMetadataId", count(*) AS cnt
           FROM core."rowLevelPermissionPredicate"
           WHERE "deletedAt" IS NULL GROUP BY "roleId", "objectMetadataId"'''
    )

    perm_map = {}
    for r in object_perms:
        perm_map[(r["roleId"], r["objectMetadataId"])] = r
    pred_map = {(r["roleId"], r["objectMetadataId"]): r["cnt"] > 0 for r in predicates}

    config = []
    for role in roles:
        objects_config = []
        for obj in objects:
            rid, oid = role["id"], obj["id"]
            has_predicate = pred_map.get((rid, oid), False)
            perm = perm_map.get((rid, oid), {})

            can_read = perm.get("canReadObjectRecords", True)
            can_update = perm.get("canUpdateObjectRecords", True)
            can_soft_delete = perm.get("canSoftDeleteObjectRecords", True)
            can_destroy = perm.get("canDestroyObjectRecords", True)

            if can_read is False:
                mode = "none"
            elif has_predicate:
                mode = "own"
            else:
                mode = "all"

            objects_config.append({
                "objectMetadataId": oid,
                "nameSingular": obj["nameSingular"],
                "labelSingular": obj["labelSingular"] or obj["nameSingular"],
                "mode": mode,
                "canRead": bool(can_read) if can_read is not None else True,
                "canUpdate": bool(can_update) if can_update is not None else True,
                "canSoftDelete": bool(can_soft_delete) if can_soft_delete is not None else True,
                "canDestroy": bool(can_destroy) if can_destroy is not None else True,
            })

        config.append({
            "roleId": role["id"],
            "label": role["label"],
            "isAdmin": role["is_admin"],
            "canReadAll": bool(role.get("canReadAllObjectRecords", True)) if role.get("canReadAllObjectRecords") is not None else True,
            "canUpdateAll": bool(role.get("canUpdateAllObjectRecords", True)) if role.get("canUpdateAllObjectRecords") is not None else True,
            "canSoftDeleteAll": bool(role.get("canSoftDeleteAllObjectRecords", True)) if role.get("canSoftDeleteAllObjectRecords") is not None else True,
            "canDestroyAll": bool(role.get("canDestroyAllObjectRecords", True)) if role.get("canDestroyAllObjectRecords") is not None else True,
            "objects": objects_config,
        })

    return config


@app.post("/api/rls/config")
async def set_rls_config(
    request: Request,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    _check_auth(authorization, token)

    body = await request.json()
    role_id = body.get("roleId")
    object_metadata_id = body.get("objectMetadataId")
    mode = body.get("mode")

    if mode not in ("none", "own", "all"):
        raise HTTPException(400, "mode must be 'none', 'own', or 'all'")

    # 1. ApplicationId (for predicate insertion)
    app_row = db_query(
        'SELECT "applicationId" FROM core."objectMetadata" WHERE "nameSingular" = \'company\' LIMIT 1'
    )
    app_id = app_row[0]["applicationId"] if app_row else None

    # 2. WorkspaceId
    role_row = db_query(
        'SELECT "workspaceId" FROM core."role" WHERE id = %s', (role_id,)
    )
    workspace_id = role_row[0]["workspaceId"] if role_row else None

    if not app_id or not workspace_id:
        raise HTTPException(400, "Could not resolve applicationId or workspaceId")

    # 3. Clear existing predicates for this role + object
    db_execute(
        '''DELETE FROM core."rowLevelPermissionPredicate"
           WHERE "roleId" = %s AND "objectMetadataId" = %s''',
        (role_id, object_metadata_id),
    )

    # 4. Set object permission
    existing_perm = db_query(
        'SELECT id FROM core."objectPermission" WHERE "roleId" = %s AND "objectMetadataId" = %s',
        (role_id, object_metadata_id),
    )

    can_read = mode != "none"
    can_update = mode == "all"
    can_soft_delete = mode == "all"
    can_destroy = mode == "all"

    if existing_perm:
        db_execute(
            '''UPDATE core."objectPermission"
               SET "canReadObjectRecords" = %s,
                   "canUpdateObjectRecords" = %s,
                   "canSoftDeleteObjectRecords" = %s,
                   "canDestroyObjectRecords" = %s
               WHERE "roleId" = %s AND "objectMetadataId" = %s''',
            (can_read, can_update, can_soft_delete, can_destroy, role_id, object_metadata_id),
        )
    else:
        import uuid
        db_execute(
            '''INSERT INTO core."objectPermission"
               (id, "roleId", "objectMetadataId", "canReadObjectRecords",
                "canUpdateObjectRecords", "canSoftDeleteObjectRecords", "canDestroyObjectRecords",
                "workspaceId", "createdAt", "updatedAt", "universalIdentifier", "applicationId")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)''',
            (str(uuid.uuid4()), role_id, object_metadata_id,
             can_read, can_update, can_soft_delete, can_destroy,
             workspace_id, str(uuid.uuid4()), app_id),
        )

    # 5. Insert predicates for 'own' mode
    if mode == "own":
        created_by_field = db_query(
            'SELECT id FROM core."fieldMetadata" WHERE "objectMetadataId" = %s AND name = \'createdBy\'',
            (object_metadata_id,),
        )
        if created_by_field:
            wm_id_field = db_query(
                '''SELECT fm.id FROM core."fieldMetadata" fm
                   JOIN core."objectMetadata" om ON om.id = fm."objectMetadataId"
                   WHERE om."nameSingular" = \'workspaceMember\' AND fm.name = \'id\''''
            )
            if wm_id_field:
                import uuid
                db_execute(
                    '''INSERT INTO core."rowLevelPermissionPredicate"
                       ("universalIdentifier", "applicationId", id, "fieldMetadataId", "objectMetadataId",
                        operand, value, "subFieldName", "workspaceMemberFieldMetadataId",
                        "rowLevelPermissionPredicateGroupId", "positionInRowLevelPermissionPredicateGroup",
                        "workspaceId", "roleId", "createdAt", "updatedAt")
                       VALUES (%s, %s, %s, %s, %s, 'IS', NULL, 'workspaceMemberId', %s, NULL, NULL, %s, %s, now(), now())''',
                    (str(uuid.uuid4()), app_id, str(uuid.uuid4()),
                     created_by_field[0]["id"], object_metadata_id,
                     wm_id_field[0]["id"], workspace_id, role_id),
                )

    log.info("RLS updated: role=%s object=%s mode=%s", role_id, object_metadata_id, mode)

    flush_workspace_cache(workspace_id)

    return {"ok": True, "roleId": role_id, "objectMetadataId": object_metadata_id, "mode": mode}


@app.post("/api/rls/fix-bypass")
async def fix_role_bypass(
    request: Request,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Disable role-level bypass flags (canReadAll, canUpdateAll, etc.) for a non-admin role."""
    _check_auth(authorization, token)

    body = await request.json()
    role_id = body.get("roleId")

    if not role_id:
        raise HTTPException(400, "roleId required")

    role_row = db_query(
        'SELECT id, "canUpdateAllSettings" FROM core."role" WHERE id = %s', (role_id,)
    )
    if not role_row:
        raise HTTPException(404, "Role not found")

    if role_row[0]["canUpdateAllSettings"]:
        return {"ok": True, "skipped": True, "detail": "Admin roles keep bypass flags"}

    db_execute(
        '''UPDATE core."role"
           SET "canReadAllObjectRecords" = false,
               "canUpdateAllObjectRecords" = false,
               "canSoftDeleteAllObjectRecords" = false,
               "canDestroyAllObjectRecords" = false
           WHERE id = %s''',
        (role_id,),
    )

    role_row2 = db_query(
        'SELECT "workspaceId" FROM core."role" WHERE id = %s', (role_id,)
    )
    if role_row2:
        flush_workspace_cache(role_row2[0]["workspaceId"])

    log.info("Bypass flags disabled for role=%s", role_id)
    return {"ok": True, "roleId": role_id}


# ---------------------------------------------------------------------------
# RLS Admin — HTML page
# ---------------------------------------------------------------------------

ADMIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acces aux donnees — Twenty CRM</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f4f5f7;color:#1a1a2e;min-height:100vh}
header{background:#141824;color:#fff;padding:20px 32px;display:flex;align-items:center;gap:16px}
header h1{font-size:20px;font-weight:600}
header span{font-size:13px;opacity:.6}
main{max-width:960px;margin:24px auto;padding:0 24px}
.role-row{display:flex;align-items:center;gap:12px;margin-bottom:24px}
.role-row label{font-size:14px;font-weight:600}
select{padding:10px 14px;border:1px solid #ddd;border-radius:8px;font-size:15px;background:#fff}
.obj-card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:12px;padding:16px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.obj-left{display:flex;align-items:center;gap:10px;min-width:200px}
.obj-icon{width:32px;height:32px;background:#e0e0ff;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:#6366f1;flex-shrink:0}
.obj-name{font-size:15px;font-weight:600}
.obj-right{display:flex;align-items:center;gap:20px;flex:1;justify-content:flex-end;flex-wrap:wrap}
.data-access{display:flex;align-items:center;gap:8px}
.data-access label{font-size:12px;color:#666;font-weight:500;white-space:nowrap}
.data-access select{padding:6px 10px;border-radius:6px;font-size:13px;border:1px solid #ddd;min-width:140px}
.crud-badges{display:flex;gap:6px}
.crud-badge{font-size:11px;padding:3px 8px;border-radius:4px;font-weight:500;border:1px solid #e5e7eb}
.crud-on{background:#ecfdf5;color:#059669;border-color:#a7f3d0}
.crud-off{background:#fef2f2;color:#dc2626;border-color:#fecaca}
.toast{position:fixed;bottom:24px;right:24px;background:#141824;color:#fff;padding:12px 20px;border-radius:8px;font-size:13px;opacity:0;transform:translateY(10px);transition:all .3s;pointer-events:none;z-index:99}
.toast.show{opacity:1;transform:translateY(0)}
.hint{font-size:12px;color:#999;margin-top:16px}
.empty{text-align:center;padding:40px;color:#999;font-size:14px}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot-all{background:#10b981}
.dot-own{background:#6366f1}
.dot-none{background:#ef4444}
</style>
</head>
<body>
<header>
  <h1>Acces aux donnees</h1>
  <span>Row-Level Security — Twenty CRM</span>
</header>
<main>
  <div class="role-row">
    <label for="role-select">Selectionner un role :</label>
    <select id="role-select"><option value="">-- Choisir --</option></select>
  </div>
  <div id="objects-container"></div>
</main>
<div id="toast" class="toast"></div>
<script>
var TOKEN = new URLSearchParams(window.location.search).get('token') || '';
var API = window.location.origin + '/api/rls/config';
var API_FIX = window.location.origin + '/api/rls/fix-bypass';
var headers = {'Content-Type':'application/json'};
if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;

var config = [];
var currentRoleIdx = -1;

function toast(msg, ms) {
  ms = ms || 2500;
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function(){ t.classList.remove('show'); }, ms);
}

async function load() {
  try {
    var url = TOKEN ? API + '?token=' + encodeURIComponent(TOKEN) : API;
    var res = await fetch(url, {headers:{'Authorization':'Bearer '+TOKEN}});
    if (!res.ok) { toast('API error: ' + res.status); return; }
    config = await res.json();
    console.log('[RLS] loaded config:', config.length, 'roles');

    var sel = document.getElementById('role-select');
    sel.innerHTML = '<option value="">-- Choisir --</option>';
    for (var i = 0; i < config.length; i++) {
      var r = config[i];
      if (r.isAdmin) continue;
      var opt = document.createElement('option');
      opt.value = i;
      opt.textContent = r.label;
      sel.appendChild(opt);
      console.log('[RLS] added role:', r.label, 'idx:', i);

      if (r.canReadAll || r.canUpdateAll || r.canSoftDeleteAll || r.canDestroyAll) {
        console.log('[RLS] auto-fixing bypass flags for:', r.label);
        fetch(API_FIX, {
          method: 'POST', headers: headers,
          body: JSON.stringify({roleId: r.roleId})
        }).then(function(fr) { return fr.json(); }).then(function(d) {
          if (d.ok) { console.log('[RLS] bypass flags fixed'); }
        });
        r.canReadAll = false;
        r.canUpdateAll = false;
        r.canSoftDeleteAll = false;
        r.canDestroyAll = false;
      }
    }
    sel.onchange = function() { currentRoleIdx = parseInt(sel.value) || -1; render(); };
    render();
  } catch(e) {
    console.error('[RLS] load error:', e);
    toast('Erreur chargement: ' + e.message);
  }
}

function render() {
  var c = document.getElementById('objects-container');
  if (currentRoleIdx < 0 || !config[currentRoleIdx]) {
    c.innerHTML = '<div class="empty">Selectionnez un role pour configurer ses permissions.</div>';
    return;
  }
  var role = config[currentRoleIdx];
  var html = '';
  for (var j = 0; j < role.objects.length; j++) {
    var obj = role.objects[j];
    var m = obj.mode;
    var dotClass = m === 'all' ? 'dot-all' : m === 'own' ? 'dot-own' : 'dot-none';
    var selAll = m === 'all' ? ' selected' : '';
    var selOwn = m === 'own' ? ' selected' : '';
    var selNone = m === 'none' ? ' selected' : '';
    html += '<div class="obj-card" data-oid="' + obj.objectMetadataId + '">' +
      '<div class="obj-left">' +
        '<div class="obj-icon">' + obj.labelSingular.charAt(0).toUpperCase() + '</div>' +
        '<div class="obj-name">' + obj.labelSingular + '</div>' +
      '</div>' +
      '<div class="obj-right">' +
        '<div class="data-access">' +
          '<label>Acces aux donnees :</label>' +
          '<select onchange="setMode(\\'' + obj.objectMetadataId + '\\',this.value)">' +
            '<option value="all"' + selAll + '>Tout</option>' +
            '<option value="own"' + selOwn + '>Mes donnees</option>' +
            '<option value="none"' + selNone + '>Aucun</option>' +
          '</select>' +
        '</div>' +
        '<div class="crud-badges">' +
          '<span class="crud-badge ' + (obj.canUpdate ? 'crud-on' : 'crud-off') + '">Modifier</span>' +
          '<span class="crud-badge ' + (obj.canSoftDelete ? 'crud-on' : 'crud-off') + '">Supprimer</span>' +
          '<span class="crud-badge ' + (obj.canDestroy ? 'crud-on' : 'crud-off') + '">Detruire</span>' +
        '</div>' +
      '</div>' +
    '</div>';
  }
  c.innerHTML = html;
  var hint = '<p class="hint">Les changements sont appliques instantanement.</p>';
  hint += '<p class="hint">La portee s\\'applique egalement aux operations Modifier, Supprimer et Detruire.</p>';
  c.innerHTML += hint;
}

async function setMode(objectMetadataId, mode) {
  var role = config[currentRoleIdx];
  var objName = objectMetadataId;
  for (var n = 0; n < role.objects.length; n++) {
    if (role.objects[n].objectMetadataId === objectMetadataId) { objName = role.objects[n].labelSingular; break; }
  }
  try {
    var res = await fetch(API, {
      method: 'POST', headers: headers,
      body: JSON.stringify({roleId: role.roleId, objectMetadataId: objectMetadataId, mode: mode})
    });
    var data = await res.json();
    if (data.ok) {
      for (var n = 0; n < role.objects.length; n++) {
        if (role.objects[n].objectMetadataId === objectMetadataId) { role.objects[n].mode = mode; break; }
      }
      var label = mode === 'all' ? 'Tout' : mode === 'own' ? 'Mes donnees' : 'Aucun';
      toast(objName + ' -> ' + label);
    } else {
      toast('Erreur: ' + (data.detail || 'inconnue'));
    }
  } catch(e) {
    console.error('[RLS] setMode error:', e);
    toast('Erreur: ' + e.message);
  }
}

load();
</script>
</body>
</html>"""


@app.get("/admin/rls", response_class=HTMLResponse)
async def rls_admin_page(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    _check_auth(authorization, token)
    from fastapi.responses import HTMLResponse as _HR
    return _HR(content=ADMIN_PAGE_HTML, media_type="text/html; charset=utf-8")
