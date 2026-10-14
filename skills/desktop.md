---
name: desktop
description: open sites and apps, play songs, send Outlook mail
triggers: \bplay\b|\bopen\b.*(youtube|gmail|chrome|outlook|site|website|app)|\blaunch\b|\bstart\b.*\b(app|chrome|outlook|spotify)\b|\bsong\b|\bmusic\b|e-?mail|\bmail\b.*\bto\b|send.*(mail|email)
---
You control the user's PC with these tools. Use them directly, do not write PowerShell for these jobs.

- "play <song>" or "play <song> on youtube music": call `play_music` with the song name (add the artist if the user said it).
  Use service "youtube" only if they said plain YouTube. The tool finds the video and Chrome starts playing it, so do not
  search the web first and do not ask for a link.
- "open youtube / gmail / a website": call `open_url` with the full address (https://www.youtube.com, https://mail.google.com).
- "open outlook / excel / spotify / an app": call `open_app` with just the app name.
- "mail <person> about <x>": call `send_email` with to, subject and a short body. The user approves it before it is sent.
  If the user gave no address, ask for it. Never invent one.

After the tool returns, say in one short line what happened. If it reports an error, say so; do not claim success.
