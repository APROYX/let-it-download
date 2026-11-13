"""Example editor hook for the Compilations tab.

Settings -> "editor hook":
    python examples/editor_hook_example.py --name {name} --channel {channel} --files {files} {project_arg}

The app runs it once per finished compilation. Replace the body with whatever your editor needs
(Premiere via ExtendScript/MCP, DaVinci Resolve API, ffmpeg concat, ...).
Print a JSON object as the LAST line of output; {"project": "..."} is shown in the card.
Exit code != 0 marks the compilation as failed (the Retry button runs it again).
"""
import argparse, json

ap = argparse.ArgumentParser()
ap.add_argument('--name', required=True)        # the compilation / sequence name (first line of your block)
ap.add_argument('--channel', default='')
ap.add_argument('--files', required=True)       # text file: one full video path per line, in list order
ap.add_argument('--project', type=int)          # only present when you set a project number with the pencil button
a = ap.parse_args()

files = [l.strip() for l in open(a.files, encoding='utf-8') if l.strip()]
print(f'{a.name}: {len(files)} clips')
for i, f in enumerate(files, 1):
    print(i, f)
print(json.dumps({'project': f'(example) {a.name}', 'clips': len(files)}))
