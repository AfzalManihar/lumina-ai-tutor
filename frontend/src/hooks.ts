import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "./api/client";

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    setLoading(true); setError(null);
    try { setData(await fnRef.current()); } catch (e) { setError(errorText(e)); } finally { setLoading(false); }
  }, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void reload(); }, deps);
  return { data, error, loading, reload, setData };
}

/** A string kept in localStorage (e.g. the last open conversation) so leaving a page and coming back resumes it.
 *  Storage can be blocked or empty, so every access is guarded and the page works without it. */
export function useStoredValue(key: string): [string | null, (v: string | null) => void] {
  const read = () => { try { return window.localStorage.getItem(key); } catch { return null; } };
  const [v, setV] = useState<string | null>(read);
  const set = useCallback((next: string | null) => {
    setV(next);
    try { if (next) window.localStorage.setItem(key, next); else window.localStorage.removeItem(key); } catch { /* ignore */ }
  }, [key]);
  return [v, set];
}
