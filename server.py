#!/usr/bin/env python3
"""Let It Download - local yt-dlp GUI server (stdlib only).

Run:  pythonw server.py   (or run.bat)   ->  http://127.0.0.1:8765
Cookie file is re-read on every check, so just overwrite cookie.txt with a fresh export.
"""
import os, sys, json, re, time, threading, subprocess, shutil, webbrowser, traceback
import urllib.request, urllib.parse, http.cookiejar, html
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

FROZEN = getattr(sys, 'frozen', False)
APP = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))   # bundled files (index.html)
DATA = Path(os.environ['LID_DATA']) if os.environ.get('LID_DATA') else (Path(sys.executable).parent if FROZEN else APP) / 'data'
DATA.mkdir(exist_ok=True)
PORT = int(os.environ.get('LID_PORT', 8765))
NOWIN = 0x08000000 if os.name == 'nt' else 0
LOCK = threading.RLock()

PALETTE = ['#ff3d9a', '#1fd1c8', '#ff8a1f', '#a66bff', '#ffd23f', '#5ee86b']
DEFAULTS = {
    'cookie': str(Path.home() / 'Downloads' / 'cookie.txt'),
    'warn_days': 7,
    'concurrency': 2,
    'quality': '1080',
    'proxy': '',
    'base': str(Path.home() / 'Downloads' / 'LetItDL'),
    'list_limit': 400,
    'channels': [],          # add yours with the + tab
    'premiere_cmd': '',      # optional editor hook, see README (Compilations)
    'premiere_status_cmd': '',
}


# ---------- persistence ----------
def jload(name, default):
    try:
        return json.loads((DATA / name).read_text('utf-8'))
    except Exception:
        return default


def jsave(name, obj):
    tmp = DATA / (name + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), 'utf-8')
    os.replace(tmp, DATA / name)


SET = {**DEFAULTS, **jload('settings.json', {})}
JOBS = jload('jobs.json', [])
RT = {}          # job id -> runtime {'proc','stop'}
CH_STATE = {}    # channel key -> {'refreshing','error'}
COOKIE_FLAG = {'rejected': False, 'mtime': 0, 'test': None}
for j in JOBS:   # interrupted by restart -> paused, ready to resume (.part files kept)
    if j['status'] in ('running', 'queued'):
        j['status'] = 'paused'
        j['note'] = 'interrupted - press resume'


def save_set():
    with LOCK:
        jsave('settings.json', SET)


def save_jobs():
    with LOCK:
        jsave('jobs.json', JOBS)


# ---------- cookie ----------
KEYS = {'LOGIN_INFO', 'SID', '__Secure-1PSID', '__Secure-3PSID', 'SAPISID', '__Secure-1PAPISID',
        '__Secure-3PAPISID', 'HSID', 'SSID', 'APISID'}


def cookie_status():
    p = SET['cookie']
    st = {'path': p, 'exists': False, 'level': 'missing', 'days': None, 'cookies': [], 'msg': 'cookie.txt not found',
          'mtime': 0, 'rejected': COOKIE_FLAG['rejected'], 'test': COOKIE_FLAG['test'], 'warn_days': SET['warn_days']}
    if not os.path.isfile(p):
        return st
    st['exists'] = True
    st['mtime'] = os.path.getmtime(p)
    if st['mtime'] != COOKIE_FLAG['mtime']:       # new file dropped in -> forget old verdicts
        COOKIE_FLAG.update(mtime=st['mtime'], rejected=False, test=None)
        st['rejected'], st['test'] = False, None
    now = time.time()
    found = {}
    try:
        for line in open(p, encoding='utf-8', errors='replace'):
            line = line.rstrip('\r\n')
            if line.startswith('#HttpOnly_'):
                line = line[10:]
            elif line.startswith('#') or not line.strip():
                continue
            f = line.split('\t')
            if len(f) < 7 or not re.search(r'(youtube|google)\.com$', f[0].lstrip('.')):
                continue
            try:
                exp = int(float(f[4]))
            except ValueError:
                continue
            if f[5] in KEYS and exp > 0:
                found[f[5]] = max(found.get(f[5], 0), exp)
    except Exception as e:
        st.update(level='bad', msg=f'cannot read cookie file: {e}')
        return st
    if not found:
        st.update(level='bad', msg='no YouTube login cookies in file (export while signed in)')
        return st
    st['cookies'] = sorted(({'name': k, 'exp': v, 'days': round((v - now) / 86400, 1)} for k, v in found.items()), key=lambda c: c['exp'])
    d = st['cookies'][0]['days']
    st['days'] = d
    if d <= 0:
        st.update(level='expired', msg='cookie EXPIRED - export a fresh cookie.txt')
    elif d <= SET['warn_days']:
        st.update(level='warn', msg=f'cookie expires in {d:.1f} days - get a new one soon')
    else:
        st.update(level='ok', msg=f'cookie good for {d:.0f} more days')
    if st['rejected'] and st['level'] in ('ok', 'warn'):
        st.update(level='warn', msg='YouTube rejected this cookie (rotated?) - export a fresh one')
    return st


def toast(title, msg):
    if os.name != 'nt':
        return
    ps = ("Add-Type -AssemblyName System.Windows.Forms;Add-Type -AssemblyName System.Drawing;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
          f"$n.ShowBalloonTip(15000,'{title}','{msg}','Warning');Start-Sleep 16;$n.Dispose()")
    try:
        subprocess.Popen(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps], creationflags=NOWIN, stdin=subprocess.DEVNULL)
    except Exception:
        pass


def cookie_watcher():
    last = jload('notify.json', {})
    while True:
        try:
            st = cookie_status()
            lv = st['level']
            if lv in ('warn', 'expired', 'missing', 'bad'):
                key = f"{lv}:{time.strftime('%Y-%m-%d')}"
                if last.get('k') != key:      # one desktop toast per level per day
                    last['k'] = key
                    jsave('notify.json', last)
                    toast('Let It Download - cookie', st['msg'].replace("'", ''))
        except Exception:
            pass
        time.sleep(1800)


