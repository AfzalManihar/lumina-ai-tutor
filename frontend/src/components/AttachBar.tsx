import { useRef, useState } from "react";
import { api, errorText } from "../api/client";
import type { Doc } from "../api/types";
import { DocPicker } from "./DocPicker";
import { ErrorBanner, Icon, Spinner } from "./ui";

const ACCEPT = ".pdf,.txt,.md,.markdown";

interface Props {
  docs: Doc[];                       // all of the student's documents
  selected: string[];                // ids attached to this chat
  onChange: (ids: string[]) => void;
  onUploaded: () => void | Promise<void>;
  disabled?: boolean;
}

/** ChatGPT-style attachment row: attach a new PDF, or pick already uploaded ones. Chips show what the chat is grounded in. */
export function AttachBar({ docs, selected, onChange, onUploaded, disabled }: Props) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const byId = new Map(docs.map((d) => [d.id, d]));

  async function upload(file: File) {
    setErr(null); setBusy(true);
    const form = new FormData(); form.append("file", file);
    try {
      const d = await api.upload<Doc>("/documents", form);   // synchronous: the document is indexed when this returns
      await onUploaded();
      onChange([...selected, d.id]);
    } catch (e) { setErr(errorText(e)); }
    finally { setBusy(false); if (input.current) input.current.value = ""; }
  }

  return (
    <div className="attachbar">
      <div className="attach-row">
        <input ref={input} id="chat-attach" type="file" accept={ACCEPT} className="sr-only" disabled={busy || disabled}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); }} />
        <label htmlFor="chat-attach" className="chip attach-btn" aria-disabled={busy || disabled} title="Upload a PDF, TXT or Markdown file and chat with it">
          {busy ? <Spinner small /> : <Icon name="upload" size={16} />} {busy ? "Reading your file…" : "Attach PDF"}
        </label>
        <button type="button" className="chip" aria-expanded={open} onClick={() => setOpen((v) => !v)} disabled={disabled}>
          {open ? "Hide my documents" : "My documents"}
        </button>
        {selected.map((id) => (
          <span key={id} className="chip chip-on doc-chip" title="This chat answers from this document">
            {byId.get(id)?.filename ?? "Document"}
            <button type="button" className="chip-x" aria-label={`Remove ${byId.get(id)?.filename ?? "document"} from this chat`}
              onClick={() => onChange(selected.filter((x) => x !== id))}>×</button>
          </span>
        ))}
        {selected.length === 0 && <small className="muted">No document attached: general tutoring.</small>}
      </div>
      {open && <DocPicker docs={docs} selected={selected} onChange={onChange} />}
      <ErrorBanner message={err} />
    </div>
  );
}
