# Let It Download

A psychedelic (Tame Impala *Currents*-flavoured) desktop GUI for **yt-dlp** on Windows.

- Download YouTube links, playlists and **whole channels** (add any `@handle` / channel URL as a tab)
- **Cookie.txt** support with expiry watching: header chip, banner, desktop toast, one-click "Test with YouTube"
  and **Extract from browser** (Firefox / Chrome / Edge, keeps only YouTube/Google cookies)
- **Resume**: pause, close the app, come back - `.part` files continue where they stopped
- **Verification**: every video is checked with ffprobe; cut-off downloads are quarantined as `.incomplete` and retried
- **Google Drive** file and folder links (resumable, uses your cookie for private files)
- **Compilations**: paste `Title / List: / titles… / channel`, it matches the titles, downloads them in order and can hand the ordered files to your editor (Premiere, Resolve, ffmpeg…)

## Run

Needs Windows 10/11 with WebView2 (preinstalled on 11), `yt-dlp` and `ffmpeg`/`ffprobe` on `PATH`, Python 3.10+.

```
pip install -r requirements.txt
python server.py
```

Or grab `LetItDownload.exe` from Releases (a native window, no browser needed).

## Build the EXE

```
pip install pyinstaller pywebview pillow numpy
build.bat
```

`tools/make_icon.py` generates `assets/icon.ico`. The EXE is unsigned, so Windows *Smart App Control* may block
it on some machines - run `python server.py` instead or sign the file.

## Compilations

Add your channels first (the **+** tab). Then paste one or more blocks into **🎬 Compilations**:

```
My new video title
List:
First video title
Second video title
Third video title
MyChannel
```

- The first line is the compilation / sequence name; the block ends with the name of one of your channel tabs
  (the channel line may also come *above* the title).
- Titles are matched on that channel (fuzzy, then your other channels as fallback). If one is not found nothing starts and
  the missing titles are listed.
- Videos download in list order into that channel's folder. Existing complete files are skipped; truncated ones are
  quarantined as `.incomplete` and re-downloaded.
- Every file is verified with ffprobe (video **and** audio length) before the compilation counts as ready.
- **🔍** re-checks files (and your editor project, if you configured a status hook), **✎** renames it and sets an optional
  project number, **↻** retries a failed one.

### Editor hook (optional)

Under **Cookie → settings → editor hook** put a command that is run once when all files are ready. Placeholders:
`{name}` `{channel}` `{folder}` `{titles}` (file with the titles) `{files}` (file with full paths, in order)
`{project_no}` and `{project_arg}` (becomes `--project N`, or nothing). See `examples/editor_hook_example.py`:

```
python examples/editor_hook_example.py --name {name} --files {files} {project_arg}
```

The hook must exit 0 and may print a JSON object on its last line (`{"project": "C:\path\project.prproj"}`).
Without a hook you simply get the ordered list in `data/files_<id>.txt`.

The optional **status hook** prints/writes JSON `{"projects": [...], "active": "...", "seqs": [...]}` (use `{out}` for
the output file) so the 🔍 button can tell whether the project is open and contains the sequence.

## Cookies

Export a Netscape `cookie.txt` (e.g. with the "Get cookies.txt LOCALLY" extension) to
`%USERPROFILE%\Downloads\cookie.txt`, or press **Extract cookies now** on the Cookie tab.
Your cookies never leave your machine; `data/` (settings, jobs, cookie backups) is git-ignored.

## Notes

yt-dlp is a separate project; respect YouTube's terms and the rights of the content owners.
