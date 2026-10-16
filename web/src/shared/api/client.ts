/** Browser network boundary. API keys are not stored or shipped in frontend code. */
export type Health = { status: 'ok' };

export async function getHealth(): Promise<Health> {
  const response = await fetch('/healthz', { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`Health request returned ${response.status}`);
  return response.json() as Promise<Health>;
}
