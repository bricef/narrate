# Building videos

How to turn scripts into videos with narrate: on your computer, in Docker, or
in GitHub Actions. [Writing scripts](writing-scripts.md) covers what goes in
the script. The commands below assume narrate is installed (see the
[README](../README.md#install)). From a source checkout, the README's alias
makes `narrate` work in any folder.

- [Edit in the studio](#edit-in-the-studio)
- [Build a video](#build-a-video)
- [Preview quickly](#preview-quickly)
- [Review a video without watching it](#review-a-video-without-watching-it)
- [Make an audio file instead](#make-an-audio-file-instead)
- [Choose a speech engine](#choose-a-speech-engine)
- [Use ElevenLabs](#use-elevenlabs)
- [Pick voices](#pick-voices)
- [Build in Docker](#build-in-docker)
- [Build in GitHub Actions](#build-in-github-actions)
- [Serve narrate over HTTP](#serve-narrate-over-http)
- [Manage downloads and caches](#manage-downloads-and-caches)
- [Make builds faster](#make-builds-faster)
- [Troubleshoot](#troubleshoot)
- [Command reference](#command-reference)
- [Environment variables](#environment-variables)

## Edit in the studio

```sh
narrate studio script.md              # then open http://127.0.0.1:8787/
narrate studio script.md --open       # and open it in a browser
narrate studio new.md                 # a new script, created on the first save
```

The studio is a web app for writing a script, served from your computer.

- **Editor:** narrate's own syntax is highlighted: scenes, stage directions,
  pictures and clips, slides, render blocks and SSML.
- **Saving:** Ctrl-S (Cmd-S on a Mac) or **Save**. The file on disk stays the
  script, so you can still edit it elsewhere, diff it and build it from the
  command line.
- **Edits made elsewhere:** they appear in the studio by themselves when it
  has nothing unsaved. Otherwise it asks whether to load the file or keep
  your version. It never overwrites a newer file without asking.
- **Timing as you type:** each scene separator is labelled with the scene's
  number and length, and each paragraph with its own.
  - Speech already synthesised gives exact lengths.
  - The rest are estimated from the text and marked `≈`. Nothing is
    synthesised while you type, so nothing is spent.
- **Outline:** the scenes, with when each starts and how long it lasts.
  Click one to go there.
- **Problems:** missing files, unknown directions and bad values are marked
  on their lines as you type.
- **Preview:** the frame at the cursor, composed by the same code a build uses,
  with pictures, slides, callouts and theme.
  - **The moment shown** is the start of the paragraph the cursor is in, or
    the moment picked on the timeline.
  - **Clips** show a still from that point, and pan and zoom the crop
    reached by then.
  - **HTML scenes** show the page at that moment, as a build captures it.
    - **Clock:** the page runs on a clock the studio moves, with its cues
      and at its `viewport`.
    - **Scrubbing:** drag the timeline, step it with the arrow keys, or play
      the scene, and the page follows frame by frame.
    - **Going back:** a page with scripts of its own reloads to go back in
      time.
    - **The exact check:** your browser draws the page, so fonts can differ
      from narrate's Chromium; **Render scene** is exact.
    - **Sandboxed:** the page runs in an origin of its own, so it can't use
      the studio. Its own files (pictures, styles, scripts, fonts and data it
      fetches) load as in a build, but it has no storage, such as
      `localStorage`.
  - **Mermaid diagrams** are drawn as you type, fitted and coloured as a
    build draws them. A diagram that doesn't parse shows Mermaid's message
    instead.
  - **Other render blocks** show what the last build drew, or a card until a
    build has drawn them.
  - **Web captures** (`render:web`): with the cursor on one, the inspector
    says which page it's of and when it was taken.
    - **Capture now** or **Retake capture** renders that scene with
      `--regenerate`, which takes the page afresh and keeps the new capture.
    - The preview then shows the new capture.
    - A page on this machine, such as your dev server, needs the studio
      started with `NARRATE_ALLOW_LOCAL_NETWORK=1`
      ([Build a video](#build-a-video)).
  - **Overlays** lie over the frame as they will in the video, and scrub and
    play like HTML scenes.
    - A scene's overlay runs on the scene's clock, with its cues.
    - The header's overlays run on the whole video's clock, so a ticker
      carries on across scenes.
    - `(overlay: none)` hides the header's overlays.
- **Listening:** each paragraph's length is also its ▶ button, and plays
  the scene from there.
  - **Synthesis:** the scene is synthesised on first play. It's the same
    narration a build uses, clip sound included, and it's cached, so the
    timing turns exact and building later costs nothing more.
  - **Controls:** **Play scene** and **Stop** sit under the preview, which
    follows the narration paragraph by paragraph, and an HTML scene frame by
    frame.
- **Engine:** chosen in the header, or with `--tts` when starting. Kokoro
  is the default, and free.
  - **Cost:** the header shows how many paragraphs it still has to
    synthesise, and how many characters that is; with ElevenLabs, that's what
    playing or building would spend.
  - **Asking first:** with ElevenLabs, the studio asks before synthesising a
    scene.
- **Building:** **Build video** saves the script, then builds it with the
  studio's engine. It writes the same files as `narrate build`, next to the
  script.
  - **Progress:** the header shows how far it has got, with **Cancel**.
  - **Watching:** the result plays in the **Video** tab.
  - **Failures:** a failed build lists its errors, and clicking one goes to
    its line.
- **Timeline:** the whole video along the bottom, each scene as wide as it
  lasts.
  - **What it shows:** each scene's pictures, clips, pages, render blocks and
    slides, as long as each is on screen; the narration's waveform, with
    paragraphs not yet synthesised shown as grey blocks; where each
    transition and paragraph starts; and named cues (`<mark>`). Click a cue's
    name to go to its word.
  - **Seeking:** click or drag to show that moment in the preview. The
    editor's cursor moves to the paragraph playing then, and **Play scene**
    starts from there. Moving the cursor yourself moves the playhead back.
    With the timeline focused, ← and → step a frame, and Shift a second.
  - **Zoom:** **+** and **−**, or Ctrl (or Cmd) and the mouse wheel. **Fit**
    shows the whole video again.
  - **Double-click** a picture or slide to go to its line.
- **Inspector:** with the cursor on a picture, clip, HTML scene or render
  block, controls under the preview change how it's shown. Each change edits
  the script's text, as you would by hand, and can be undone in the editor
  with Ctrl-Z (Cmd-Z on a Mac).
  - **Fit:** `cover`, `contain` or `fit`. **Position:** the six positions
    narrate has; those that make no difference for this picture are dimmed.
  - **Framing a picture:** the whole picture shows below, with its crop and
    focal point. Click or drag there, or drag the preview, to move the focal
    point anywhere (`position: 38% 50%`); it becomes a named position when it
    lands on one.
  - **Pan and zoom:** `zoom`, `pan`, `panzoom` and the rest, and how far it
    moves, or **custom** for windows of your own (`from` and `to`). The first
    and last frames' windows show on the whole picture: drag one to move it,
    or its corner to zoom. **Show the motion** plays it.
  - **Clips:** sync, the segment to play, and volume or mute.
  - **The written form is kept:** `![cover top](photo.jpg)` stays a picture
    line, with its words changed in place. A zoom amount or a volume other
    than mute needs `(image: {…})`, so the line switches to that form.
- **Callouts:** **◯ circle** and **▭ rectangle**, under the preview, draw
  one on the frame shown; it's written as a `(callout: …)` direction after
  the scene's picture. The scene's callouts show as outlines: click one to
  select it, drag it to move it, or drag its handles to resize it.
  Coordinates are the frame's own pixels, so what you draw is what renders.
  With the cursor on a callout, the inspector switches its shape, sets its
  shading or makes it a black mask, and deletes it.
- **Voices:** the **Voices** tab lists every voice: Kokoro's, ElevenLabs'
  defaults, Narakeet's names and your pinned names, each with its language
  and what it becomes on the studio's engine.
  - **Audition:** ▶ reads the sample (the paragraph at the cursor, or what you
    type) the way the script would, with its voice speed and pitch, so you
    choose by ear. Each sample is cached once heard; with ElevenLabs, the
    first play of each asks first, saying how many characters it sends.
  - **Use** writes `voice:` in the header (the whole script), or `(voice: …)`
    before the paragraph at the cursor (from there on).
  - **Pins:** pin a name to a voice on each engine, saved to `voices.yaml` next
    to the script, so `voice: Narrator` is that voice on Kokoro and on
    ElevenLabs ([Pick voices](#pick-voices)). Pins in your user-level
    `voices.yaml` are shown, not changed.
  - With the cursor on a `voice:` line or a `(voice: …)`, the inspector offers
    the picker.
- **Timing marks:** with the cursor in narration, **Add mark** (or Alt-M;
  Option-M on a Mac) puts a `<mark name="…"/>` before the word at the cursor,
  so an HTML scene or overlay can time something to it with `--cue-NAME`.
  - **The name:** it's named after the word (`fleet`, or `fleet-2` if the
    scene has one), and the name is selected, so typing renames it.
  - **The paragraph:** it keeps its markup and is spoken the same way, only
    split at the mark.
- **Rendering a scene:** **Render scene**, under the preview, saves the
  script, then renders the scene at the cursor with the real builder, as
  `narrate build --scenes` does. It plays in the **Scene** tab: exactly that
  part of the video, with music, subtitles and theme. It goes in a temporary
  folder, never over the video, and a scene whose text, engine and files
  haven't changed isn't rendered again.
- **The design:** the studio's decisions and their reasons are in
  [design/studio.md](design/studio.md).

**Other sites:** the studio answers only to `localhost`, `127.0.0.1`,
`[::1]` and its `--host` address, refuses changes sent from another site's
page, and can't be shown in another site's frame. So a web page open in your
browser can't read your project or start builds through it.

**Other people's projects:** the preview runs a project's HTML scenes,
overlays and slides sandboxed. So a page in a project from someone else can't
change your script, switch the engine, start builds or read what the studio
answers.

**On another machine,** such as a server you reach over a VPN:
`narrate studio script.md --host 0.0.0.0 --allow-host server`, then open
`http://server:8787/`. `--allow-host` names the host name or address you open
it by; repeat it for more than one. The studio has no login yet. Anyone who
can reach the port can read the files in the script's folder (not hidden ones
such as `.env`) and change the script, so only expose it on a network you
trust, or use an SSH tunnel, which needs neither option:
`ssh -L 8787:127.0.0.1:8787 server`.

**In Docker:**

```sh
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/work" -v narrate-cache:/cache \
    -p 127.0.0.1:8787:8787 ghcr.io/bricef/narrate studio script.md --host 0.0.0.0
```

## Build a video

```sh
narrate build script.md                   # script.mp4 and script.jpg, in the current folder
narrate build script.md -o out/video.mp4  # choose where
narrate build project.zip                 # a ZIP of the script and its files
```

- **Outputs:** the MP4, a poster JPG next to it and, when subtitles are on
  (any mode but `none`), `.srt` and `.vtt` files. They are named after the
  script (for a ZIP, the script inside it) and go in the current folder,
  unless `-o` says otherwise.
- **Subtitles per build:** `--subtitles MODE` chooses the subtitles for this
  build, over the script's header, keeping the header's style (font, colour,
  `break` and so on). For example, from one script:

  ```sh
  narrate build talk.md --subtitles external -o youtube.mp4   # clean video + youtube.srt to upload
  narrate build talk.md --subtitles tiktok -o tiktok.mp4      # captions burned in (and tiktok.srt)
  ```

- **Transcript:** `--transcript` also writes what's spoken as plain text,
  `<output>.transcript.txt`: a line per paragraph and a blank line between
  scenes, for show notes, a blog post or an accessibility page. Recordings
  are transcribed for it, which downloads the speech-to-text model the first
  time.
- **Progress** goes to the terminal: one line per scene, then notes about
  anything narrate adapted, such as a voice it matched. `-q` keeps it quiet.
- **Check first:** `narrate check script.md` finds missing pictures, clips and
  recordings, and lines it doesn't understand, in a second.
- **Timing and cost:** `narrate check script.md --timing` also plans it,
  still without building or spending anything:
  - each scene's start and length, and the total;
  - what's left to synthesise, in paragraphs and characters, for the engine
    `--tts` names (auto, as for a build);
  - bad values, and missing theme, music or font files, as errors with their
    lines.

  Lengths are exact for speech already synthesised with that engine (by a
  build, or the studio) and estimated from the text elsewhere, marked `≈`.
  `--json` prints the same report for programs. `--tts elevenlabs` works
  without an ElevenLabs key, so you can see what a build would spend before
  setting one up: it reads the speech cache and estimates the rest, without
  calling ElevenLabs.
- **Local addresses:** the pages a build captures reach only public
  addresses. That's `render:web` pages, HTML scenes, overlays and slides, and
  everything they load. So a script can't put what's on this machine or your
  network in its video: the studio, a router's page, or a cloud's metadata
  address.
  - **Your own dev server:** `--allow-local-network` lets pages reach this
    machine and private networks, so `render:web` can capture
    `http://localhost:3000`. `NARRATE_ALLOW_LOCAL_NETWORK=1` does the same,
    and reaches the studio's builds when you start the studio with it.
  - **Without it:** a `render:web` capture of a local address stops the
    build, naming the address. Anything else a page asks of one is refused,
    and the build notes it.
  - **Stored captures** are reused as before: only taking one afresh needs
    the address.
- **Intermediate files** are deleted afterwards; `--keep-work DIR` keeps them
  for inspection.
- **First build:** the Kokoro voice model downloads (about 350 MB), and from
  a source checkout so does ffmpeg (a 150 MB download). Later builds reuse
  them.

## Preview quickly

```sh
narrate build script.md --tts silent -o preview.mp4
```

`--tts silent` replaces speech with silence timed like speech, so a build
takes seconds and spends no credits. Use it to check layout, timing and
subtitles before rendering the real voices. It doesn't show which voice each
name maps to; a real build's notes do.

**Just some scenes:**

```sh
narrate build script.md --scenes 3      # script-scene-3.mp4
narrate build script.md --scenes 3-5    # script-scenes-3-5.mp4; also 2,4 or 1-2,5
```

- **Exact:** the scenes look and sound as they will in the whole video. The
  background music is the part that plays under them, not its beginning.
- **Only those scenes cost anything:** the others are timed from speech
  already synthesised, or estimated, and nothing is synthesised for them.
  Until it is, the music's position is estimated, and the build says so.
- **The edges:** the header's `fade-in` applies only if scene 1 is included,
  and `fade-out` only if the last scene is. The first scene rendered has no
  transition in, since nothing comes before it. Loudness is normalised over
  the scenes rendered.
- **Merged scenes,** which share one narration (`narration-mode: merge`), are
  rendered together.
- A problem in a scene you didn't ask for doesn't stop the build.

## Review a video without watching it

```sh
narrate frames script.mp4 --script script.md   # one frame per scene: script-frames.png
narrate frames script.mp4                      # 12 frames, evenly spaced
narrate frames script.mp4 --at 1.5,12,30 -o look.png
narrate frames script.mp4 --every 5
```

A contact sheet: the video's frames side by side, each labelled with its
time, and with `--script`, its scene. It shows layout, framing, slides,
callouts and subtitles at a glance, for a quick look on a phone, or for an AI
agent that can read a picture but not play a video.

- **One per scene:** `--script` takes each scene's middle from the script's
  timing (as `narrate check --timing` gives it). Pass the `--tts` engine the
  video was built with; if the script's length doesn't match the video's,
  narrate says so.
- **Size:** up to four frames across, each `--width` pixels wide (default
  480).

**Hear it, too:**

```sh
narrate hear script.mp4 --script script.md    # where the narration sounds different from the script
narrate hear script.mp4                       # just what's heard, with times
```

`narrate hear` listens with narrate's speech-to-text and lines up what it
hears with the script's narration, then lists only the differences, with the
scene, line and moment of each:

```
script.md: 152 of 154 words heard as written (99%)
    0:12.4  scene 2, line 14: wrote "yt-dlp", heard "it DLP"
    0:48.0  heard "thanks for watching", not in the script (a recording, or noise)
```

- **What counts as the same:** case and punctuation don't count. Words heard
  joined, split or spelled almost alike match ("QuietTube" for "quietube").
  Numbers match their spoken form ("531" and "five hundred and thirty-one").
- **Use it** to catch names, acronyms and jargon the voice gets wrong, then
  fix them with [pronunciation markup](writing-scripts.md#control-pronunciation).
  Speech-to-text makes mistakes of its own (homophones such as "your" and
  "you're"), so a difference is worth a listen, not proof.
- **Recordings** aren't in the script's text, so their words show as heard but
  not in the script.
- `--json` prints the comparison for programs; `--language` sets the language
  if the script doesn't.

## Make an audio file instead

```sh
narrate audio script.md                  # script.mp3
narrate audio script.md -o talk.m4a      # .mp3, .m4a, .aac, .ogg, .opus, .flac or .wav
```

The audio holds the narration, recordings, clip soundtracks and background
music, timed exactly as in the video. Pictures, slides and callouts are
skipped. When subtitles are on, `.srt` and `.vtt` files come with it.
`--subtitles` and `--transcript` work as for videos.

## Choose a speech engine

`--tts` picks the engine for a build:

| Engine | What it is |
| --- | --- |
| `auto` (default) | ElevenLabs when `ELEVENLABS_API_KEY` is set, otherwise Kokoro |
| `kokoro` | Kokoro-82M, free and running on your computer's CPU. 54 voices in 9 languages, several times faster than real time on a modern CPU. |
| `elevenlabs` | ElevenLabs' service: natural voices in many languages, using your account's credits |
| `silent` | no speech, for previews |

- **Kokoro's languages:** American and British English, Spanish, French,
  Hindi, Italian, Japanese, Brazilian Portuguese and Mandarin. A voice in any
  other language falls back to Kokoro's default American voice, with a note;
  use ElevenLabs for German, Russian, Korean and the rest.
- **Speed:** both engines render some speeds themselves (Kokoro 0.5–2×,
  ElevenLabs 0.7–1.2×, except its v3 and v4 models). narrate stretches the
  audio for speeds beyond those, keeping the pitch.
- **Rebuilds** reuse every paragraph that hasn't changed, from the speech
  cache, so they cost no credits.

## Use ElevenLabs

1. Create an API key in your ElevenLabs account. Text-to-speech permission is
   enough.
2. Put it in a `.env` file, either in the folder you run narrate from or next
   to the script:

   ```sh
   ELEVENLABS_API_KEY=sk_...
   ```

   Or set `ELEVENLABS_API_KEY` in the environment, which wins over `.env`.
   Keep `.env` out of version control.

   `.env` sets only `ELEVENLABS_API_KEY`. A project's folder may not be
   yours, so narrate ignores any other variable there and warns, naming it.
   Set the others in the environment.
3. Build. With a key set, `--tts auto` uses ElevenLabs; `--tts kokoro` still
   picks Kokoro.

**What it will cost:** `narrate check script.md --timing --tts elevenlabs`
says how many characters a build would send, with or without a key. Speech
already in the cache costs nothing.

- **Model:** `eleven_multilingual_v2` by default. Choose another with
  `--tts-model eleven_v4`, or `NARRATE_ELEVENLABS_MODEL`.
  - **Phonetic spans** are always voiced by `eleven_flash_v2`, the model that
    reads IPA.
  - **`eleven_v3` and `eleven_v4`** have no speed setting. narrate speaks with
    them at their own pace and stretches the audio to the `voice-speed` you
    ask for, keeping the pitch. Other models set the speed themselves between
    0.7 and 1.2, and narrate stretches beyond that.
- **Voices:** ElevenLabs' default voices work on any plan. Voices from the
  Voice Library need a paid plan, and listing your own library needs a key
  with voice-reading permission.
- **Credits:** every new paragraph spends credits; cached ones don't. Preview
  with `--tts silent` until the script settles.
- **What it spent:** `--usage spent.json` (on `build` and `audio`) records
  what the build sent, by model:
  - characters and requests;
  - credits, as ElevenLabs billed them or, where it doesn't say, at the
    model's rate (half a credit a character for flash and turbo);
  - the characters the cache saved;
  - CPU time.
- **Rate limits:** narrate sends two requests at a time, the free plan's limit,
  and retries when ElevenLabs is busy. Raise it with
  `NARRATE_ELEVENLABS_CONCURRENCY` on a paid plan.

## Pick voices

A script names voices in any of three ways:

- **A Narakeet voice name**, such as `Brian` or `Amy`.
- **A Kokoro voice ID**, such as `bf_emma`.
- **An ElevenLabs voice:** a default voice's name (`George`), or the ID of a
  voice in your account.

Each engine uses the name directly if it is one of its own voices.
ElevenLabs also looks in your account's library, if the key may read it.
Otherwise the engine picks one of its voices with the same language and
gender, and prints a note saying which. Australian, Indian, Irish, Scottish
and South African English fall back to British, then American. A name it
can't place at all gets the engine's default voice, also with a note.

One name is in both catalogues: `Brian` is Narakeet's British voice and an
ElevenLabs default voice, which is American. narrate takes it as the
ElevenLabs voice on both engines; pin it (as below) for a British voice.

To choose exactly, pin names in a `voices.yaml` next to the script, or in
`~/.config/narrate/voices.yaml` for every project (the project's file wins):

```yaml
Brian:
  kokoro: bm_lewis
  elevenlabs: George      # a default voice name, or a voice ID
Amy:
  kokoro: bf_emma
```

`narrate voices` lists every engine's voices; `narrate voices script.md` also
shows the pins in effect for that script.

## Build in Docker

The image `ghcr.io/bricef/narrate` holds narrate and everything it needs, so
Docker is the only thing to install:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v narrate-cache:/cache \
    ghcr.io/bricef/narrate build script.md -o video.mp4
```

- **Files:** the current folder is mounted at `/work`, where narrate runs, so
  paths are relative to it. `--user` makes the video yours rather than root's.
- **Cache:** the voice model, music and synthesised speech go in `/cache`. The
  `narrate-cache` volume keeps them between runs. Use the same `--user` with
  it each time; another user can't write to it.
- **Processors:** the image is built for x86-64 and arm64, so it runs
  natively on Intel and AMD machines and on Macs with Apple chips.
- **ElevenLabs:** keep the key in a `.env` in the mounted folder, or pass
  `-e ELEVENLABS_API_KEY`.
- **Other commands** work the same way: put `check script.md`, `audio
  script.md` or `doctor` after the image name.
- **Fonts:** the image covers Latin, Greek and Cyrillic, plus emoji. For other
  scripts, build an image on top of it:

  ```dockerfile
  FROM ghcr.io/bricef/narrate
  USER root
  RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
   && rm -rf /var/lib/apt/lists/*
  USER narrate
  ```

- **Size:** about 1.2 GB unpacked, 420 MB to download.

To build the image from a checkout, see [Development](development.md#the-container-image).

## Build in GitHub Actions

The repository is a GitHub Action, named "narrate video", that builds a video
in the narrate image. Its outputs match Narakeet's `build-video-github-action`:

```yaml
name: Video
on: push
jobs:
  video:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: read          # to pull the narrate image
    steps:
      - uses: actions/checkout@v7
      - id: video
        uses: bricef/narrate@v0
        with:
          source-path: videos/intro.md
          output: out/intro.mp4
          elevenlabs-api-key: ${{ secrets.ELEVENLABS_API_KEY }}   # optional
      - uses: actions/upload-artifact@v7
        with:
          name: intro
          path: |
            ${{ steps.video.outputs.video-file }}
            ${{ steps.video.outputs.poster-file }}
```

`@v0` follows narrate's 0.x releases: each release moves it, and the action
builds with that release's image. Before 1.0 a release may change how a script
renders, so for builds that change only when you say so, pin a
[release](https://github.com/bricef/narrate/releases):

```yaml
        uses: bricef/narrate@v0.1.1
```

| Input | Default | Meaning |
| --- | --- | --- |
| `source-path` | (required) | the script, or a `.zip` project, in the repository |
| `output` | `narrate/result.mp4` | where to write the video; the poster and subtitles go next to it |
| `tts` | `auto` | the speech engine, as `--tts` |
| `elevenlabs-api-key` | none | an ElevenLabs key, from a secret |
| `subtitles` | none | the subtitles for this build, as `--subtitles` (the script decides when empty) |
| `transcript` | `false` | `true` also writes a transcript, as `--transcript` |
| `image` | the release's | the narrate image to build with, such as `ghcr.io/bricef/narrate:latest` |

Outputs: `video-file`, `poster-file`, `srt-file` and `vtt-file` when subtitles
are on, and `transcript-file` when a transcript is asked for.

- **The image:** the action builds with the image of the release it's used
  at: `ghcr.io/bricef/narrate:1.2.3` for `@v1.2.3`, and
  `ghcr.io/bricef/narrate:0` for `@v0`, which each 0.x release moves too. A
  branch or a commit, such as `@main`, builds with `latest`, which follows
  `main`. The `image` input overrides it.
- **0.1.0's action came before this:** it builds with `latest` unless you
  give it `image: ghcr.io/bricef/narrate:0.1.0`. `@v0` moves past it with
  the next release.
- **From your own composite action,** set `image`: GitHub can give a nested
  action its caller's ref, and so the wrong image.
- The voice model and synthesised speech are cached between runs, so
  unchanged paragraphs cost nothing.
- Paths must be inside the repository checkout.

## Serve narrate over HTTP

`narrate serve` offers narrate over HTTP, so your own programs (agents, CI
jobs, other services) can check, build and review videos by calling it.

```sh
narrate serve                                    # http://127.0.0.1:8788/v1
narrate serve --host 0.0.0.0 --allow-host buildbox --workers 2 --data /srv/narrate
docker run -p 8788:8788 -v narrate-data:/data -v narrate-cache:/cache -e ELEVENLABS_API_KEY \
  ghcr.io/bricef/narrate serve --host 0.0.0.0 --data /data
```

- **No login:** keep it on localhost or a private network. Anything that can
  reach the port can spend its ElevenLabs credits.
- **Host names:** it answers to `localhost`, `127.0.0.1`, `[::1]` and its
  `--host` address. Programs on other machines call it by its machine's name
  or address: allow each with `--allow-host`, which you can repeat. A request
  to any other name gets 403, so a web page can't reach it by pointing its own
  name at your machine.
- **Browsers:** a request that changes something, sent from another site's
  page, gets 403. Programs send no `Origin` header, so this doesn't affect
  them.
- **Local addresses:** jobs' pages reach only public addresses, so a job
  can't capture the service, other clients' jobs, or anything else on your
  network. `--allow-local-network` lets every build job reach them, the
  service included. A job can't ask for it, and `NARRATE_ALLOW_LOCAL_NETWORK`
  doesn't reach jobs.
- **HTTP proxies:** builds honour `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`
  (including lowercase forms). narrate resolves and checks each destination
  first, then asks the configured proxy to connect to that checked address.
  Local addresses remain refused unless `--allow-local-network` is set.
- **Speech:** jobs use Kokoro unless they ask for another engine, or you
  start it with `--tts`. With `"tts": "elevenlabs"`, a job uses the service's
  own `ELEVENLABS_API_KEY`, from its environment or a `.env` where it starts.
  Jobs read no `.env` of their own, so a project can't set anything for its
  job.
- **Workers:** `--workers` jobs run at once (default 1). A build already uses
  every core.
- **Its data:** uploaded files, each job's project and outputs, and the job
  records (SQLite) go in `--data`. Nothing is deleted on its own.
- **The design:** [design/service.md](design/service.md).

**A project** is the script and its files, each uploaded once and named by its
SHA-256. A rebuild after an edit uploads only what changed. Send the whole
project folder: HTML scenes load files narrate can't see in the script.
Hidden files (`.env`, `.git`) are refused, apart from `.narrate/`. A ZIP's
hidden files are left out when it's unpacked.

```sh
api=http://127.0.0.1:8788/v1
h=$(sha256sum photo.jpg | cut -d' ' -f1)
curl -s -X POST $api/files/missing -H 'Content-Type: application/json' -d "{\"hashes\": [\"$h\"]}"
curl -s -X PUT --data-binary @photo.jpg $api/files/$h
curl -s -X POST $api/jobs -H 'Content-Type: application/json' -d @- <<JSON
{"kind": "build", "options": {"tts": "kokoro", "subtitles": "external"},
 "project": {"script": "intro.md", "text": "![](photo.jpg)\n\nHello there.\n", "files": {"photo.jpg": "$h"}}}
JSON
curl -s $api/jobs/j_19a…                         # its state, progress and outputs
curl -sO $api/jobs/j_19a…/outputs/intro.mp4
```

| Request | Does |
| --- | --- |
| `POST /v1/files/missing` | `{hashes}`: which of these files to upload |
| `PUT /v1/files/{sha256}` | upload one; refused if its content doesn't match |
| `POST /v1/check` | `{project, tts}`: `narrate check --timing --json`, answered at once: problems with their lines, scene times, and what the engine would be sent |
| `GET /v1/voices` | every voice, with what it becomes on an engine (`?tts=elevenlabs`) |
| `POST /v1/auditions` | `{voice, text, tts}`: a short sample as a script would read it; an audio job, answered when it's done |
| `POST /v1/jobs` | start a job: `{kind, project, options}`, or `{kind, media, options}` to review |
| `GET /v1/jobs` | recent jobs, newest first (`?state=running`, `?limit=`) |
| `GET /v1/jobs/{id}` | a job: state, progress, outputs, notes, errors |
| `GET /v1/jobs/{id}/events` | the job as it changes (server-sent events), ending with it |
| `POST /v1/jobs/{id}/cancel` | stop it |
| `GET /v1/jobs/{id}/outputs/{name}` | an output file |
| `GET /v1/usage` | usage and cost by day or by job (`?since=2026-10-01&until=…&by=job&format=csv`) |
| `GET /v1/openapi.json` | all of it, as OpenAPI |

- **Kinds:**
  - `build` makes a video, and `audio` the narration alone.
  - `frames` makes a contact sheet ([Review a video](#review-a-video-without-watching-it)).
  - `hear` reports what speech-to-text hears, and where it differs from the
    script.
- **What to review:** a review job's `media` is a finished job's output,
  `{"job": "j_…"}`, which brings the job's script and engine. Or it's an
  uploaded file, `{"file": sha256, "name": "talk.mp4"}`, with a `project` to
  compare it with.
- **Projects:** files by hash (`{"script": "intro.md", "files": {path: sha256}}`),
  a script sent as `text`, with or without files, or an uploaded ZIP
  (`{"zip": sha256}`, with `"source"` if it holds more than one script).
- **Options:** those of the matching command.
  - `build` and `audio`: `tts`, `tts_model`, `subtitles` and `transcript`.
  - `build` only: `scenes` and `regenerate`.
  - `audio` only: `format` (`mp3`, `m4a`, `ogg`, `opus`, `flac` or `wav`).
  - `frames`: `at`, `every`, `count`, `width`, `tts` and `tts_model`.
  - `hear`: `language`.

  A job records the engine it used.
- **States:** `queued`, `running`, `done`, `failed` (with `errors`, giving
  script lines where they're known) and `cancelled`. Restarting the service
  fails the jobs that were running, and runs those queued.
- **Outputs:** by name, `video` (or `audio`), `poster`, `srt`, `vtt` and
  `transcript`, or a review's `sheet` (PNG) or `report` (JSON). Any `render:web` capture taken is also an output, under
  `captures`, by its path in the project, so you can keep it there for later
  builds.

**What each job cost:**
- **Usage:** every job records what it spent, as `--usage` reports it:
  - speech characters, requests and credits by model;
  - what the cache saved;
  - renders drawn;
  - the video's length;
  - wall and CPU time.
- **Prices:** start the service with `--prices prices.yaml`, and each job's
  `usage.cost` prices that usage:

  ```yaml
  currency: USD
  elevenlabs_per_1000_credits: 0.30   # what your ElevenLabs plan charges
  cpu_per_hour: 0.05                  # what the machine's time is worth, if anything
  ```

  The prices are kept with each job, so changing them later doesn't change
  what earlier jobs cost. Without a prices file, jobs report usage but no
  cost.
- **Reports:** `GET /v1/usage` totals usage and cost for any period, a row per
  day or per job, as JSON or CSV:

  ```sh
  curl -s "$api/usage?since=2026-10-01&by=day"
  curl -s "$api/usage?since=2026-10-01&by=job&format=csv" > october.csv
  ```

  Usage is kept with the job records, indefinitely. A check costs nothing,
  but it says what a build would send, so you can see the price first.

## Manage downloads and caches

narrate fetches some files on first use and caches synthesised speech:

| What | Where | Size |
| --- | --- | --- |
| Kokoro voice model | `~/.cache/narrate/models/` | about 350 MB |
| ffmpeg (from a source checkout, on Linux only) | `~/.cache/narrate/ffmpeg/` | 170 MB (a 150 MB download) |
| Speech-to-text model, for subtitles on recordings | `~/.cache/narrate/stt/` | about 460 MB |
| Built-in music | `~/.cache/narrate/music/` | a few MB |
| mermaid.js, for diagrams | `~/.cache/narrate/mermaid/` | about 5 MB |
| Synthesised speech and transcripts | `~/.cache/narrate/tts/`, `stt/` | grows with use |
| Pictures drawn from render blocks | `~/.cache/narrate/render/` | grows with use |
| Web captures (`render:web`) | `.narrate/generated/web/`, beside the script | kept until you delete them; commit them |

- **Download ahead of time**, for example before going offline:
  `narrate models`.
- **Transcripts** of recordings are `.json` files in `stt/`, beside the
  speech-to-text model. `narrate cache --prune` leaves them alone; to clear
  them, delete the `.json` files rather than the folder.
- **Move them:** `NARRATE_TOOLS` sets where downloads go, and `NARRATE_CACHE`
  where speech is cached. Both default to `~/.cache/narrate`, or
  `$XDG_CACHE_HOME/narrate`.
- **The speech and render caches are never pruned on their own:**
  - `narrate cache` shows their size per engine and renderer.
  - `narrate cache --prune --max-size 500M` removes the least recently used
    entries until it fits.
  - `narrate cache --prune --older-than 90` removes entries unused for 90 days.
  - Entries used in the last hour are kept, so a running build keeps what it
    needs.

## Make builds faster

- **Preview** with `--tts silent` while working on layout.
- **Rebuilds** are faster than first builds: unchanged paragraphs come from
  the speech cache.
- **Moving pages cost the most:** an HTML scene or overlay that animates is
  captured frame by frame, at about twice real time, so a ticker over a
  three-minute video adds about a minute and a half. A page that never changes
  is captured once.
- **Parallelism:** narrate synthesises speech in the background while earlier
  scenes render, and encodes several segments at once. The result is the same
  as a one-at-a-time build.

  | Setting | Default |
  | --- | --- |
  | `--jobs N` or `NARRATE_JOBS` | up to 4 encodes at once |
  | `NARRATE_KOKORO_WORKERS` | up to 4 Kokoro sessions |
  | `NARRATE_ELEVENLABS_CONCURRENCY` | 2 requests at once |

## Troubleshoot

Start with `narrate doctor`. It checks ffmpeg, Chromium, fonts, the voice
models, the cache and the ElevenLabs key, and prints a fix for each problem.

- **"Chromium is not installed for Playwright":** run the fix doctor
  prints. That's `uv run playwright install chromium` in a checkout, and
  `playwright install chromium` after `uv tool install`.
- **"Chromium can't start: system libraries are missing":**
  - With root, run the fix doctor prints, such as
    `uv run playwright install-deps chromium`.
  - Without root, use the Docker image, or extract the libraries yourself
    (Ubuntu 24.04 and Debian 13 package names):

    ```sh
    mkdir -p ~/.cache/chromium-libs && cd ~/.cache/chromium-libs
    apt-get download libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
        libcups2t64 libdrm2 libgbm1 libwayland-server0 libxcomposite1 libxdamage1 libxext6 \
        libxfixes3 libxi6 libxkbcommon0 libxrandr2 libxrender1 libxshmfence1 libxtst6
    find . -name '*.deb' -exec dpkg -x {} root \;
    export LD_LIBRARY_PATH=$HOME/.cache/chromium-libs/root/usr/lib/x86_64-linux-gnu
    ```

    Put the `export` line in your shell profile so it lasts; the `just`
    recipes set it themselves when the folder exists. If Chromium then names
    another missing library, `apt-get download` its package into the same
    folder and extract it the same way.
- **"ELEVENLABS_API_KEY is not set":** you asked for `--tts elevenlabs`
  without a key; see [Use ElevenLabs](#use-elevenlabs).
- **"ElevenLabs request failed (401)" or "(402)":** the message after the
  code says why. Common causes are a wrong or revoked key, used-up credits,
  and a Voice Library voice on a free plan.
- **A voice sounds wrong:** read the notes at the end of the build. They name
  each voice narrate substituted; pin the one you want in `voices.yaml`
  ([Pick voices](#pick-voices)).
- **"file not found":** paths are relative to the script's folder, and in
  Docker everything must be inside the mounted folder.
- **"narrate can't write to …":** another user filled part of the cache, for
  example a Docker build without `--user`. `narrate build` stops before it
  starts, and the message ends with the command that gives the folders back
  to you. For the Docker volume:

  ```sh
  docker run --rm --user 0 -v narrate-cache:/cache --entrypoint chown \
      ghcr.io/bricef/narrate -R "$(id -u):$(id -g)" /cache
  ```

- **Text shows as boxes:** the font lacks those characters. Install a font that
  has them, or name one in a theme with `@font-face`.
- **"no ffmpeg on PATH, and narrate has no ffmpeg of its own":** narrate
  downloads its own ffmpeg on Linux only. On macOS, run
  `brew install ffmpeg-full`, which narrate finds without being told.
  Homebrew's plain `ffmpeg` lacks libass and rubberband. Elsewhere, narrate
  uses the ffmpeg on `PATH`.
- **Another ffmpeg:** narrate uses its own build on Linux, because older
  versions crash on some transitions. `NARRATE_FFMPEG=/path/to/ffmpeg`
  overrides it. It needs ffmpeg 9.0 or later with libx264, libass,
  librubberband (for pitch and emphasis) and the `xfade` filter, which
  `narrate doctor` checks. Audio exports also need libmp3lame (MP3) and
  libopus (OGG and Opus).

## Command reference

| Command | Does |
| --- | --- |
| `narrate studio SCRIPT [--open]` | edit the script in a web app (`--tts` for its engine; `--host`, `--port` to serve elsewhere; `--allow-host` for each other name it's opened by) |
| `narrate serve` | serve narrate over HTTP for your own programs (`--host`, `--port`, `--allow-host`, `--data`, `--workers`, `--tts`, `--prices`; `--allow-local-network` lets build jobs' pages reach local addresses) |
| `narrate build SCRIPT [-o OUT.mp4]` | render a video (`SCRIPT` may be a `.zip`; `--source` picks the script inside; `--scenes 3-5` builds just those; `--subtitles MODE` and `--transcript` choose the extra outputs; `--regenerate` retakes web captures; `--allow-local-network` lets pages reach this machine and private networks; `--usage FILE` records what it spent) |
| `narrate audio SCRIPT [-o OUT]` | render the narration only (also takes `--subtitles`, `--transcript` and `--usage`) |
| `narrate hear VIDEO [--script SCRIPT]` | what speech-to-text hears, and where it differs from the script (`--json`, `--usage`) |
| `narrate frames VIDEO [--script SCRIPT]` | a contact sheet of the video's frames (`--at`, `--every`, `--count`, `-o`, `--usage`) |
| `narrate check SCRIPT [--timing]` | report missing files and problems, without rendering; `--timing` adds scene lengths, the total and what synthesis would cost (`--json` for programs) |
| `narrate callouts FILE` | write a page for placing callouts on an image or a clip's frame |
| `narrate voices [SCRIPT]` | list voices, and a script's `voices.yaml` pins |
| `narrate models` | download the voice, speech-to-text, ffmpeg and mermaid.js files now |
| `narrate doctor` | check the setup |
| `narrate cache [--prune …]` | show or prune the speech and render caches |

`build` and `audio` take `--tts`, `--tts-model`, `--jobs` and `-q`; `build` also
takes `--keep-work`. `narrate COMMAND --help` lists every option.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `ELEVENLABS_API_KEY` | none | ElevenLabs key; also read from `.env` |
| `NARRATE_ELEVENLABS_MODEL` | `eleven_multilingual_v2` | ElevenLabs model, like `--tts-model` |
| `NARRATE_ELEVENLABS_CONCURRENCY` | 2 | ElevenLabs requests at once |
| `NARRATE_KOKORO_WORKERS` | up to 4 | Kokoro sessions at once |
| `NARRATE_JOBS` | up to 4 | encodes at once, like `--jobs` |
| `NARRATE_ALLOW_LOCAL_NETWORK` | none | `1` lets captured pages reach this machine and private networks, like `--allow-local-network` (not for `narrate serve`'s jobs) |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | system settings | proxy for captured pages; `NO_PROXY` bypasses it (lowercase names work too) |
| `NARRATE_STT_MODEL` | `small` | faster-whisper model for transcribing recordings |
| `NARRATE_MUSIC_DIR` | none | recordings to use instead of the built-in music |
| `NARRATE_FFMPEG` | narrate's own on Linux; on macOS, Homebrew's `ffmpeg-full` or the one on `PATH` | the ffmpeg to use |
| `NARRATE_MERMAID_JS` | narrate's own | the mermaid.js to use for `render:mermaid` |
| `NARRATE_TOOLS` | `~/.cache/narrate` | where downloads go |
| `NARRATE_CACHE` | `~/.cache/narrate` | where synthesised speech and transcripts are cached |
| `NARRATE_DOTENV` | `1` | `0` stops narrate reading `.env`; `narrate serve` sets it for its jobs |

Only `ELEVENLABS_API_KEY` is also read from `.env`. Set the others in the
environment.
