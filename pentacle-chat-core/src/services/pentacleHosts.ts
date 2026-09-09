export function normalizePentacleHost(host: string): string {
  return String(host || '').trim().toLowerCase();
}
