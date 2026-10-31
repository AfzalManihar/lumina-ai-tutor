import { motion } from "motion/react";
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, errorText } from "../api/client";
import type { ChatMessage, ChatResult, ConversationDetail } from "../api/types";
import { Badge, Button, ErrorBanner, Icon, Spinner } from "./ui";
import { Citations } from "./Citations";
import { SPRING, WordReveal } from "./fx";
import { StepList } from "./StepList";

const STARTERS = ["Explain photosynthesis step by step", "Quiz me on basic algebra", "Make me a 7-day revision plan"];
const LESSON_STARTERS = ["Explain this lesson again in simpler words", "Give me a real-world example", "What should I remember most?"];
const DOC_STARTERS = ["Summarise the key ideas", "List the main definitions", "What are the most important dates or numbers?"];

interface Props {
  conversationId: string | null;
  onConversation: (id: string) => void;
  documentIds?: string[] | null;       // set => answers must be grounded in these documents
  requireDocuments?: boolean;          // Document Q&A mode: never fall back to a general answer
  placeholder?: string;
  above?: ReactNode;                   // e.g. the attachment bar, rendered right above the composer
  onLoaded?: (c: ConversationDetail) => void;
  onMissing?: () => void;              // the saved conversation no longer exists (deleted); the page should start fresh
  topic?: string;                      // e.g. the lesson being studied
  compact?: boolean;                   // embedded use: no big empty-state hero
  emptyHint?: string;  // lets the page restore the thread's saved document scope
}

export function ChatPanel({ conversationId, onConversation, documentIds, requireDocuments, placeholder, above, onLoaded, onMissing, topic, compact, emptyHint }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSent, setLastSent] = useState<string | null>(null);
  const [loadingConv, setLoadingConv] = useState(false);
  const [freshId, setFreshId] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const convRef = useRef<string | null>(conversationId);

  useEffect(() => {
    convRef.current = conversationId;
    if (!conversationId) { setMessages([]); return; }
    let alive = true;
    setLoadingConv(true);
    api.get<ConversationDetail>(`/tutor/conversations/${conversationId}`)
      .then((c) => { if (alive && convRef.current === conversationId) { setMessages(c.messages); onLoaded?.(c); } })
      .catch((e) => { if (!alive) return; if (e instanceof ApiError && e.status === 404) onMissing?.(); else setError(errorText(e)); })
      .finally(() => alive && setLoadingConv(false));
    return () => { alive = false; };
  }, [conversationId]);

  useEffect(() => { endRef.current?.scrollIntoView?.({ block: "end" }); }, [messages, busy]);

  const grounded = !!documentIds && documentIds.length > 0;
  const blocked = requireDocuments && (!documentIds || documentIds.length === 0);

  async function send(message: string) {
    setError(null); setBusy(true); setLastSent(message);
    const tempId = `tmp-${Date.now()}`;
    setMessages((m) => [...m, { id: tempId, role: "user", content: message }]);
    try {
      const r = await api.post<ChatResult>("/tutor/chat", {
        message, conversation_id: convRef.current ?? undefined, ...(topic ? { topic } : {}),
        // An array (even empty) is the thread's explicit scope; empty = detach documents, null = leave scope alone.
        ...(documentIds ? { document_ids: documentIds, use_documents: documentIds.length > 0 } : {}),
      });
      if (!convRef.current) { convRef.current = r.conversation_id; onConversation(r.conversation_id); }
      setMessages((m) => [...m, {
        id: r.message_id, role: "assistant", content: r.answer, suggestion: r.memory_suggestion,
        meta: { intent: r.intent, grounding_status: r.grounding_status, citations: r.citations, limitations: r.limitations,
                flags: r.flags, follow_ups: r.follow_ups, quiz_id: r.quiz_id, lesson_id: r.lesson_id, status: r.status, steps: r.steps, stats: r.stats },
      }]);
      setFreshId(r.message_id);
      setLastSent(null);
    } catch (e) {
      setMessages((m) => m.filter((x) => x.id !== tempId));
      setError(errorText(e));
    } finally { setBusy(false); }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const v = text.trim();
    if (!v || busy || blocked) return;
    setText("");
    void send(v);
  }

  async function saveSuggestion(id: string, s: { text: string; type: string }) {
    if (!s.text) { setMessages((m) => m.map((x) => (x.id === id ? { ...x, suggestion: null } : x))); return; }  // dismissed
    try {
      await api.post("/memory", { text: s.text, type: s.type, source: "approved_suggestion" });
      setMessages((m) => m.map((x) => (x.id === id ? { ...x, suggestion: null } : x)));
    } catch (e) { setError(errorText(e)); }
  }

  return (
    <div className="chat">
      <div className="chat-log" aria-live="polite" aria-label="Conversation">
        {loadingConv && <div className="loading"><Spinner /><span>Loading conversation…</span></div>}
        {!loadingConv && messages.length === 0 && (
          <div className={`chat-empty${compact ? " chat-empty-compact" : ""}`}>
            {!compact && <span className="orb orb-lg" aria-hidden="true" />}
            <h2>{compact ? "Have a doubt about this lesson?" : requireDocuments ? "Ask your documents" : "What are you studying today?"}</h2>
            <p>{emptyHint ?? (requireDocuments
              ? "Pick one or more documents, then ask a question. Answers cite the exact passages they come from."
              : "Ask a question, or ask for a lesson or quiz and I will create one for you.")}</p>
            {!blocked && (
              <div className="starters">
                {(compact ? LESSON_STARTERS : requireDocuments ? DOC_STARTERS : STARTERS).map((s) => (
                  <button key={s} type="button" className="chip" onClick={() => { setText(s); inputRef.current?.focus(); }}>{s}</button>
                ))}
              </div>
            )}
          </div>
        )}
        {messages.map((m) => <Bubble key={m.id} m={m} fresh={m.id === freshId} onSave={saveSuggestion} onFollow={(q) => void send(q)} busy={busy} />)}
        {busy && (
          <div className="msg msg-ai"><span className="orb" aria-hidden="true" />
            <div className="bubble bubble-ai working" role="status"><span className="typing" aria-hidden="true"><i /><i /><i /></span>Working on your request. The steps taken appear with the answer.</div></div>
        )}
        <div ref={endRef} />
      </div>
      <ErrorBanner message={error} onRetry={lastSent ? () => void send(lastSent) : undefined} />
      {above}
      <form className="chat-form" onSubmit={submit}>
        <label htmlFor="chat-input" className="sr-only">Message</label>
        <textarea id="chat-input" ref={inputRef} className="composer" rows={1} value={text} maxLength={4000} disabled={blocked}
          placeholder={blocked ? "Select a document first" : placeholder ?? (grounded ? "Ask a doubt about your attached document" : "Type your question")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(e); } }} />
        <button type="submit" className="send" disabled={!text.trim() || blocked || busy} aria-label="Send">
          {busy ? <Spinner small /> : <Icon name="send" size={18} />}
        </button>
      </form>
      <p className="chat-hint">{text.trim() ? <><span className="tok-est" title="Rough estimate: about 4 characters per token. The real count is shown after the reply.">~{estimateTokens(text)} tokens in this message (estimate)</span> · </> : null}Enter to send, Shift+Enter for a new line. AI answers can be wrong.</p>
    </div>
  );
}

