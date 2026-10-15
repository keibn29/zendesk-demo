import { describe, expect, it } from 'vitest';
import {
  buildSlackDeepLink,
  buildSlackWebLink,
  formatEpochSecondsUtc,
  getReactionConfig,
  parseSlackExternalId
} from './slack';

describe('parseSlackExternalId', () => {
  it('handles empty values', () => {
    for (const v of [null, undefined, '', '   ']) expect(parseSlackExternalId(v).type).toBe('empty');
  });

  it('parses valid ids', () => {
    expect(parseSlackExternalId('slack:T01234567:C09876543:1672531199.000200')).toMatchObject({
      type: 'valid', teamId: 'T01234567', channelId: 'C09876543', messageTs: '1672531199.000200'
    });
    expect(parseSlackExternalId('slack:E12345:C999:1700000000').type).toBe('valid');
  });

  it('flags malformed ids', () => {
    for (const v of ['jira:PROJ-1234', 'slack:T1:C1', 'slack:T1:C1:123.4:extra', 'slack::C1:123.4',
      'slack:T1<script>:C1:123.4', 'slack:T1:C1:not_a_ts']) {
      expect(parseSlackExternalId(v).type).toBe('malformed');
    }
    expect(parseSlackExternalId('jira:PROJ-1234')).toMatchObject({ raw: 'jira:PROJ-1234' });
  });
});

describe('links', () => {
  it('builds web and deep links', () => {
    expect(buildSlackWebLink('C09876543', '1672531199.000200')).toBe('https://slack.com/archives/C09876543/p1672531199000200');
    expect(buildSlackDeepLink('T01234567', 'C09876543', '1672531199.000200'))
      .toBe('slack://channel?team=T01234567&id=C09876543&message=1672531199.000200');
  });
});

describe('getReactionConfig', () => {
  it('maps statuses', () => {
    expect(getReactionConfig('new')).toMatchObject({ name: 'eyes', emoji: '👀' });
    expect(getReactionConfig('open')).toMatchObject({ name: 'eyes' });
    expect(getReactionConfig('pending')).toMatchObject({ name: 'hourglass_flowing_sand', emoji: '⏳' });
    expect(getReactionConfig('hold')).toMatchObject({ name: 'hourglass_flowing_sand' });
    expect(getReactionConfig('solved')).toMatchObject({ name: 'white_check_mark', emoji: '✅' });
    expect(getReactionConfig('closed')).toMatchObject({ name: 'white_check_mark' });
    expect(getReactionConfig('custom_status')).toMatchObject({ name: 'none', emoji: '—' });
  });
});

describe('formatEpochSecondsUtc', () => {
  it('formats and rejects invalid input', () => {
    const f = formatEpochSecondsUtc('1672531199.000200');
    expect(f.endsWith('UTC')).toBe(true);
    expect(formatEpochSecondsUtc('invalid')).toBe('');
    expect(formatEpochSecondsUtc('0')).toBe('');
  });
});
