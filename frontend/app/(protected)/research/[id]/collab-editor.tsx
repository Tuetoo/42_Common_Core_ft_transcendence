"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import * as Y from "yjs";
import { wsUrl } from "@/app/lib/ws-url";

// Lifecycle of the live connection, as shown to the user.
type ConnectionStatus = "connecting" | "synced" | "reconnecting" | "ended";

const STATUS_TEXT: Record<ConnectionStatus, string> = {
  connecting: "Connecting…",
  synced: "Live: changes sync and save automatically",
  reconnecting: "Connection lost, reconnecting…",
  ended: "Disconnected",
};

const STATUS_COLOR: Record<ConnectionStatus, string> = {
  connecting: "var(--accent)",
  synced: "var(--up)",
  reconnecting: "var(--accent)",
  ended: "var(--down)",
};

// Close codes from services/core-service/api/ws_common.py.
const CLOSE_UNAUTHORIZED = 4401; // token expired or invalid: get a new one
const CLOSE_FORBIDDEN = 4403; // no access (or the note was deleted)
const FATAL_CLOSE_CODES = new Set([1003, 1008, 4400, CLOSE_FORBIDDEN]);

const REMOTE = "remote";
const MAX_BACKOFF_MS = 30_000;

// Turns a plain "old string -> new string" edit into a targeted Y.Text
// delete+insert instead of wiping and re-typing the whole field. This
// matters for a CRDT: a full wipe on every keystroke would make two
// people's concurrent edits fight over "whose wipe wins" — small, precise
// ops are what let Yjs merge them cleanly instead.
function applyTextDiff(ytext: Y.Text, oldValue: string, newValue: string) {
  let start = 0;
  while (start < oldValue.length && start < newValue.length && oldValue[start] === newValue[start]) {
    start++;
  }
  let oldEnd = oldValue.length;
  let newEnd = newValue.length;
  while (oldEnd > start && newEnd > start && oldValue[oldEnd - 1] === newValue[newEnd - 1]) {
    oldEnd--;
    newEnd--;
  }
  if (oldEnd > start) {
    ytext.delete(start, oldEnd - start);
  }
  if (newEnd > start) {
    ytext.insert(start, newValue.slice(start, newEnd));
  }
}

type RelativeSelection = { start: Y.RelativePosition; end: Y.RelativePosition } | null;

// Someone else's edit re-renders the textarea, which would move the caret
// to the end. Remember the caret relative to the Yjs text before applying
// the edit, and turn it back into indexes afterwards.
function captureSelection(el: HTMLTextAreaElement | null, ytext: Y.Text): RelativeSelection {
  if (!el || document.activeElement !== el) return null;
  return {
    start: Y.createRelativePositionFromTypeIndex(ytext, el.selectionStart),
    end: Y.createRelativePositionFromTypeIndex(ytext, el.selectionEnd),
  };
}

function resolveSelection(selection: RelativeSelection, doc: Y.Doc): { start: number; end: number } | null {
  if (!selection) return null;
  const start = Y.createAbsolutePositionFromRelativePosition(selection.start, doc);
  const end = Y.createAbsolutePositionFromRelativePosition(selection.end, doc);
  return start && end ? { start: start.index, end: end.index } : null;
}

// Asks the frontend server for the current access token (the session
// cookie is httpOnly). null: the session is over. undefined: network error.
async function fetchFreshToken(): Promise<string | null | undefined> {
  try {
    const res = await fetch("/research/collab-token", { cache: "no-store" });
    if (res.status === 401) return null;
    if (!res.ok) return undefined;
    const body = (await res.json()) as { token?: string };
    return body.token ?? undefined;
  } catch {
    return undefined;
  }
}

