export interface ReactionConfig {
  name: string;
  emoji: string;
  badgeClass: string;
  badgeLabel: string;
}

export const STATUS_REACTION_MAP: Record<string, ReactionConfig> = {
  new: { name: 'eyes', emoji: '👀', badgeClass: 'badge--new', badgeLabel: 'New' },
  open: { name: 'eyes', emoji: '👀', badgeClass: 'badge--open', badgeLabel: 'Open' },
  pending: { name: 'hourglass_flowing_sand', emoji: '⏳', badgeClass: 'badge--pending', badgeLabel: 'Pending' },
  hold: { name: 'hourglass_flowing_sand', emoji: '⏳', badgeClass: 'badge--hold', badgeLabel: 'Hold' },
  solved: { name: 'white_check_mark', emoji: '✅', badgeClass: 'badge--solved', badgeLabel: 'Solved' },
  closed: { name: 'white_check_mark', emoji: '✅', badgeClass: 'badge--closed', badgeLabel: 'Closed' }
};

export type ParsedExternalId =
  | { type: 'empty' }
  | { type: 'malformed'; raw: string; reason: string }
  | { type: 'valid'; teamId: string; channelId: string; messageTs: string; raw: string };

const ID_PATTERN = /^[A-Za-z0-9_-]+$/;
const TS_PATTERN = /^[0-9]+(?:\.[0-9]+)?$/;

/** Parses slack:{team_id}:{channel_id}:{message_ts}. */
export function parseSlackExternalId(externalId: string | null | undefined): ParsedExternalId {
  if (externalId == null || typeof externalId !== 'string' || externalId.trim() === '') {
    return { type: 'empty' };
  }
  const raw = externalId.trim();
  const bad = (reason: string): ParsedExternalId => ({ type: 'malformed', raw, reason });

  if (!raw.startsWith('slack:')) return bad('Prefix must be "slack:"');
  const parts = raw.split(':');
  if (parts.length !== 4) return bad('Expected 4 colon-delimited components');

  const teamId = parts[1].trim();
  const channelId = parts[2].trim();
  const messageTs = parts[3].trim();
  if (!teamId || !channelId || !messageTs) return bad('All components must be non-empty');
  if (!ID_PATTERN.test(teamId)) return bad('Invalid team_id format');
  if (!ID_PATTERN.test(channelId)) return bad('Invalid channel_id format');
  if (!TS_PATTERN.test(messageTs)) return bad('Invalid message_ts format');

  return { type: 'valid', teamId, channelId, messageTs, raw };
}

export function buildSlackWebLink(channelId: string, messageTs: string): string {
  const cleanTs = String(messageTs).replace('.', '');
  return 'https://slack.com/archives/' + encodeURIComponent(channelId) + '/p' + encodeURIComponent(cleanTs);
}

export function buildSlackDeepLink(teamId: string, channelId: string, messageTs: string): string {
  return 'slack://channel?team=' + encodeURIComponent(teamId) +
    '&id=' + encodeURIComponent(channelId) +
    '&message=' + encodeURIComponent(messageTs);
}

export function getReactionConfig(status: string | null | undefined): ReactionConfig {
  const normalized = (status || '').toString().trim().toLowerCase();
  if (Object.prototype.hasOwnProperty.call(STATUS_REACTION_MAP, normalized)) {
    return STATUS_REACTION_MAP[normalized];
  }
  return {
    name: 'none',
    emoji: '—',
    badgeClass: '',
    badgeLabel: normalized ? normalized.charAt(0).toUpperCase() + normalized.slice(1) : 'None'
  };
}

export function formatEpochSecondsUtc(messageTs: string): string {
  const num = parseFloat(messageTs);
  if (isNaN(num) || num <= 0) return '';
  const date = new Date(num * 1000);
  if (isNaN(date.getTime())) return '';
  return date.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
}