/** Rough client-side estimate (about 4 characters per token). The server reports the real number after each reply. */
const estimateTokens = (t: string) => Math.max(1, Math.ceil(t.trim().length / 4));

function Bubble({ m, fresh, onSave, onFollow, busy }: { m: ChatMessage; fresh: boolean; onSave: (id: string, s: { text: string; type: string }) => void; onFollow: (q: string) => void; busy: boolean }) {
  const [done, setDone] = useState(!fresh);
  const finish = useCallback(() => setDone(true), []);
  if (m.role === "user") return <motion.div className="msg msg-user" initial={{ opacity: 0, x: 24, scale: 0.97 }} animate={{ opacity: 1, x: 0, scale: 1 }} transition={SPRING}><div className="bubble bubble-user">{m.content}</div></motion.div>;
  const meta = m.meta ?? {};
  const g = meta.grounding_status;
  if (meta.status === "refused") return (
    <motion.div className="msg msg-ai" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={SPRING}><span className="orb orb-warn" aria-hidden="true" />
      <div className="bubble bubble-ai bubble-warn" role="alert"><Icon name="alert" size={18} /><div><strong>Outside what Lumina can help with</strong><p>{m.content}</p><small>{(meta.stats?.tokens ?? 0) > 0 ? `${meta.stats!.tokens.toLocaleString("en-IN")} tokens used to check this request.` : "No tokens were used."}</small></div></div>
    </motion.div>
  );
  return (
    <motion.div className="msg msg-ai" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={SPRING}><span className="orb" aria-hidden="true" />
    <div className="bubble bubble-ai">
      {g === "grounded" && <Badge tone="mark">Grounded in your documents</Badge>}
      {g === "insufficient_evidence" && <Badge tone="warn">Not enough evidence in your documents</Badge>}
      <div className="answer">{fresh && !done ? <WordReveal text={m.content} onDone={finish} /> : m.content}</div>
      {done && (
        <motion.div className="extras" initial={fresh ? { opacity: 0, y: 8 } : false} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
      {meta.citations && <Citations citations={meta.citations} />}
      {meta.limitations?.map((l, i) => <p key={i} className="limit">{l}</p>)}
      {meta.flags?.includes("prompt_injection_suspected") && <p className="limit">Your message looked like an attempt to change my instructions, so I treated it as plain text.</p>}
      {meta.quiz_id && <Link className="btn btn-secondary" to={`/quizzes?open=${meta.quiz_id}`}>Open the quiz</Link>}
      {meta.lesson_id && <Link className="btn btn-secondary" to={`/lessons?open=${meta.lesson_id}`}>Open the lesson</Link>}
      {m.suggestion && (
        <div className="suggest">
          <span>Save to memory: “{m.suggestion.text}”?</span>
          <Button variant="secondary" onClick={() => onSave(m.id, m.suggestion!)}>Save</Button>
          <Button variant="ghost" onClick={() => onSave(m.id, { text: "", type: "" })} aria-label="Dismiss suggestion">Dismiss</Button>
        </div>
      )}
      {!!meta.follow_ups?.length && (
        <div className="follow">{meta.follow_ups.map((f) => <button key={f} className="chip" disabled={busy} onClick={() => onFollow(f)}>{f}</button>)}</div>
      )}
      {typeof meta.stats?.tokens === "number" && <p className="tok-used">{meta.stats.tokens.toLocaleString("en-IN")} tokens used for this reply</p>}
      <StepList steps={meta.steps} />
        </motion.div>
      )}
    </div></motion.div>
  );
}