def extract_cookies(browser):
    tmp = DATA / 'ck_tmp.txt'
    if tmp.exists():
        tmp.unlink()
    cmd = ['yt-dlp', '--encoding', 'utf-8', '--no-colors', '--cookies-from-browser', browser, '--cookies', str(tmp),
           '--skip-download', '--no-playlist', '--print', 'id', 'https://www.youtube.com/watch?v=jNQXAC9IVRw']
    if SET['proxy']:
        cmd += ['--proxy', SET['proxy']]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180,
                           creationflags=NOWIN, stdin=subprocess.DEVNULL)
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    if not tmp.exists():
        err = next((l for l in reversed((r.stderr or '').splitlines()) if 'ERROR' in l), 'yt-dlp could not read the browser cookies')
        hint = ''
        if 'Could not copy' in err or 'Permission' in err or 'DPAPI' in err or 'decrypt' in err.lower():
            hint = ' - Chrome/Edge lock their cookie file while running (and newer versions encrypt it per-app). Close ALL ' + browser + ' windows and retry, or use Firefox.'
        elif 'could not find' in err:
            hint = ' - that browser is not installed / has no profile.'
        return {'ok': False, 'error': err[:200] + hint}
    keep, n = [], 0
    for line in tmp.read_text('utf-8', errors='replace').splitlines():     # only google/youtube cookies leave the browser
        if line.startswith('# ') or not line.strip():
            keep.append(line)
            continue
        f = line.lstrip('#').replace('HttpOnly_', '', 1).split('\t')
        if len(f) >= 7 and re.search(r'(youtube|google)\.com$', f[0].lstrip('.')):
            keep.append(line)
            n += 1
    tmp.unlink()
    text = '\n'.join(keep) + '\n'
    if not any(k in text for k in ('LOGIN_INFO', '__Secure-3PSID', 'SAPISID')):
        return {'ok': False, 'error': f'{browser} is not signed in to YouTube (no login cookies found)'}
    p = SET['cookie']
    if os.path.isfile(p):
        shutil.copy2(p, p + '.bak')
    Path(p).write_text(text, encoding='utf-8', newline='\n')
    return {'ok': True, 'count': n}


def test_cookie():
    cmd = ytdlp_base() + ['--skip-download', '--print', '%(title)s', 'https://www.youtube.com/watch?v=jNQXAC9IVRw']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90, creationflags=NOWIN, stdin=subprocess.DEVNULL)
    except Exception as e:
        return {'ok': False, 'msg': str(e), 'at': time.time()}
    err = (r.stderr or '')
    if 'no longer valid' in err or 'Sign in to confirm' in err:
        COOKIE_FLAG['rejected'] = True
        res = {'ok': False, 'msg': 'YouTube rejected the cookie (rotated or bot-check). Export a fresh cookie.txt.'}
    elif r.returncode != 0:
        res = {'ok': False, 'msg': (err.strip().splitlines() or ['yt-dlp failed'])[-1][:200]}
    else:
        COOKIE_FLAG['rejected'] = False
        res = {'ok': True, 'msg': 'cookie accepted by YouTube'}
    res['at'] = time.time()
    COOKIE_FLAG['test'] = res
    return res


# ---------- yt-dlp ----------
def ytdlp_base(playlist=False):
    a = ['yt-dlp', '--encoding', 'utf-8', '--no-colors']
    if not playlist:
        a.append('--no-playlist')
    if os.path.isfile(SET['cookie']):
        a += ['--cookies', SET['cookie']]
    if SET['proxy']:
        a += ['--proxy', SET['proxy']]
    return a


def fmt_args(q):
    if q == 'audio':
        return ['-x', '--audio-format', 'mp3', '--audio-quality', '0']
    if q == 'best':
        return ['-f', 'bv*+ba/b', '--merge-output-format', 'mp4']
    h = {'1080': 1080, '720': 720, '480': 480}.get(q, 1080)
    return ['-f', f'bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/b[vcodec^=avc1][height<={h}]/bv*[height<={h}]+ba/b[height<={h}]',
            '--merge-output-format', 'mp4']


TPL = 'download:P|%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s'


def killtree(proc):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, creationflags=NOWIN, stdin=subprocess.DEVNULL)
        else:
            proc.kill()
    except Exception:
        pass


def num(x):
    try:
        return float(x)
    except Exception:
        return None


FFPROBE = shutil.which('ffprobe') or 'ffprobe'
PROBE_CACHE = {}
BG = {'on': False}


def _pkey(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime, st.st_size)
    except OSError:
        return None


def probe(path, cached_only=False):
    key = _pkey(path)
    if not key:
        return None
    if key in PROBE_CACHE or cached_only:
        return PROBE_CACHE.get(key)
    out = {'v': None, 'a': None, 'f': None}
    try:
        r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,duration:format=duration', '-of', 'json', path],
                           capture_output=True, text=True, timeout=60, creationflags=NOWIN, stdin=subprocess.DEVNULL)
        d = json.loads(r.stdout or '{}')
        out['f'] = num((d.get('format') or {}).get('duration'))
        for st in d.get('streams', []):
            k = {'video': 'v', 'audio': 'a'}.get(st.get('codec_type'))
            du = num(st.get('duration'))
            if k and du is not None:
                out[k] = max(out[k] or 0, du)
    except Exception:
        return None
    PROBE_CACHE[key] = out
    return out


def media_ok(path, expected=None, cached_only=False):
    """(ok, why). Video AND audio stream must reach the expected length - catches a cut-off video track under a full audio track."""
    p = probe(path, cached_only)
    if p is None:
        return (True, '') if cached_only else (False, 'unreadable file')
    vals = [x for x in (p['v'], p['a'], p['f']) if x]
    if not vals:
        return False, 'no duration'
    ref = expected or max(vals)
    tol = max(3.0, ref * 0.02)
    for k, label in (('v', 'video'), ('a', 'audio')):
        if p[k] is not None and p[k] < ref - tol:
            return False, f'{label} only {p[k]:.0f}s of {ref:.0f}s'
    return True, ''


def bg_probe(paths):
    if BG['on'] or not paths:
        return
    BG['on'] = True

    def w():
        try:
            for p in paths:
                probe(p)
        finally:
            BG['on'] = False
    threading.Thread(target=w, daemon=True).start()


def file_valid(folder, it):
    f = file_for(folder, it['title'])
    return bool(f) and media_ok(f, it.get('dur'))[0]


def stop_job(j, why):
    """pause/cancel a job whether it is running (any kind), queued or idle"""
    rt = RT.get(j['id'])
    if rt is not None:
        rt['stop'] = why
        if rt.get('proc'):
            killtree(rt['proc'])
        return
    if why == 'cancel':
        JOBS[:] = [x for x in JOBS if x['id'] != j['id']]
    else:
        j.update(status='paused', note='paused')


