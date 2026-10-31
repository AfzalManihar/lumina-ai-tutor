import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, errorText } from "../api/client";
import type { Difficulty, Doc, Lesson, LessonSummary, Page } from "../api/types";
import { DocPicker } from "../components/DocPicker";
import { Badge, Button, Card, EmptyState, ErrorBanner, Field, Loading, PageHeader, Select, TextArea, TextInput } from "../components/ui";
import { useLoad } from "../hooks";
import { fmtDate } from "../lib";

export function LessonView({ lesson }: { lesson: Lesson }) {
  const l = lesson.lesson;
  return (
    <article className="lesson">
      <h2>{l.title}</h2>
      <p>
        <Badge>{l.difficulty}</Badge>{" "}
        {lesson.grounded ? <Badge tone="mark">Built from your documents</Badge> : <Badge tone="warn">General knowledge, no sources</Badge>}
      </p>
      {lesson.sources.length > 0 && (
        <p><Link className="btn btn-secondary" to={`/tutor?docs=${[...new Set(lesson.sources.map((s) => s.document_id))].join(",")}`}>
          Ask this lesson: chat about its source PDF</Link></p>
      )}
      <h3>Objectives</h3>
      <ul>{l.objectives.map((o, i) => <li key={i}>{o}</li>)}</ul>
      {l.sections.map((s, i) => (<section key={i}><h3>{s.heading}</h3><p className="read">{s.content}</p></section>))}
      <h3>Key takeaways</h3>
      <ul>{l.key_takeaways.map((o, i) => <li key={i}>{o}</li>)}</ul>
      {l.practice_questions.length > 0 && (<><h3>Practice questions</h3><ol>{l.practice_questions.map((o, i) => <li key={i}>{o}</li>)}</ol></>)}
      {lesson.sources.length > 0 && (
        <details>
          <summary>Sources used ({lesson.sources.length})</summary>
          <ul className="cites">{lesson.sources.map((s) => (
            <li key={s.cite_id}><span className="cite-id">{s.cite_id}</span> {s.filename}{s.page ? `, page ${s.page}` : ""}
              <blockquote className="passage"><p>{s.passage}</p></blockquote></li>))}</ul>
        </details>
      )}
    </article>
  );
}

export function Lessons() {
  const [params, setParams] = useSearchParams();
  const openId = params.get("open");
  const list = useLoad(() => api.get<Page<LessonSummary>>("/lessons", { limit: 30 }));
  const docs = useLoad(() => api.get<Page<Doc>>("/documents", { limit: 100 }));
  const current = useLoad(() => (openId ? api.get<Lesson>(`/lessons/${openId}`) : Promise.resolve(null)), [openId]);
  const [topic, setTopic] = useState(""); const [difficulty, setDifficulty] = useState<Difficulty>("beginner");
  const [objective, setObjective] = useState(""); const [language, setLanguage] = useState("English");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault(); setErr(null); setBusy(true);
    try {
      const l = await api.post<Lesson>("/lessons", {
        topic, difficulty, language, ...(objective.trim() ? { objective: objective.trim() } : {}),
        ...(selected.length ? { document_ids: selected } : {}),
      });
      setParams({ open: l.id }); await list.reload();
    } catch (x) { setErr(errorText(x)); } finally { setBusy(false); }
  }
  return (
    <>
      <PageHeader title="Lessons">Generate a structured lesson on any topic. Select documents to build it from your own material; without documents it uses general knowledge and says so.</PageHeader>
      <div className="split">
        <div>
          <Card title="New lesson">
            <form onSubmit={submit}>
              <ErrorBanner message={err} />
              <Field label="Topic">{(id) => <TextInput id={id} required minLength={2} maxLength={120} value={topic} onChange={(e) => setTopic(e.target.value)} />}</Field>
              <Field label="Difficulty">{(id) => (
                <Select id={id} value={difficulty} onChange={(e) => setDifficulty(e.target.value as Difficulty)}>
                  <option value="beginner">Beginner</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option>
                </Select>)}</Field>
              <Field label="Goal (optional)">{(id) => <TextArea id={id} rows={2} maxLength={300} value={objective} onChange={(e) => setObjective(e.target.value)} />}</Field>
              <Field label="Language">{(id) => <TextInput id={id} maxLength={40} value={language} onChange={(e) => setLanguage(e.target.value)} />}</Field>
              <DocPicker docs={docs.data?.items ?? []} selected={selected} onChange={setSelected} />
              <Button type="submit" loading={busy} disabled={topic.trim().length < 2}>Generate lesson</Button>
            </form>
          </Card>
          <Card title="Your lessons">
            {list.loading && !list.data && <Loading label="Loading lessons" />}
            <ErrorBanner message={list.error} onRetry={list.reload} />
            {list.data?.items.length === 0 && <EmptyState title="No lessons yet">Generate one above.</EmptyState>}
            <ul className="plain">{list.data?.items.map((l) => (
              <li key={l.id}><button className="link" onClick={() => setParams({ open: l.id })}>{l.title}</button>
                <small className="muted"> {l.difficulty} · {fmtDate(l.created_at)}</small></li>))}</ul>
          </Card>
        </div>
        <Card>
          {busy && <Loading label="Generating your lesson" />}
          {!busy && openId && current.loading && <Loading label="Loading lesson" />}
          <ErrorBanner message={openId ? current.error : null} />
          {!busy && current.data && <LessonView lesson={current.data} />}
          {!busy && !openId && <EmptyState title="No lesson open">Generate a lesson or pick one from your list.</EmptyState>}
        </Card>
      </div>
    </>
  );
}