// Live editor for a note's body. The server room holds a Yjs document whose
// Y.Text "content" is saved back into note.content
// (services/core-service/yjs_relay.py). Protocol: on connect the server
// sends its full state as one binary message; after that each message in
// either direction is a Yjs update.
export function CollabEditor({
  noteId,
  accessToken,
  canEdit,
  initialText,
}: {
  noteId: string;
  accessToken: string;
  canEdit: boolean;
  initialText: string;
}) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [detail, setDetail] = useState<string | null>(null);
  const [text, setText] = useState(initialText);
  const ytextRef = useRef<Y.Text | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const tokenRef = useRef(accessToken);
  // Caret to restore after a remote edit re-renders the textarea.
  const pendingSelection = useRef<{ start: number; end: number } | null>(null);

  // A server re-render (e.g. router.refresh) may bring a newer token; keep
  // it for the next reconnect without tearing down a working socket.
  useEffect(() => {
    tokenRef.current = accessToken;
  }, [accessToken]);

  useEffect(() => {
    // One Y.Doc for the whole life of the editor, across reconnects, so
    // edits that had not reached the server yet are never thrown away.
    const doc = new Y.Doc();
    const ytext = doc.getText("content");
    ytextRef.current = ytext;

    let ws: WebSocket | null = null;
    let disposed = false;
    let everSynced = false;
    let attempts = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    function connect(token: string) {
      if (disposed) return;
      let synced = false;
      const socket = new WebSocket(wsUrl(`/yjs/${noteId}?token=${encodeURIComponent(token)}`));
      socket.binaryType = "arraybuffer";
      ws = socket;

      socket.onopen = () => {
        // The component unmounted while connecting: close now that it is
        // safe (closing a CONNECTING socket logs a browser warning).
        if (disposed) socket.close(1000);
      };

      socket.onmessage = (event) => {
        if (disposed || !(event.data instanceof ArrayBuffer)) return;
        const update = new Uint8Array(event.data);
        const selection = captureSelection(textareaRef.current, ytext);
        if (!synced) {
          // First message: the server's full state. On a reconnect, send
          // back whatever this doc has that the server lacks (an edit that
          // was in flight when the socket dropped).
          const serverVector = Y.encodeStateVectorFromUpdate(update);
          Y.applyUpdate(doc, update, REMOTE);
          if (everSynced) {
            const missing = Y.encodeStateAsUpdate(doc, serverVector);
            if (missing.length > 2) socket.send(missing);
          }
          synced = true;
          everSynced = true;
          attempts = 0;
          pendingSelection.current = resolveSelection(selection, doc);
          setText(ytext.toString());
          setStatus("synced");
          setDetail(null);
          return;
        }
        Y.applyUpdate(doc, update, REMOTE);
        pendingSelection.current = resolveSelection(selection, doc);
      };

      socket.onclose = (event) => {
        if (ws === socket) ws = null;
        if (disposed) return;
        if (FATAL_CLOSE_CODES.has(event.code)) {
          setStatus("ended");
          setDetail(
            event.code === CLOSE_FORBIDDEN
              ? `You no longer have access to this note${event.reason ? ` (${event.reason})` : ""}.`
              : "The server refused an edit. Reload the page to continue."
          );
          return;
        }
        // An expired token gets an immediate retry with a new one; other
        // drops back off (1s, 2s, 4s ... 30s, plus jitter).
        const delay =
          event.code === CLOSE_UNAUTHORIZED && attempts === 0
            ? 0
            : Math.min(1000 * 2 ** attempts, MAX_BACKOFF_MS) + Math.random() * 500;
        attempts += 1;
        setStatus("reconnecting");
        setDetail(event.code === 4429 ? "Too many open connections for your account." : null);
        scheduleReconnect(delay);
      };
    }

    function scheduleReconnect(delay: number) {
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = setTimeout(async () => {
        retryTimer = null;
        const token = await fetchFreshToken();
        if (disposed) return;
        if (token === null) {
          setStatus("ended");
          setDetail("Your session has ended. Log in again to keep editing.");
          return;
        }
        if (token === undefined) {
          attempts += 1;
          scheduleReconnect(Math.min(1000 * 2 ** attempts, MAX_BACKOFF_MS));
          return;
        }
        tokenRef.current = token;
        connect(token);
      }, delay);
    }

    // Back online: don't wait for the rest of the backoff.
    function onOnline() {
      if (retryTimer) scheduleReconnect(0);
    }
    window.addEventListener("online", onOnline);

    const onDocUpdate = (update: Uint8Array, origin: unknown) => {
      setText(ytext.toString());
      // Don't echo a remote update straight back to the server that sent it.
      if (origin === REMOTE) return;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(update);
      }
    };
    doc.on("update", onDocUpdate);

    connect(tokenRef.current);

    return () => {
      disposed = true;
      window.removeEventListener("online", onOnline);
      if (retryTimer) clearTimeout(retryTimer);
      doc.off("update", onDocUpdate);
      const socket = ws;
      ws = null;
      if (socket) {
        socket.onmessage = null;
        // A CONNECTING socket is closed from its onopen (see above).
        if (socket.readyState === WebSocket.OPEN) socket.close(1000);
      }
      doc.destroy();
      ytextRef.current = null;
    };
  }, [noteId]);

  useLayoutEffect(() => {
    const el = textareaRef.current;
    const selection = pendingSelection.current;
    if (el && selection && document.activeElement === el) {
      el.setSelectionRange(selection.start, selection.end);
    }
    pendingSelection.current = null;
  }, [text]);

  const editable = canEdit && status === "synced";

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const ytext = ytextRef.current;
    if (!ytext || !editable) return;
    applyTextDiff(ytext, ytext.toString(), e.target.value);
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={`collab-${noteId}`} className="text-xs font-medium text-[var(--text-secondary)]">
          Note content {canEdit ? "(live, shared editing)" : "(live, read only)"}
        </label>
        <div
          role="status"
          aria-live="polite"
          className="mono flex items-center gap-2 text-xs text-[var(--text-secondary)]"
        >
          <span
            aria-hidden="true"
            className="inline-block h-[7px] w-[7px] rounded-full"
            style={{ background: STATUS_COLOR[status] }}
          />
          <span>
            {STATUS_TEXT[status]}
            {detail && ` — ${detail}`}
          </span>
        </div>
      </div>
      <textarea
        id={`collab-${noteId}`}
        ref={textareaRef}
        value={text}
        onChange={handleChange}
        readOnly={!editable}
        aria-readonly={!editable}
        aria-describedby={`collab-help-${noteId}`}
        rows={12}
        placeholder={canEdit ? "Start typing. Everyone with this note open sees your edits live." : ""}
        className={`w-full min-w-0 rounded-md border border-[var(--border)] bg-[var(--bg)] px-[11px] py-[9px] text-[13px] leading-relaxed text-[var(--text)] focus:border-[var(--accent)] focus:outline-none ${
          editable ? "" : "cursor-default opacity-80"
        }`}
      />
      <p id={`collab-help-${noteId}`} className="m-0 text-xs text-[var(--text-faint)]">
        {!canEdit
          ? "You can read this note but not edit it. Only the author and organization owners, admins and members can edit."
          : status === "synced"
            ? "Saved automatically: within about 30 seconds of an edit and when the last editor leaves."
            : "Editing is paused until the connection is back, so no change is lost."}
      </p>
    </div>
  );
}