def apply_stop(j, stop):
    if stop == 'pause':
        j.update(status='paused', speed=None, eta=None, note='paused - resume continues the .part file')
    else:
        JOBS[:] = [x for x in JOBS if x['id'] != j['id']]
    save_jobs()


DRIVE_ID = re.compile(r'(?:drive|docs)\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^\s#]*&)?id=|document/d/)([A-Za-z0-9_-]{20,})')


def drive_opener():
    cj = http.cookiejar.MozillaCookieJar()
    if os.path.isfile(SET['cookie']):
        try:
            cj.load(SET['cookie'], ignore_discard=True, ignore_expires=True)    # lets private files open with your login
        except Exception:
            pass
    hs = [urllib.request.HTTPCookieProcessor(cj)]
    if SET['proxy']:
        hs.append(urllib.request.ProxyHandler({'http': SET['proxy'], 'https': SET['proxy']}))
    op = urllib.request.build_opener(*hs)
    op.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36')]
    return op


def cd_name(h):
    m = re.search(r"filename\*=UTF-8''([^;]+)", h or '')
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', h or '')
    return m.group(1) if m else None


def safe_name(n):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', n).strip(' .') or 'drive_file'


DRIVE_FOLDER = re.compile(r'drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{20,})')


def drive_folder_list(fid, depth=0):
    """[(file_id, name, subpath)] via the embedded folder view (works for 'anyone with the link' folders and your signed-in ones)."""
    try:
        page = drive_opener().open(f'https://drive.google.com/embeddedfolderview?id={fid}', timeout=40).read().decode('utf-8', 'replace')
    except Exception:
        return []
    out = []
    for chunk in page.split('class="flip-entry"')[1:]:
        m = re.search(r'id="entry-([\w-]+)"', chunk)
        t = re.search(r'flip-entry-title">([^<]*)<', chunk)
        href = re.search(r'href="([^"]+)"', chunk)
        if not (m and t):
            continue
        name = html.unescape(t.group(1))
        if href and '/folders/' in href.group(1):
            if depth < 3:
                out += [(a, b, os.path.join(safe_name(name), c) if c else safe_name(name)) for a, b, c in drive_folder_list(m.group(1), depth + 1)]
        else:
            out.append((m.group(1), name, ''))
    return out


def collect_drive(text, root):
    """-> {target_folder: [items]}; a 'Label: link' line puts that link's files in <root>/<Label>/"""
    groups, seen, notes = {}, set(), []
    for line in text.splitlines():
        for m in list(DRIVE_FOLDER.finditer(line)) + list(DRIVE_ID.finditer(line)):
            pre = re.sub(r'https?://(?:www.)?$', '', line[:m.start()]).strip(' :-	')
            label = safe_name(pre) if pre else ''
            base = os.path.join(root, label) if label else root
            if DRIVE_FOLDER.match(m.group(0)):
                files = drive_folder_list(m.group(1))
                if not files:
                    notes.append(f'folder {label or m.group(1)[:8]}: empty or not shared / needs sign-in')
                for fid, name, sub in files:
                    if fid not in seen:
                        seen.add(fid)
                        groups.setdefault(os.path.join(base, sub) if sub else base, []).append({'id': fid, 'title': name, 'kind': 'drive'})
            elif m.group(1) not in seen:
                seen.add(m.group(1))
                groups.setdefault(base, []).append({'id': m.group(1), 'kind': 'drive'})
    return groups, notes


def run_drive(j):
    jid = j['id']
    rt = RT[jid]
    try:
        os.makedirs(j['folder'], exist_ok=True)
        op = drive_opener()
        fid = j['vid']
        base = f'https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t'
        part = os.path.join(j['folder'], j['fname'] + '.part') if j.get('fname') else None
        have = os.path.getsize(part) if part and os.path.isfile(part) else 0

        def open_url(url):
            req = urllib.request.Request(url, headers={'Range': f'bytes={have}-'} if have else {})
            return op.open(req, timeout=40)

        r = open_url(base)
        if 'text/html' in (r.headers.get('Content-Type') or ''):       # confirm / virus-scan page: follow its form
            page = r.read().decode('utf-8', 'replace')
            m = re.search(r'<form[^>]+action="([^"]+)"', page)
            if not m:
                msg = re.sub(r'<[^>]+>', ' ', page)
                raise RuntimeError('Drive refused: ' + (' sorry, no permission / quota exceeded / sign in (new cookie?)' if 'quota' in msg.lower() or 'sign' in msg.lower() or 'access' in msg.lower() else 'got a web page, not a file'))
            q = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page))
            r = open_url(html.unescape(m.group(1)) + '?' + urllib.parse.urlencode(q))
            if 'text/html' in (r.headers.get('Content-Type') or ''):
                raise RuntimeError('Drive refused (quota exceeded or no access)')
        name = safe_name(cd_name(r.headers.get('Content-Disposition')) or j.get('fname') or fid)
        if not j.get('fname') or j['fname'] != name:
            j['fname'] = name
            part = os.path.join(j['folder'], name + '.part')
            have = os.path.getsize(part) if os.path.isfile(part) else 0
        j['title'] = name
        status = getattr(r, 'status', 200)
        cr = r.headers.get('Content-Range')
        if status == 206 and cr:
            total = int(cr.split('/')[-1])
        else:
            have = 0                      # server ignored Range -> start over
            total = int(r.headers.get('Content-Length') or 0)
        j['size'] = total or None
        final = os.path.join(j['folder'], name)
        t0, last, got = time.time(), time.time(), 0
        with open(part, 'ab' if have else 'wb') as f:
            while True:
                if rt.get('stop'):
                    break
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.5:
                    last = now
                    cur = have + got
                    sp = got / max(now - t0, 0.1)
                    j.update(pct=round(cur / total * 100, 1) if total else 0, speed=sp,
                             eta=(total - cur) / sp if total and sp else None)
        r.close()
        with LOCK:
            if rt.get('stop'):
                apply_stop(j, rt['stop'])
                return
            cur = have + got
            if total and cur < total:
                raise RuntimeError(f'connection dropped at {cur}/{total} - press resume')
            if os.path.exists(final):
                os.remove(final)
            os.replace(part, final)
            j.update(status='done', pct=100, speed=None, eta=None, note='', file=final, size=os.path.getsize(final))
            save_jobs()
    except Exception as e:
        with LOCK:
            if rt.get('stop'):
                apply_stop(j, rt['stop'])
            else:
                j.update(status='error', speed=None, eta=None, note=str(e)[:200])
                save_jobs()
    finally:
        RT.pop(jid, None)


