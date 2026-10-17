/** Browser boundary for administrator-managed moderator access.
 *
 * A team bearer token lives only in caller-provided React memory. This module
 * never writes cookies, local/session storage, history, URLs or diagnostics.
 * Credential-issuing responses are no-store and returned to the caller once.
 */
export type TeamRole = 'admin' | 'moderator';
export type MemberStatus = 'active' | 'disabled';
export type TeamMember = {
  member_id: string;
  role: TeamRole;
  status: MemberStatus;
  credential_source: 'managed' | 'configuration';
};
export type TeamIdentity = { member_id: string; role: TeamRole };
export type TokenIssued = {
  member_id: string;
  role: 'moderator';
  status: 'active';
  access_token: string;
};
export type MemberDisabled = {
  member_id: string;
  role: 'moderator';
  status: 'disabled';
};

export class TeamApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = 'TeamApiError';
  }
}

async function authorized<T>(
  path: string,
  key: string,
  method: 'GET' | 'POST' = 'GET',
  payload?: object,
): Promise<T> {
  if (!key) throw new TeamApiError(401, 'Требуется ключ администратора');
  const response = await fetch(path, {
    method,
    cache: 'no-store',
    credentials: 'omit',
    headers: {
      'X-API-Key': key,
      ...(payload ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(payload ? { body: JSON.stringify(payload) } : {}),
  });
  if (!response.ok) {
    let message = 'Запрос не выполнен';
    try {
      const details = await response.json() as { detail?: string };
      if (typeof details.detail === 'string') message = details.detail.slice(0, 250);
    } catch {
      // Do not echo arbitrary response bodies or any submitted API token.
    }
    throw new TeamApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export const teamApi = {
  me: (key: string) => authorized<TeamIdentity>('/v1/me', key),
  members: async (key: string) =>
    (await authorized<{ members: TeamMember[] }>('/v1/admin/members', key)).members,
  invite: (key: string, memberId: string) =>
    authorized<TokenIssued>('/v1/admin/members', key, 'POST', { member_id: memberId }),
  rotate: (key: string, memberId: string) =>
    authorized<TokenIssued>(
      '/v1/admin/members/' + encodeURIComponent(memberId) + '/rotate', key, 'POST',
    ),
  disable: (key: string, memberId: string) =>
    authorized<MemberDisabled>(
      '/v1/admin/members/' + encodeURIComponent(memberId) + '/disable', key, 'POST',
    ),
};
