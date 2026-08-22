import Link from 'next/link';

export default function NotFound() {
  return (
    <main className="grid min-h-screen place-items-center px-6 text-center">
      <section>
        <p className="text-xs font-bold uppercase tracking-[0.14em] text-[var(--accent-dark)]">
          404 · Lost frame
        </p>
        <h1 className="mt-4 text-5xl font-semibold tracking-[-0.06em]">没有找到这个页面</h1>
        <p className="mt-4 text-[var(--muted)]">它可能还没有被开发，或者地址已经发生变化。</p>
        <Link
          href="/photos"
          className="mt-8 inline-block rounded-xl bg-[var(--ink)] px-5 py-3 text-sm font-bold text-white"
        >
          返回时间线
        </Link>
      </section>
    </main>
  );
}