def run_job(j):
    if j.get('kind') == 'drive':
        return run_drive(j)
    jid = j['id']
    rt = RT[jid]
    try:
        os.makedirs(j['folder'], exist_ok=True)
        if not j.get('title') or not j.get('dur'):
            r = subprocess.run(ytdlp_base() + ['--skip-download', '--print', '%(title)s', '--print', '%(duration)s', j['url']], capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=90, creationflags=NOWIN, stdin=subprocess.DEVNULL)
            t = (r.stdout or '').strip().splitlines()
            if t:
                j['title'] = j.get('title') or t[0]
            if len(t) > 1 and num(t[1]):
                j['dur'] = num(t[1])
        if j.get('title') and j['quality'] != 'audio':
            ex = file_for(j['folder'], j['title'])          # an old truncated file would make yt-dlp say "already downloaded"
            if ex and not media_ok(ex, j.get('dur'))[0]:
                os.replace(ex, ex + '.incomplete')
        if rt.get('stop'):
            with LOCK:
                apply_stop(j, rt['stop'])
            return
        cmd = ytdlp_base() + fmt_args(j['quality']) + [
            '-o', os.path.join(j['folder'], '%(title)s.%(ext)s'), '--windows-filenames', '--newline',
            '--retries', '20', '--fragment-retries', '20', '--http-chunk-size', '10M', '-N', '4', '--no-mtime', '--progress-template', TPL, j['url']]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True,
                                encoding='utf-8', errors='replace', creationflags=NOWIN, stdin=subprocess.DEVNULL,
                                env={**os.environ, 'PYTHONUNBUFFERED': '1'})
        rt['proc'] = proc
        nstreams, idx, dest, final, err = 1, -1, None, None, None
        botcheck = False
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith('P|'):
                f = line.split('|')
                if len(f) < 7:
                    continue
                dl, tot, est = num(f[2]), num(f[3]), num(f[4])
                tot = tot or est
                sp, eta = num(f[5]), num(f[6])
                sp_pct = (dl / tot * 100) if (dl is not None and tot) else 0
                if nstreams == 2:
                    w = [0.92, 0.08]
                    pct = (sum(w[:max(idx, 0)]) * 100) + w[min(max(idx, 0), 1)] * sp_pct
                else:
                    pct = sp_pct
                j.update(pct=round(min(pct, 99.5), 1), speed=sp, eta=eta, size=tot or j.get('size'), stream=f'{max(idx, 0) + 1}/{nstreams}')
                if f[1] == 'finished' and idx == nstreams - 1:
                    j['status_txt'] = 'finishing'
                continue
            if line:
                j['last'] = line[:140]
            if 'Downloading' in line and 'format(s):' in line:
                nstreams = line.split('format(s):')[1].strip().count('+') + 1
                idx = -1
            elif line.startswith('[download] Destination:'):
                idx += 1
                dest = line.split('Destination:', 1)[1].strip()
            elif 'has already been downloaded' in line:
                m = re.match(r'\[download\] (.*) has already been downloaded', line)
                if m:
                    final = m.group(1)
                j['pct'] = 99.5
            elif line.startswith('[Merger]') and 'into "' in line:
                final = line.split('into "', 1)[1].rstrip('"')
                j.update(pct=99.5, status_txt='merging', speed=None, eta=None)
            elif line.startswith('[ExtractAudio]') and 'Destination:' in line:
                final = line.split('Destination:', 1)[1].strip()
                j['status_txt'] = 'converting'
            elif line.startswith('ERROR'):
                err = line[:300]
                if 'Sign in to confirm' in line or 'cookies' in line.lower() and 'valid' in line.lower():
                    botcheck = True
            elif 'no longer valid' in line:
                COOKIE_FLAG['rejected'] = True
        rc = proc.wait()
        stop = rt.get('stop')
        with LOCK:
            if stop == 'pause':
                j.update(status='paused', speed=None, eta=None, note='paused - resume continues the .part file')
            elif stop == 'cancel':
                JOBS[:] = [x for x in JOBS if x['id'] != jid]
            elif rc == 0:
                f = final or dest
                j.update(status='done', pct=100, speed=None, eta=None, note='', status_txt='')
                if f and os.path.isfile(f):
                    j['file'] = f
                    j['size'] = os.path.getsize(f)
                    ok, why = (True, '') if j['quality'] == 'audio' else media_ok(f, j.get('dur'))
                    if not ok:                                  # cut-off download: never pass it on
                        os.replace(f, f + '.incomplete')
                        j.update(file='', size=None, pct=0)
                        if j.get('tries', 0) < 2:
                            j.update(status='queued', tries=j.get('tries', 0) + 1, note=f'incomplete ({why}) - retrying {j.get("tries", 0) + 1}/2')
                        else:
                            j.update(status='error', note=f'incomplete ({why}) after 3 tries - press retry')
            else:
                if botcheck:
                    COOKIE_FLAG['rejected'] = True
                j.update(status='error', speed=None, eta=None,
                         note=('cookie rejected / bot check - export a fresh cookie.txt, then retry. ' if botcheck else '') + (err or f'yt-dlp exit {rc}'))
            save_jobs()
    except Exception as e:
        with LOCK:
            j.update(status='error', note=f'{e}')
            save_jobs()
        traceback.print_exc()
    finally:
        RT.pop(jid, None)


def scheduler():
    while True:
        try:
            with LOCK:
                running = sum(1 for j in JOBS if j['status'] == 'running')
                for j in JOBS:
                    if running >= SET['concurrency']:
                        break
                    if j['status'] == 'queued' and j['id'] not in RT:
                        j.update(status='running', note='', status_txt='', speed=None, eta=None)
                        RT[j['id']] = {}
                        threading.Thread(target=run_job, args=(j,), daemon=True).start()
                        running += 1
        except Exception:
            traceback.print_exc()
        time.sleep(1)


def norm(s):
    return re.sub(r'\W+', '', (s or '').lower())


YT_ID = re.compile(r'(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})')


