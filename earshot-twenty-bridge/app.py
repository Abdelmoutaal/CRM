"""Earshot → Twenty CRM bridge.

Receives Earshot webhook POSTs and creates Notes + CallRecordings in Twenty CRM.

Configure via environment variables:
  TWENTY_API_URL   – e.g. http://twenty-server:3000  (internal Docker network)
  TWENTY_API_KEY   – a Twenty API key (Settings → API keys)
  BRIDGE_SECRET    – optional shared secret Earshot should send as Bearer token
  PORT              – listen port (default 8100)
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TWENTY_API_URL = os.environ.get("TWENTY_API_URL", "http://twenty-server:3000")
TWENTY_API_KEY = os.environ.get("TWENTY_API_KEY", "")
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("earshot-bridge")

app = FastAPI(title="Earshot → Twenty Bridge")

# ---------------------------------------------------------------------------
# Models (matches Earshot webhook payload)
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
    notes: Optional[dict] = None  # MeetingNotes schema
    bookmarks: Optional[list] = None


# ---------------------------------------------------------------------------
# Twenty CRM GraphQL helpers
# ---------------------------------------------------------------------------

def twenty_gql(query: str, variables: dict | None = None) -> dict:
    """Run a GraphQL query/mutation against Twenty CRM."""
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
    """Find a Twenty Person by primary email. Returns person ID or None."""
    q = """
    query FindPerson($email: String!) {
      people(filter: { emails: { primaryEmail: { eq: $email } } }) {
        edges {
          node { id }
        }
      }
    }
    """
    data = twenty_gql(q, {"email": email})
    edges = data.get("data", {}).get("people", {}).get("edges", [])
    if edges:
        return edges[0]["node"]["id"]
    return None


def find_person_by_name(name: str) -> Optional[str]:
    """Fallback: find Person by name (firstName contains)."""
    q = """
    query FindPersonByName($name: String!) {
      people(filter: { name: { firstName: { contains: $name } } }) {
        edges {
          node { id name { firstName lastName } emails { primaryEmail } }
        }
      }
    }
    """
    data = twenty_gql(q, {"name": name})
    edges = data.get("data", {}).get("people", {}).get("edges", [])
    if edges:
        return edges[0]["node"]["id"]
    return None


def create_note(title: str, body: str, person_ids: list[str]) -> Optional[str]:
    """Create a Note in Twenty and link it to persons. Returns note ID."""
    # Create the note
    q = """
    mutation CreateNote($body: String!, $title: String!) {
      createNote(data: { body: $body, title: $title }) {
        note { id }
      }
    }
    """
    data = twenty_gql(q, {"body": body, "title": title})
    note_id = data.get("data", {}).get("createNote", {}).get("note", {}).get("id")
    if not note_id:
        log.error("Failed to create note: %s", data)
        return None

    # Link note to each person via NoteTargets
    for pid in person_ids:
        link_q = """
        mutation LinkNoteToPerson($noteId: ID!, $personId: ID!) {
          createNoteTarget(
            data: {
              note: { id: $noteId },
              person: { id: $personId }
            }
          ) {
            noteTarget { id }
          }
        }
        """
        twenty_gql(link_q, {"noteId": note_id, "personId": pid})

    return note_id


def create_call_recording(
    title: str,
    summary: str,
    transcript: Optional[str],
    duration_secs: Optional[int],
    start_date: Optional[str],
    person_ids: list[str],
) -> Optional[str]:
    """Create a CallRecording in Twenty linked to persons."""
    q = """
    mutation CreateCall($input: CreateCallRecordingInput!) {
      createCallRecording(data: $input) {
        callRecording { id }
      }
    }
    """

    call_data: dict = {
        "title": title,
        "summary": summary or "",
    }

    if transcript:
        call_data["transcript"] = transcript
    if duration_secs:
        call_data["durationInSeconds"] = duration_secs
    if start_date:
        call_data["startDateTime"] = start_date

    data = twenty_gql(q, {"input": call_data})
    call_id = data.get("data", {}).get("createCallRecording", {}).get("callRecording", {}).get("id")

    if not call_id:
        log.error("Failed to create call recording: %s", data)
        return None

    # Link to persons via CallParticipant
    for pid in person_ids:
        link_q = """
        mutation LinkCallToPerson($callId: ID!, $personId: ID!) {
          createCallParticipant(
            data: {
              callRecording: { id: $callId },
              person: { id: $personId }
            }
          ) {
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
async def receive_meeting(
    meeting: EarshotMeeting,
    authorization: Optional[str] = Header(None),
):
    """Receive an Earshot meeting webhook and sync to Twenty CRM."""

    # Optional auth check
    if BRIDGE_SECRET:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != BRIDGE_SECRET:
            raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")

    log.info("Received meeting: %s (%s)", meeting.title, meeting.id)

    # Resolve notes content
    notes_data = meeting.notes or {}
    summary = notes_data.get("summary", "")
    note_sections = notes_data.get("sections", [])
    action_items = notes_data.get("action_items", [])
    note_attendees = notes_data.get("attendees", meeting.attendees)

    # Build the Note body (Markdown)
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

    # Resolve attendees to Person IDs
    resolved_person_ids: list[str] = []
    for attendee in note_attendees:
        # Try email first (if attendee looks like an email)
        person_id = None
        if "@" in attendee:
            person_id = find_person_by_email(attendee)
        if not person_id:
            # Try name
            person_id = find_person_by_name(attendee)
        if person_id and person_id not in resolved_person_ids:
            resolved_person_ids.append(person_id)

    log.info(
        "Resolved %d / %d attendees to Person records",
        len(resolved_person_ids),
        len(note_attendees),
    )

    # Create Note in Twenty
    note_id = create_note(note_title, note_body, resolved_person_ids)
    log.info("Created Note: %s", note_id)

    # Create CallRecording if transcript is available
    call_id = None
    if meeting.transcript:
        call_id = create_call_recording(
            title=meeting.title,
            summary=summary,
            transcript=meeting.transcript,
            duration_secs=meeting.duration_secs,
            start_date=meeting.date_iso,
            person_ids=resolved_person_ids,
        )
        log.info("Created CallRecording: %s", call_id)

    return {
        "ok": True,
        "meeting_id": meeting.id,
        "twenty_note_id": note_id,
        "twenty_call_recording_id": call_id,
        "persons_linked": len(resolved_person_ids),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "earshot-twenty-bridge"}
