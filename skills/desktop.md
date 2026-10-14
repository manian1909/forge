---
name: desktop
description: open apps, folders and sites, play songs, volume, send Outlook mail
triggers: \bvol(ume)?\b|\bmute\b|louder|quieter|\bskip\b|next song|previous song|\bpause\b|\bplay\b|\bopen\b.*(youtube|gmail|chrome|outlook|site|website|app)|\blaunch\b|\bstart\b.*\b(app|chrome|outlook|spotify)\b|\bsong\b|\bmusic\b|e-?mail|\bmail\b.*\bto\b|send.*(mail|email)
---
You control the user's PC with these tools. Use them directly, do not write PowerShell for these jobs.

- "play <song>" or "play <song> on youtube music": call `play_music` with the song name (add the artist if the user said it).
  Use service "youtube" only if they said plain YouTube. The tool finds the video and Chrome starts playing it, so do not
  search the web first and do not ask for a link.
- volume, next/previous song, pause, mute: call `media_control` (volume_set with value 0-100 for "vol 40%"; volume_down/volume_up for
  "a bit lower", value 5 for "a bit"). Never use PowerShell for volume: it has no such command.
- "open youtube / gmail / a website": call `open_url` with the full address (https://www.youtube.com, https://mail.google.com).
- "open outlook / excel / spotify / an app": call `open_app` with just the app name.
- "open documents / downloads / a folder / a drive": call `open_folder` with the folder name or path. Never use Start-Process or
  explorer.exe in the shell: they cannot start windows from the sandbox and print nothing, so they look like they worked when they did not.
- "mail <person> about <x>": call `send_email` with to, subject and a short body. The user approves it before it is sent.
  If the user gave no address, ask for it. Never invent one.

After the tool returns, say in one short line what happened. If it reports an error, say so; do not claim success.