def add_jobs(items, folder, quality):
    """items: [{'id','title'}]"""
    added = 0
    with LOCK:
        live = {j['id'] for j in JOBS if j['status'] in ('queued', 'running', 'paused')}
        for it in items:
            vid = it['id']
            kind = it.get('kind', 'yt')
            jid = ('drive-' if kind == 'drive' else '') + vid + '-' + re.sub(r'\W', '', folder)[-12:] + quality
            if jid in live:
                continue
            JOBS[:] = [x for x in JOBS if x['id'] != jid]      # re-adding finished/failed one starts fresh
            JOBS.append({'id': jid, 'vid': vid, 'kind': kind, 'title': it.get('title', ''), 'dur': it.get('dur'), 'tries': 0,
                         'url': f'https://drive.google.com/file/d/{vid}/view' if kind == 'drive' else f'https://www.youtube.com/watch?v={vid}',
                         'folder': folder, 'quality': quality, 'status': 'queued', 'pct': 0, 'speed': None, 'eta': None,
                         'size': None, 'note': '', 'file': '', 'added': time.time(), 'stream': '', 'status_txt': ''})
            added += 1
        save_jobs()
    return added


# ---------- channels ----------
def find_channel(key):
    return next((c for c in SET['channels'] if c['key'] == key), None)


def fetch_channel(key):
    ch = find_channel(key)
    if not ch:
        return
    st = CH_STATE.setdefault(key, {})
    if st.get('refreshing'):
        return
    st.update(refreshing=True, error='', partial=[])

    def work():
        try:
            cmd = ytdlp_base(playlist=True) + ['--flat-playlist', '--playlist-end', str(SET['list_limit']), '--print',
                                              '%(id)s\t%(title)s\t%(duration)s\t%(view_count)s\t%(playlist_channel)s', ch['url']]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                    errors='replace', creationflags=NOWIN, stdin=subprocess.DEVNULL)
            items, cname = [], ''
            threading.Thread(target=lambda: setattr(proc, 'err', proc.stderr.read()), daemon=True).start()
            for line in proc.stdout:        # stream so the grid fills while yt-dlp is still paging
                f = line.rstrip('\r\n').split('\t')
                if len(f) < 2 or len(f[0]) != 11:
                    continue
                items.append({'id': f[0], 'title': f[1], 'dur': num(f[2]) if len(f) > 2 else None,
                              'views': num(f[3]) if len(f) > 3 else None})
                st['partial'] = list(items)
                if len(f) > 4 and f[4] not in ('NA', ''):
                    cname = f[4]
            proc.wait()
            r = type('R', (), {'stderr': getattr(proc, 'err', '')})
            if items:
                jsave(f'ch_{key}.json', {'items': items, 'updated': time.time()})
                if ch.get('autoname') and cname:
                    ch['name'] = cname
                    ch.pop('autoname', None)
                    save_set()
            else:
                st['error'] = (r.stderr.strip().splitlines() or ['no videos found'])[-1][:200]
        except Exception as e:
            st['error'] = str(e)
        finally:
            st['refreshing'] = False

    threading.Thread(target=work, daemon=True).start()


def channel_payload(key):
    ch = find_channel(key)
    if not ch:
        return {'error': 'unknown channel'}
    c = jload(f'ch_{key}.json', None)
    if c is None:
        fetch_channel(key)
    have = set()
    try:
        stems = {norm(Path(f).stem): os.path.join(ch['folder'], f) for f in os.listdir(ch['folder']) if f.lower().endswith(('.mp4', '.mkv', '.webm'))}
    except Exception:
        stems = {}
    items = c['items'] if c else CH_STATE.get(key, {}).get('partial', [])
    bad, todo = [], []
    for it in items:
        p = stems.get(norm(it['title']))
        if not p:
            continue
        if probe(p, cached_only=True) is None:
            todo.append(p)
        ok, _ = media_ok(p, it.get('dur'), cached_only=True)
        if ok:
            have.add(it['id'])
        else:
            bad.append(it['id'])
    bg_probe(todo)                       # first look at a folder: verify files in the background
    return {'items': items, 'updated': c['updated'] if c else 0, 'have': sorted(have), 'bad': bad, 'folder': ch['folder'],
            'refreshing': CH_STATE.get(key, {}).get('refreshing', False), 'error': CH_STATE.get(key, {}).get('error', '')}


def resolve_channel(text):
    t = text.strip()
    if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', t):
        return f'https://www.youtube.com/channel/{t}/videos', t
    if t.startswith('@'):
        return f'https://www.youtube.com/{t}/videos', t[1:]
    if re.fullmatch(r'[A-Za-z0-9_.-]{3,}', t):
        return f'https://www.youtube.com/@{t}/videos', t
    m = re.search(r'youtube\.com/(@[\w.-]+|channel/UC[\w-]{22}|c/[\w.-]+|user/[\w.-]+)', t)
    if m:
        part = m.group(1)
        return f'https://www.youtube.com/{part}/videos', part.split('/')[-1].lstrip('@')
    return None, None



# ---------- compilations: "NAME / List: / titles / <channel>" -> download in order -> optional editor hook ----------
import difflib
COMPS = jload('comps.json', [])
for _c in COMPS:
    if _c['status'] == 'premiere':        # app closed mid-import: never auto-rebuild (it would make a 2nd project)
        _c.update(status='error', note='interrupted during the Premiere step - check Premiere for the project, press retry only if it is missing')


def save_comps():
    with LOCK:
        jsave('comps.json', COMPS)


def chan_words():
    w = {}
    for c in SET['channels']:
        w[c['key'].lower()] = c['key']
        w[c['name'].strip().lower()] = c['key']
    return w


def parse_comps(text):
    CHAN_WORDS = chan_words()
    blocks, cur, pend = [], [], None

    def has_list():
        return any(l.strip().lower() == 'list:' for l in cur)

    def close(ch):
        nonlocal cur
        low = [l.strip().lower() for l in cur]
        i = low.index('list:')
        name = next((l.strip() for l in cur[:i] if l.strip()), '')
        titles = [l.strip() for l in cur[i + 1:] if l.strip()]
        blocks.append({'name': name, 'channel': ch, 'titles': titles})
        cur = []
    for line in text.replace('\r', '').split('\n'):
        w = line.strip().lower()
        if w in CHAN_WORDS:
            if has_list():
                close(CHAN_WORDS[w])
                pend = None
            else:
                cur = []
                pend = CHAN_WORDS[w]      # channel written above the title
        else:
            cur.append(line)
    if has_list():
        close(pend)
    return blocks


