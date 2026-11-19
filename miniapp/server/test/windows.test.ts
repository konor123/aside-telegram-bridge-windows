/**
 * Windows paths and the Windows CLI location.
 *
 * Everything here takes the platform as an argument or feeds Windows-shaped
 * strings into platform-independent code, so it runs (and must pass) on
 * every OS -- the Windows install is checked from any machine.
 */
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  defaultAsideCliPath,
  expandHome,
  windowsAsideCliCandidates,
} from '../src/config.js';
import { isWithin } from '../src/localfiles.js';
import { labelFor } from '../src/thread.js';
import { splitAttachmentHeader } from '../src/uploads.js';

const HOME = 'C:\\Users\\me';

describe('the Windows aside CLI location', () => {
  it('lists the installer locations in the daemon’s own order', () => {
    expect(windowsAsideCliCandidates({ LOCALAPPDATA: 'D:\\Local' }, HOME)).toEqual([
      'D:\\Local\\Aside\\CLI\\current\\aside.exe',
      'C:\\Users\\me\\.local\\bin\\aside.exe',
      'C:\\Users\\me\\.aside\\bin\\aside.exe',
    ]);
  });

  it('puts an ASIDE_CLI_INSTALL_DIR override first', () => {
    const got = windowsAsideCliCandidates(
      { LOCALAPPDATA: 'D:\\Local', ASIDE_CLI_INSTALL_DIR: 'E:\\Tools\\Aside' },
      HOME,
    );
    expect(got[0]).toBe('E:\\Tools\\Aside\\current\\aside.exe');
    expect(got).toHaveLength(4);
  });

  it('derives LOCALAPPDATA from the home directory when it is unset', () => {
    expect(windowsAsideCliCandidates({}, HOME)[0]).toBe(
      'C:\\Users\\me\\AppData\\Local\\Aside\\CLI\\current\\aside.exe',
    );
  });

  it('picks the first candidate that exists', () => {
    const present = 'C:\\Users\\me\\.aside\\bin\\aside.exe';
    expect(
      defaultAsideCliPath('win32', (p) => p === present, { LOCALAPPDATA: 'D:\\L' }, HOME),
    ).toBe(present);
  });

  it('falls back to a PATH lookup when nothing is installed where expected', () => {
    expect(defaultAsideCliPath('win32', () => false, {}, HOME)).toBe('aside');
  });

  it('leaves macOS and Linux exactly as they were', () => {
    expect(defaultAsideCliPath('darwin')).toBe(
      '~/.aside/cli/Aside CLI.app/Contents/MacOS/aside',
    );
    expect(defaultAsideCliPath('linux')).toBe('aside');
  });

  it('expands a backslash home prefix too', () => {
    expect(expandHome('~\\bridge\\config.json')).toBe(
      path.join(expandHome('~'), 'bridge\\config.json'),
    );
  });
});

describe('isWithin', () => {
  it('compares Windows paths without regard to case', () => {
    expect(isWithin('c:\\users\\me\\.aside\\shot.png', 'C:\\Users\\me\\.aside', 'win32')).toBe(true);
    expect(isWithin('C:\\Users\\Me\\.ASIDE', 'c:\\users\\me\\.aside', 'win32')).toBe(true);
  });

  it('still refuses a sibling that merely shares a prefix', () => {
    expect(isWithin('C:\\Users\\me\\.aside-evil\\x.png', 'C:\\Users\\me\\.aside', 'win32')).toBe(false);
    expect(isWithin('/home/me/.aside-evil/x', '/home/me/.aside', 'linux')).toBe(false);
  });

  it('handles a drive root', () => {
    expect(isWithin('C:\\x.png', 'C:\\', 'win32')).toBe(true);
    expect(isWithin('D:\\x.png', 'C:\\', 'win32')).toBe(false);
  });

  it('stays case-sensitive on POSIX', () => {
    expect(isWithin('/Home/me/x', '/home/me', 'linux')).toBe(false);
    expect(isWithin('/home/me/x', '/home/me', 'darwin')).toBe(true);
  });
});

describe('Windows paths in transcripts', () => {
  it('names a written file by its basename, not the whole path', () => {
    expect(
      labelFor('write_file', { file_path: 'C:\\Users\\me\\Desktop\\notes.md' }, undefined),
    ).toBe('Wrote notes.md');
    expect(labelFor('read_file', { path: 'C:/Users/me/report.txt' }, undefined)).toBe(
      'Read report.txt',
    );
  });

  it('strips an attachment header that lists Windows paths', () => {
    const { text, files } = splitAttachmentHeader(
      '[user sent 2 files from their phone, saved to: ' +
        'C:\\Users\\me\\uploads\\0123456789ab-photo.png, ' +
        'C:\\Users\\me\\uploads\\ba9876543210-doc.pdf]\nlook at these',
    );
    expect(files).toEqual([{ name: 'photo.png' }, { name: 'doc.pdf' }]);
    expect(text).toBe('look at these');
  });
});
