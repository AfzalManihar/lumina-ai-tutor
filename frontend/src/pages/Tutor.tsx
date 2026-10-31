import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, errorText } from "../api/client";
import type { Conversation, Doc, Page } from "../api/types";
import { ChatPanel } from "../components/ChatPanel";
import { AttachBar } from "../components/AttachBar";
import { DocPicker } from "../components/DocPicker";
import { Button, Confirm, ErrorBanner, PageHeader } from "../components/ui";
import { useLoad } from "../hooks";
import { fmtDateTime } from "../lib";

export function Tutor() {
  const [params, setParams] = useSearchParams();
  const convId = params.get("c");
  const convs = useLoad(() => api.get<Page<Conversation>>("/tutor/conversations", { limit: 30 }));
  const docs = useLoad(() => api.get<Page<Doc>>("/documents", { limit: 100 }));
  const [selected, setSelected] = useState<string[]>(() => (params.get("docs") ?? "").split(",").filter(Boolean));
  const [confirm, setConfirm] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const select = (id: string | null) => setParams(id ? { c: id } : {}, { replace: true });
  async function remove(id: string) {
    try { await api.del(`/tutor/conversations/${id}`); setConfirm(null); if (convId === id) select(null); await convs.reload(); }
    catch (e) { setErr(errorText(e)); }
  }
  return (
    <>
      <PageHeader title="Tutor chat">Ask anything, or attach a PDF and keep asking doubts about it. Answers from your PDF cite the page.</PageHeader>
      <ErrorBanner message={err ?? convs.error} />
      <div className="split">
        <aside className="side" aria-label="Conversations">
          <Button variant="secondary" onClick={() => { setSelected([]); select(null); }}>New conversation</Button>
          <ul className="convlist">
            {(convs.data?.items ?? []).map((c) => (
              <li key={c.id} className={c.id === convId ? "active" : undefined}>
                <button className="link" onClick={() => select(c.id)}>{c.title || "Untitled"}<small>{fmtDateTime(c.updated_at)}</small></button>
                {confirm === c.id ? <Confirm message="Delete this conversation?" onYes={() => remove(c.id)} onNo={() => setConfirm(null)} />
                  : <button className="link link-danger" aria-label={`Delete conversation ${c.title}`} onClick={() => setConfirm(c.id)}>Delete</button>}
              </li>
            ))}
            {convs.data?.items.length === 0 && <li className="muted">No conversations yet.</li>}
          </ul>
        </aside>
        <div className="chat-wrap">
          <ChatPanel conversationId={convId} onConversation={(id) => { select(id); void convs.reload(); }}
            documentIds={selected} onLoaded={(c) => setSelected(c.document_ids ?? [])}
            above={<AttachBar docs={docs.data?.items ?? []} selected={selected} onChange={setSelected} onUploaded={docs.reload} />} />
        </div>
      </div>
    </>
  );
}

export function AskDocuments() {
  const docs = useLoad(() => api.get<Page<Doc>>("/documents", { limit: 100 }));
  const [selected, setSelected] = useState<string[]>([]);
  const [conv, setConv] = useState<string | null>(null);
  return (
    <>
      <PageHeader title="Ask your documents">Answers come only from the documents you select. If they don’t contain the answer, you will be told.</PageHeader>
      <ErrorBanner message={docs.error} onRetry={docs.reload} />
      <div className="split">
        <aside className="side">
          <DocPicker docs={docs.data?.items ?? []} selected={selected} onChange={(s) => { setSelected(s); }} />
          <Button variant="ghost" onClick={() => setConv(null)}>Start a new thread</Button>
        </aside>
        <div className="chat-wrap">
          <ChatPanel conversationId={conv} onConversation={setConv} documentIds={selected} requireDocuments
            placeholder="Ask a question about the selected documents" />
        </div>
      </div>
    </>
  );
}