def cnorm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def chan_items(key):
    c = jload(f'ch_{key}.json', None)
    if c:
        return c['items']
    ch = find_channel(key)
    r = subprocess.run(ytdlp_base(playlist=True) + ['--flat-playlist', '--playlist-end', str(SET['list_limit']), '--print', '%(id)s\t%(title)s', ch['url']],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900, creationflags=NOWIN, stdin=subprocess.DEVNULL)
    items = [{'id': f[0], 'title': f[1]} for f in (l.split('\t') for l in r.stdout.splitlines()) if len(f) > 1 and len(f[0]) == 11]
    if items:
        jsave(f'ch_{key}.json', {'items': items, 'updated': time.time()})
    return items


def match_titles(titles, key):
    """match on the target channel; titles it does not have are searched on your other channels as a fallback"""
    def idx(k):
        return {cnorm(v['title']): v for v in chan_items(k)}
    main = idx(key)
    others = None
    out, missing = [], []
    for want in titles:
        k = cnorm(re.sub(r'\s*\(.{0,30}channel\)\s*$', '', want, flags=re.I))
        hit = main.get(k)
        if not hit:
            m = difflib.get_close_matches(k, list(main), 1, 0.85)
            hit = main[m[0]] if m else None
        if not hit:
            if others is None:
                others = {}
                for c in SET['channels']:
                    if c['key'] != key:
                        others.update(idx(c['key']))
            hit = others.get(k)
            if not hit:
                m = difflib.get_close_matches(k, list(others), 1, 0.85)
                hit = others[m[0]] if m else None
        if hit:
            out.append({'want': want, 'id': hit['id'], 'title': hit['title'], 'dur': hit.get('dur')})
        else:
            missing.append(want)
    return out, missing


def file_for(folder, title):
    k = cnorm(title)
    try:
        for f in os.listdir(folder):
            if f.lower().endswith('.mp4') and cnorm(f[:-4].split(' (1080p')[0]) == k:
                return os.path.join(folder, f)
    except Exception:
        pass
    return None


def job_id(vid, folder, q):
    return vid + '-' + re.sub(r'\W', '', folder)[-12:] + q


def add_comps(text, premiere):
    blocks = parse_comps(text)
    if not blocks:
        return {'ok': False, 'error': 'no "List:" block found'}
    errs, plans = [], []
    for b in blocks:
        if not b['channel']:
            errs.append(f"{b['name'] or '?'}: no channel line - end the block with one of your channel names ({', '.join(c['name'] for c in SET['channels']) or 'add a channel first'})")
            continue
        if not b['titles']:
            errs.append(f"{b['name']}: empty list")
            continue
        items, missing = match_titles(b['titles'], b['channel'])
        if missing:
            errs.append(f"{b['name']}: NOT FOUND on channel -> " + ' | '.join(missing))
            continue
        plans.append((b, items))
    if errs:
        return {'ok': False, 'error': '\n'.join(errs)}      # nothing starts until every title resolves
    for b, items in plans:
        folder = find_channel(b['channel'])['folder']
        add_jobs([{'id': it['id'], 'title': it['title'], 'dur': it.get('dur')} for it in items if not file_valid(folder, it)], folder, '1080')
        with LOCK:
            COMPS.append({'id': str(int(time.time() * 1000)), 'name': b['name'], 'channel': b['channel'], 'folder': folder,
                          'premiere': premiere, 'items': [{**it, 'job': job_id(it['id'], folder, '1080')} for it in items],
                          'status': 'downloading', 'note': '', 'result': None, 'added': time.time()})
            save_comps()
        time.sleep(0.01)
    return {'ok': True, 'started': len(plans)}


def hook_args(tpl, c, extra=None):
    """expand an editor-hook command template (quotes kept together, {project_arg} -> --project N or nothing)"""
    tf = DATA / f"titles_{c['id']}.txt"
    tf.write_text('\n'.join(i['title'] for i in c['items']), 'utf-8')
    ff = DATA / f"files_{c['id']}.txt"
    ff.write_text('\n'.join(file_for(c['folder'], i['title']) or '' for i in c['items']), 'utf-8')
    vals = {'name': c['name'], 'channel': c['channel'], 'folder': c['folder'], 'titles': str(tf), 'files': str(ff),
            'project_no': str(c.get('project_no') or ''), **(extra or {})}
    out = []
    for tok in re.findall(r'"[^"]*"|\S+', tpl):
        if tok == '{project_arg}':
            out += ['--project', str(c['project_no'])] if c.get('project_no') else []
            continue
        tok = tok.strip('"')
        out.append(re.sub(r'\{(\w+)\}', lambda m: vals.get(m.group(1), m.group(0)), tok))
    return out


def premiere_status(c):
    """optional: the status hook prints/writes JSON {projects:[], active:'', seqs:[]}; None when unset or unreachable"""
    tpl = SET.get('premiere_status_cmd', '').strip()
    if not tpl:
        return None
    out = DATA / 'editor_status.json'
    try:
        if out.exists():
            out.unlink()
        r = subprocess.run(hook_args(tpl, c, {'out': str(out)}), capture_output=True, text=True, timeout=90, creationflags=NOWIN, stdin=subprocess.DEVNULL)
        r = json.loads(out.read_text('utf-8')) if out.exists() else json.loads(r.stdout)
        return None if r.get('fatal') else r
    except Exception:
        return None


def check_comp(c):
    rep = {'at': time.time(), 'items': [], 'project': {}}
    path = (c.get('result') or {}).get('project', '')
    st = premiere_status(c)
    if SET.get('premiere_status_cmd', '').strip():
        pj = {'saved': path, 'exists': bool(path) and os.path.isfile(path), 'premiere': st is not None}
        if st:
            norm_open = [os.path.normcase(p) for p in st['projects']]
            pj['open'] = bool(path) and os.path.normcase(path) in norm_open
            pj['active'] = st['active']
            key = cnorm(c['name'].split('..')[0])
            pj['seq'] = any(cnorm(q).startswith(key) for q in st['seqs'])
            pj['same_active'] = bool(path) and os.path.normcase(st['active']) == os.path.normcase(path)
        rep['project'] = pj
    for n, it in enumerate(c['items'], 1):
        f = file_for(c['folder'], it['title'])
        if not f:
            rep['items'].append({'n': n, 'title': it['title'], 'ok': False, 'why': 'file missing'})
            continue
        ok, why = media_ok(f, it.get('dur'))
        rep['items'].append({'n': n, 'title': it['title'], 'ok': ok, 'why': why})
    c['check'] = rep
    return rep


