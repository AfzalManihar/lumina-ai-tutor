export interface UserSettings { memory_enabled?: boolean; learning_topic?: string }
export interface User { id: string; email: string; name: string; role: "student" | "admin"; created_at: string; settings: UserSettings }
export interface TokenOut { access_token: string; expires_in: number; user: User }

export interface Citation {
  cite_id: string; chunk_id: string; document_id: string; filename: string;
  page: number | null; section: string | null; passage: string; score: number;
}
export interface Step { name: string; status: string; [k: string]: unknown }
export type Grounding = "grounded" | "insufficient_evidence" | "not_requested";
export interface MemorySuggestion { text: string; type: string }
export interface ChatMeta {
  intent?: string; grounding_status?: Grounding; citations?: Citation[]; limitations?: string[];
  flags?: string[]; follow_ups?: string[]; quiz_id?: string | null; lesson_id?: string | null;
  status?: string; steps?: Step[]; stats?: Record<string, number>;
}
export interface ChatResult extends ChatMeta {
  answer: string; conversation_id: string; message_id: string; clarification: boolean;
  memory_suggestion: MemorySuggestion | null; stats: Record<string, number>;
}
export interface ChatMessage { id: string; role: "user" | "assistant"; content: string; created_at?: string; meta?: ChatMeta; suggestion?: MemorySuggestion | null }
export interface Conversation { id: string; title: string; topic: string | null; message_count: number; document_ids?: string[]; created_at: string; updated_at: string }
export interface ConversationDetail extends Conversation { messages: ChatMessage[] }

export interface Doc { id: string; filename: string; kind: string; size_bytes: number; status: string; pages: number | null; chunk_count: number; error: string | null; embedding_provider: string | null; created_at: string }
export interface Page<T> { items: T[]; total: number }

export type Difficulty = "beginner" | "intermediate" | "advanced";
export interface LessonContent { title: string; topic: string; difficulty: Difficulty; objectives: string[]; sections: { heading: string; content: string }[]; key_takeaways: string[]; practice_questions: string[]; citations: string[] }
export interface Lesson { id: string; topic: string; difficulty: Difficulty; language: string | null; grounded: boolean; created_at: string; lesson: LessonContent; sources: { cite_id: string; filename: string; page: number | null; passage: string; document_id: string }[] }
export interface LessonSummary { id: string; topic: string; difficulty: Difficulty; title: string; grounded: boolean; created_at: string }

export interface QuizQuestion { prompt: string; options: string[]; cite_ids: string[]; correct_index?: number; explanation?: string }
export interface Quiz { id: string; topic: string; difficulty: Difficulty; created_at: string; grounded: boolean; attempts: number; questions: QuizQuestion[]; last_attempt?: { score: number; total: number; percent: number; created_at: string } }
export interface QuizSummary { id: string; topic: string; difficulty: Difficulty; created_at: string; grounded: boolean; attempts: number; question_count: number; last_attempt?: Quiz["last_attempt"] }
export interface QuizResult { score: number; total: number; percent: number; results: { index: number; given: number | null; correct: boolean; correct_index: number; explanation: string }[] }
export interface AnswerEvaluation { score: number; feedback: string; missing_points: string[]; verdict: "correct" | "partially_correct" | "incorrect" }

export interface TopicProgress { topic: string; lessons: number; quiz_attempts: number; best_percent: number | null; last_activity: string }
export interface Progress { topics: TopicProgress[]; quizzes_taken: number; average_quiz_percent: number | null; lessons_completed: number; recent_activity: { type: string; topic: string | null; percent: number | null; created_at: string }[] }

export interface Memory { id: string; text: string; type: string; source: string; created_at: string; updated_at: string }
export interface MemoryList extends Page<Memory> { enabled: boolean }

export interface Plan { id: string; name: string; price_inr: number; monthly_token_limit: number; max_documents: number; features: string[] }
export interface Subscription { plan_id: string; status: string; expires_at: string | null; cancel_at_period_end: boolean }
export interface Payment { order_id: string; plan_id: string; amount: number; currency: string; status: string; created_at: string }
export interface Order { order_id: string; amount: number; currency: string; key_id: string; plan_id: string }
export interface UsageCurrent { period: string; tokens_used: number; limit: number; remaining: number; requests: number; by_feature: Record<string, number>; plan_id: string }
export interface Usage { current: UsageCurrent; history: { period: string; tokens_used: number; requests: number }[] }

export interface Dashboard {
  recent_conversations: Conversation[]; progress: Progress;
  documents: { total: number; recent: { id: string; filename: string; status: string; created_at: string }[] };
  recent_quiz_results: { topic: string; percent: number; score: number; total: number; created_at: string }[];
  subscription: Subscription; usage: UsageCurrent;
}

export interface Diagnostics {
  database: string; counts: Record<string, number>;
  integrations: Record<string, Record<string, unknown>>;
  limits: Record<string, number>;
  tools: { name: string; description: string; permission: string; read_only: boolean; requires_approval: boolean; timeout_s: number }[];
}

export interface TraceRow { trace_id: string; workflow: string; latency_ms: number; tokens: number; retries: number; error_category: string | null; created_at: string; spans: { name: string; kind: string; status: string; duration_ms: number; result?: string }[] }
export interface Traces { items: TraceRow[]; guardrails: { refused_off_topic: number; refused_injection: number } }
