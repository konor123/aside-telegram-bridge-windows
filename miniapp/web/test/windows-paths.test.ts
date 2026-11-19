/**
 * Image references an agent on Windows writes into an answer.
 *
 * `C:` looks like a one-letter URL scheme, so a drive-letter path used to
 * be classified as a URL and rendered as a broken image instead of being
 * routed through the authenticated local-file endpoint.
 */
import { describe, expect, it } from 'vitest';
import { localImagePath } from '../src/utils/images';

describe('localImagePath on Windows paths', () => {
  it('accepts a drive-letter path with either separator', () => {
    expect(localImagePath('C:\\Users\\me\\shot.png')).toBe('C:\\Users\\me\\shot.png');
    expect(localImagePath('c:/Users/me/shot.png')).toBe('c:/Users/me/shot.png');
  });

  it('unwraps a Windows file:// URL to the drive path', () => {
    expect(localImagePath('file:///C:/Users/me/a%20b.png')).toBe('C:/Users/me/a b.png');
    expect(localImagePath('file://localhost/D:/x.png')).toBe('D:/x.png');
  });

  it('still treats real schemes and drive-relative names as not local', () => {
    expect(localImagePath('https://example.com/a.png')).toBeNull();
    expect(localImagePath('C:shot.png')).toBeNull();
    expect(localImagePath('C:\\a\0b.png')).toBeNull();
  });
});