PREM_LOCK = threading.Lock()      # one editor-hook run at a time (so project numbers never collide)


def run_premiere(c):
    with PREM_LOCK:
        _run_premiere(c)


def _run_premiere(c):
    tpl = SET.get('premiere_cmd', '').strip()
    if not tpl:                                   # no hook configured: the ordered file list is the result
        hook_args('x', c)
        with LOCK:
            c.update(status='done', result={'files': str(DATA / f"files_{c['id']}.txt")}, note='')
        save_comps()
        return
    try:
        r = subprocess.run(hook_args(tpl, c), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3600,
                           creationflags=NOWIN, stdin=subprocess.DEVNULL)
        out = r.stdout or ''
        i = out.rfind('\n{')
        try:
            res = json.loads(out[i + 1:]) if i >= 0 else {}
        except Exception:
            res = {}
        with LOCK:
            if r.returncode == 0:
                c.update(status='done', result=res or {'ok': True}, note='')
            else:
                c.update(status='error', note='editor hook failed: ' + (r.stderr or out).strip()[-300:])
    except Exception as e:
        with LOCK:
            c.update(status='error', note=f'editor hook failed: {e}')
    save_comps()


def comp_watcher():
    while True:
        try:
            with LOCK:
                jm = {j['id']: j for j in JOBS}
                for c in COMPS:
                    if c['status'] != 'downloading':
                        continue
                    ready, bad = 0, None
                    for it in c['items']:
                        j = jm.get(it['job'])
                        if j and j['status'] == 'error':
                            bad = j['note']
                        if j and j['status'] != 'done':
                            continue
                        if file_valid(c['folder'], it):
                            ready += 1
                    c['note'] = ('a download failed: ' + bad[:120]) if bad else ''
                    if ready == len(c['items']):
                        if c['premiere'] and not c.get('result'):     # one editor run per compilation, ever
                            c['status'] = 'premiere'
                            threading.Thread(target=run_premiere, args=(c,), daemon=True).start()
                        else:
                            c['status'] = 'done'
                        save_comps()
        except Exception:
            traceback.print_exc()
        time.sleep(3)


