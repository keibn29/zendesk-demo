import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const zafDir = resolve(__dirname, '../../zaf');
const readJson = (p: string) => JSON.parse(readFileSync(resolve(zafDir, p), 'utf8'));

describe('zaf package', () => {
  it('has a valid manifest', () => {
    const m = readJson('manifest.json');
    expect(m.private).toBe(true);
    expect(m.frameworkVersion).toBe('2.0');
    expect(m.defaultLocale).toBe('en');
    expect(m.name).toBeTruthy();
    expect(m.author.name && m.author.email).toBeTruthy();
    const sidebar = m.location.support.ticket_sidebar;
    expect(sidebar.url).toMatch(/^https:\/\/[^/]+\/$/);
    expect(sidebar.flexible).toBe(true);
  });

  it('has logos and translations', () => {
    expect(existsSync(resolve(zafDir, 'assets/logo.png'))).toBe(true);
    expect(existsSync(resolve(zafDir, 'assets/logo-small.png'))).toBe(true);
    const t = readJson('translations/en.json');
    for (const k of ['name', 'short_description', 'long_description', 'installation_instructions']) {
      expect(t.app[k]).toBeTruthy();
    }
  });
});
