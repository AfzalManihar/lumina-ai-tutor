import { useRef, useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorText } from "../api/client";
import type { Doc, Page } from "../api/types";
import { Badge, Button, Card, Confirm, EmptyState, ErrorBanner, Icon, Loading, Notice, PageHeader } from "../components/ui";
import { useLoad } from "../hooks";
import { fmtBytes, fmtDate } from "../lib";

const ACCEPT = ".pdf,.txt,.md,.markdown";

export function Documents() {
  const { data, error, loading, reload } = useLoad(() => api.get<Page<Doc>>("/documents", { limit: 100 }));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function upload(file: File) {
    setErr(null); setOk(null); setBusy(true);
    const form = new FormData(); form.append("file", file);
    try { const d = await api.upload<Doc>("/documents", form); setOk(`${d.filename} is ready: ${d.chunk_count} passages indexed.`); await reload(); }
    catch (e) { setErr(errorText(e)); } finally { setBusy(false); if (input.current) input.current.value = ""; }
  }
  const onDrop = (e: DragEvent) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files?.[0]; if (f) void upload(f); };
  async function act(id: string, fn: () => Promise<unknown>) {
    setErr(null); setWorking(id);
    try { await fn(); setConfirm(null); await reload(); } catch (e) { setErr(errorText(e)); } finally { setWorking(null); }
  }
  return (
    <>
      <PageHeader title="Document library">Upload PDF, TXT or Markdown files. Text is extracted, split into passages and indexed for your account only.</PageHeader>
      <Card>
        <div className={`drop${drag ? " drop-on" : ""}`} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={onDrop}>
          <span className="drop-icon" aria-hidden="true"><Icon name="upload" size={26} /></span>
          <p>Drop a file here, or</p>
          <input ref={input} id="file" type="file" accept={ACCEPT} className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); }} />
          <label htmlFor="file" className="btn btn-primary" aria-disabled={busy}>{busy ? "Indexing…" : "Choose a file"}</label>
          <p className="field-hint">Scanned PDFs without text and encrypted PDFs are rejected. Maximum 10 MB by default.</p>
        </div>
        {busy && <Loading label="Extracting and indexing" />}
        <ErrorBanner message={err ?? error} onRetry={error ? reload : undefined} />
        {ok && <Notice tone="ok">{ok}</Notice>}
      </Card>
      <Card title={`Your documents${data ? ` (${data.total})` : ""}`}>
        {loading && !data && <Loading label="Loading documents" />}
        {data && data.items.length === 0 && <EmptyState title="No documents yet">Upload your first file above to start asking questions about it.</EmptyState>}
        {data && data.items.length > 0 && (
          <div className="tablewrap"><table>
            <thead><tr><th>File</th><th>Status</th><th>Pages</th><th>Passages</th><th>Size</th><th>Added</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{data.items.map((d) => (
              <tr key={d.id}>
                <td>{d.filename}</td>
                <td><Badge tone={d.status === "ready" ? "ok" : d.status === "failed" ? "warn" : "neutral"}>{d.status}</Badge>{d.error && <small className="muted"> {d.error}</small>}</td>
                <td>{d.pages ?? "—"}</td><td>{d.chunk_count}</td><td>{fmtBytes(d.size_bytes)}</td><td>{fmtDate(d.created_at)}</td>
                <td><div className="actions">
                  {confirm === d.id ? <Confirm message={`Delete ${d.filename}?`} onYes={() => act(d.id, () => api.del(`/documents/${d.id}`))} onNo={() => setConfirm(null)} /> : <>
                    <Link to={`/tutor?docs=${d.id}`}>Chat</Link>
                    <Button variant="ghost" loading={working === d.id} onClick={() => act(d.id, () => api.post(`/documents/${d.id}/reindex`))}>Re-index</Button>
                    <Button variant="ghost" onClick={() => setConfirm(d.id)}>Delete</Button></>}
                </div></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </>
  );
}