# ---------- http ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        h = (self.headers.get('Host') or '').split(':')[0]
        o = self.headers.get('Origin')
        if h not in ('127.0.0.1', 'localhost'):
            return False
        return not o or urlparse(o).hostname in ('127.0.0.1', 'localhost')

    def _send(self, obj, code=200, ctype='application/json'):
        b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8' if ctype != 'application/octet-stream' else ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._ok_host():
            return self._send({'error': 'forbidden'}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ('/', '/index.html'):
                return self._send((APP / 'index.html').read_bytes(), 200, 'text/html')
            if u.path == '/api/state':
                with LOCK:
                    return self._send({'settings': {k: v for k, v in SET.items()}, 'cookie': cookie_status(),
                                       'jobs': JOBS, 'comps': COMPS, 'compilations': True, 'now': time.time()})
            if u.path == '/api/channel':
                return self._send(channel_payload(q.get('key', [''])[0]))
            self._send({'error': 'not found'}, 404)
        except Exception as e:
            traceback.print_exc()
            self._send({'error': str(e)}, 500)

    def do_POST(self):
        if not self._ok_host() or 'application/json' not in (self.headers.get('Content-Type') or ''):
            return self._send({'error': 'forbidden'}, 403)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            d = json.loads(self.rfile.read(n) or b'{}')
            self._send(self.route(urlparse(self.path).path, d))
        except Exception as e:
            traceback.print_exc()
            self._send({'error': str(e)}, 500)

    def route(self, path, d):
        if path == '/api/add':
            text = d.get('text', '')
            items = [{'id': i['id'], 'title': i.get('title', '')} for i in d.get('items', [])]
            have = {i['id'] for i in items}
            dgroups, dnotes = collect_drive(text, d.get('drive_folder') or d.get('folder') or SET['base'])
            for m in YT_ID.finditer(text):
                if m.group(1) not in have:
                    items.append({'id': m.group(1)})
                    have.add(m.group(1))
            for line in text.splitlines():
                s = line.strip()
                if re.fullmatch(r'[A-Za-z0-9_-]{11}', s) and s not in have:
                    items.append({'id': s})
                    have.add(s)
            # playlist links -> expand
            for m in re.finditer(r'https?://[^\s]*[?&]list=([A-Za-z0-9_-]+)[^\s]*', text):
                if 'v=' in m.group(0):
                    continue
                r = subprocess.run(ytdlp_base(playlist=True) + ['--flat-playlist', '--print', '%(id)s\t%(title)s',
                                                              f'https://www.youtube.com/playlist?list={m.group(1)}'],
                                   capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, creationflags=NOWIN, stdin=subprocess.DEVNULL)
                for line in r.stdout.splitlines():
                    f = line.split('\t')
                    if len(f[0]) == 11 and f[0] not in have:
                        items.append({'id': f[0], 'title': f[1] if len(f) > 1 else ''})
                        have.add(f[0])
            if not items and not dgroups:
                return {'ok': False, 'error': 'no YouTube / Google Drive links found' + ('; ' + '; '.join(dnotes) if dnotes else '')}
            folder = d.get('folder') or SET['base']
            n = add_jobs(items, folder, d.get('quality') or SET['quality'])
            found = len(items)
            for tgt, its in dgroups.items():
                n += add_jobs(its, tgt, 'file')
                found += len(its)
            return {'ok': True, 'added': n, 'found': found, 'notes': dnotes}
        if path == '/api/comp/add':
            return add_comps(d.get('text', ''), bool(d.get('premiere', True)))
        if path == '/api/comp/check':
            c = next((x for x in COMPS if x['id'] == d.get('id')), None)
            if not c:
                return {'ok': False}
            r = check_comp(c)
            save_comps()
            return {'ok': True, 'check': r}
        if path == '/api/comp':
            with LOCK:
                c = next((x for x in COMPS if x['id'] == d.get('id')), None)
                if not c:
                    return {'ok': False}
                a = d.get('action')
                if a == 'remove':
                    COMPS.remove(c)
                elif a == 'rename' and c['status'] != 'premiere':
                    if d.get('name', '').strip():
                        c['name'] = d['name'].strip()
                    pn = re.sub(r'\D', '', str(d.get('project_no') or ''))
                    c['project_no'] = int(pn) if pn else None
                elif a == 'retry' and c['status'] == 'error':
                    c['status'] = 'downloading'
                    for it in c['items']:
                        j = next((x for x in JOBS if x['id'] == it['job']), None)
                        if j and j['status'] == 'error':
                            j['status'] = 'queued'
                    save_jobs()
                save_comps()
            return {'ok': True}
        if path == '/api/job':
            with LOCK:
                j = next((x for x in JOBS if x['id'] == d.get('id')), None)
                if not j:
                    return {'ok': False}
                a = d.get('action')
                rt = RT.get(j['id'])
                if a == 'pause' and j['status'] in ('running', 'queued'):
                    stop_job(j, 'pause')
                elif a in ('resume', 'retry') and j['status'] in ('paused', 'error'):
                    j.update(status='queued', note='', pct=j['pct'] if a == 'resume' else 0)
                elif a == 'remove':
                    stop_job(j, 'cancel')
                save_jobs()
            return {'ok': True}
        if path == '/api/jobs':
            a = d.get('action')
            with LOCK:
                for j in list(JOBS):
                    if a == 'resume_all' and j['status'] in ('paused', 'error'):
                        j['status'] = 'queued'
                        j['note'] = ''
                    elif a == 'pause_all' and j['status'] in ('running', 'queued'):
                        stop_job(j, 'pause')
                    elif a == 'clear_done' and j['status'] == 'done':
                        JOBS.remove(j)
                save_jobs()
            return {'ok': True}
        if path == '/api/open':
            p = d.get('path', '')
            with LOCK:
                allowed = {j.get('file') for j in JOBS} | {j['folder'] for j in JOBS} | {c['folder'] for c in SET['channels']} | {SET['base']}
            if p in allowed and os.name == 'nt':
                if os.path.isfile(p):
                    subprocess.Popen(['explorer', '/select,' + p])
                elif os.path.isdir(p):
                    os.startfile(p)
            return {'ok': True}
        if path == '/api/channel/refresh':
            fetch_channel(d.get('key'))
            return {'ok': True}
        if path == '/api/channels':
            a = d.get('action')
            with LOCK:
                if a == 'add':
                    url, name = resolve_channel(d.get('input', ''))
                    if not url:
                        return {'ok': False, 'error': 'give an @handle, channel URL or UC... id'}
                    key = re.sub(r'\W', '', name.lower())[:20] or 'ch'
                    if find_channel(key):
                        return {'ok': False, 'error': 'already added'}
                    SET['channels'].append({'key': key, 'name': name, 'autoname': True, 'url': url,
                                            'folder': d.get('folder') or str(Path(SET['base']) / key),
                                            'color': PALETTE[len(SET['channels']) % len(PALETTE)]})
                    save_set()
                    return {'ok': True, 'key': key}
                ch = find_channel(d.get('key'))
                if not ch:
                    return {'ok': False}
                if a == 'remove':
                    SET['channels'].remove(ch)
                elif a == 'edit':
                    for k in ('name', 'folder'):
                        if d.get(k):
                            ch[k] = d[k]
                            ch.pop('autoname', None)
                save_set()
            return {'ok': True}
        if path == '/api/cookie/save':
            t = d.get('text', '')
            if not any(len(l.split('\t')) >= 7 for l in t.splitlines()) or 'youtube' not in t.lower() and 'google' not in t.lower():
                return {'ok': False, 'error': 'does not look like a Netscape cookie.txt for YouTube'}
            p = SET['cookie']
            if os.path.isfile(p):
                shutil.copy2(p, p + '.bak')
            Path(p).write_text(t, encoding='utf-8', newline='\n')
            return {'ok': True, 'cookie': cookie_status()}
        if path == '/api/cookie/extract':
            b = d.get('browser', 'chrome')
            if b not in ('chrome', 'edge', 'firefox', 'brave', 'opera', 'vivaldi'):
                return {'ok': False, 'error': 'unknown browser'}
            SET['browser'] = b
            save_set()
            res = extract_cookies(b)
            if res['ok']:
                res['test'] = test_cookie()
            return {**res, 'cookie': cookie_status()}
        if path == '/api/cookie/test':
            return {'ok': True, 'result': test_cookie(), 'cookie': cookie_status()}
        if path == '/api/settings':
            with LOCK:
                for k in ('cookie', 'proxy', 'base', 'quality', 'premiere_cmd', 'premiere_status_cmd'):
                    if k in d:
                        SET[k] = str(d[k]).strip()
                for k in ('warn_days', 'concurrency', 'list_limit'):
                    if k in d:
                        SET[k] = max(1, int(d[k]))
                save_set()
            return {'ok': True}
        return {'error': 'unknown'}


def open_ui():
    url = f'http://127.0.0.1:{PORT}'
    for e in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files\Google\Chrome\Application\chrome.exe', r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'):
        if os.path.isfile(e):
            subprocess.Popen([e, f'--app={url}', '--window-size=1280,860'])
            return
    webbrowser.open(url)


ICON = APP / 'assets' / 'icon.ico'


def stop_all():
    for rt in list(RT.values()):
        if rt.get('proc'):
            killtree(rt['proc'])


def run_window():
    """native window (WebView2) with the app icon; False when pywebview is unavailable"""
    try:
        import webview
    except Exception:
        return False
    webview.create_window('Let It Download', f'http://127.0.0.1:{PORT}', width=1280, height=860, min_size=(900, 600))
    webview.start()
    return True


def main():
    if FROZEN or sys.stderr is None or 'pythonw' in os.path.basename(sys.executable).lower():
        sys.stderr = sys.stdout = open(DATA / 'server.log', 'a', encoding='utf-8')
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    except OSError:
        open_ui()      # already running
        return
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=cookie_watcher, daemon=True).start()
    threading.Thread(target=comp_watcher, daemon=True).start()
    if '--no-ui' in sys.argv:
        srv.serve_forever()
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if run_window():            # window closed = quit; running downloads come back paused (.part kept)
        stop_all()
        save_jobs()
        os._exit(0)
    open_ui()
    srv.serve_forever()


if __name__ == '__main__':
    main()
