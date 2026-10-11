"use client";

import { useEffect, useRef, useState } from "react";
import * as Y from "yjs";
import { wsUrl } from "@/app/lib/ws-url";

type ConnectionStatus = "connecting" | "open" | "closed";

const STATUS_COLOR: Record<ConnectionStatus, string> = {
  connecting: "var(--accent)",
  open: "var(--up)",
  closed: "var(--down)",
};

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

export function CollabEditor({ noteId, accessToken }: { noteId: string; accessToken: string }) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [text, setText] = useState("");
  const ytextRef = useRef<Y.Text | null>(null);

  useEffect(() => {
    const doc = new Y.Doc();
    const ytext = doc.getText("content");
    ytextRef.current = ytext;

    const url = wsUrl(`/yjs/${noteId}?token=${accessToken}`);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => setStatus("open");
    ws.onclose = () => setStatus("closed");
    ws.onmessage = (event) => {
      Y.applyUpdate(doc, new Uint8Array(event.data as ArrayBuffer), "remote");
    };

    const onDocUpdate = (update: Uint8Array, origin: unknown) => {
      setText(ytext.toString());
      // Don't echo a remote update straight back to the server that sent it.
      if (origin === "remote") return;
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(update);
      }
    };
    doc.on("update", onDocUpdate);

    return () => {
      doc.off("update", onDocUpdate);
      ws.close();
      doc.destroy();
    };
  }, [noteId, accessToken]);

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const ytext = ytextRef.current;
    if (!ytext) return;
    applyTextDiff(ytext, ytext.toString(), e.target.value);
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 mono" style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: 999,
            background: STATUS_COLOR[status],
            display: "inline-block",
          }}
        />
        {status}
      </div>
      <textarea
        value={text}
        onChange={handleChange}
        rows={8}
        placeholder="Start typing — open this note in another tab to see it sync live."
        style={{
          background: "var(--bg)",
          border: "1px solid var(--border)",
          borderRadius: 6,
          padding: "9px 11px",
          color: "var(--text)",
          fontFamily: "inherit",
          fontSize: 13,
        }}
      />
    </div>
  );
}
