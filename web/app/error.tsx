'use client';

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="grid min-h-screen place-items-center px-6">
      <section className="w-full max-w-lg rounded-3xl border border-[var(--line)] bg-white p-8 shadow-[var(--shadow)]">
        <p className="text-xs font-bold uppercase tracking-[0.12em] text-[var(--accent-dark)]">
          Something went wrong
        </p>
        <h1 className="mt-4 text-3xl font-semibold tracking-tight">页面暂时无法加载</h1>
        <p className="mt-3 text-sm leading-7 text-[var(--muted)]">
          {error.message || '出现了未预期的错误，请稍后重试。'}
        </p>
        <button
          type="button"
          className="mt-7 rounded-xl bg-[var(--ink)] px-5 py-3 text-sm font-bold text-white"
          onClick={reset}
        >
          重新加载
        </button>
      </section>
    </main>
  );
}
