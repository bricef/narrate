# Changelog

Notable changes to narrate, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Removed

- **`narakeet-api-client`:** narrate reads Narakeet's script format, but it
  doesn't imitate Narakeet's command-line client. Use `narrate build`, or
  `narrate audio` for audio projects. Its downloads of ZIP URLs and GitHub
  folders went with it: fetch the project first, then build it.

### Fixed

- **A problem with the background music stops a build at the start:** a
  missing music file, one that won't decode, or a built-in track that can't
  be made in the cache now stops `narrate build` and `narrate audio` before
  any speech is synthesised or any scene captured, not once every scene is
  captured. Each error names the music; a track that couldn't be made used
  to end in a traceback.
- **The studio scrubs HTML scenes in Safari:** its preview's clock fakes
  only the timers the browser has, and WebKit has no `requestIdleCallback`.

## [0.1.1] - 2026-10-21

### Added

- **A major tag for the GitHub Action:** `uses: bricef/narrate@v0` follows the
  0.x releases. Each release moves it, and the image tag `0`, once its image
  has passed its checks. Only a newer release moves them, never an older one
  run again.

### Changed

- **The GitHub Action builds with its own release's image,** with no `image`
  input: `@v1.2.3` uses `ghcr.io/bricef/narrate:1.2.3`, and `@v0` uses
  `ghcr.io/bricef/narrate:0`. A branch or a commit still uses `latest`, and
  `image` still overrides it. 0.1.0's action uses `latest` unless given
  `image`.
- **Only release tags build images:** `v1.2.3`, and pre-releases such as
  `v1.3.0-rc.1`, which publish only their own image tag.
- **The GitHub Action is named "narrate video",** so that it can be listed on
  the GitHub Marketplace, which refuses `narrate`: an organisation has that
  name. `uses: bricef/narrate@…` doesn't change.
- **Dependencies:** Pillow 13 (narrate now needs it), Playwright 1.64 with
  its newer headless Chromium, Starlette 1.8, NumPy 2.5.4 and certifi
  2026.10.19. From a checkout or a uv install, run
  `playwright install chromium` again after updating if `narrate doctor`
  says Chromium is missing.

## [0.1.0] - 2026-10-21

narrate's first release. Earlier changes are in the git history.

If you ran narrate from `main` before this release, three things now work
differently, as Security explains: the studio and the service answer only to
their own host names (`--allow-host` adds more), the pages a build captures
reach only public addresses (`--allow-local-network` lets them reach yours),
and a `.env` sets only `ELEVENLABS_API_KEY`.

### Added

- **Scripts:** builds narrated videos from Narakeet's "Markdown to Video"
  script format: narration, pictures with pan and zoom, video clips, text and
  code slides, themes, callouts, transitions, recorded audio, background music
  and subtitles.
- **Speech:** Kokoro, free and running locally, or ElevenLabs with your own
  key.
- **narrate's extensions:** Mermaid diagrams, web page captures and
  placeholder cards as code blocks; animated HTML scenes; and HTML overlays.
- **Checking and reviewing:** `narrate check` reports problems, timing and
  what speech would cost; `narrate frames` and `narrate hear` review a built
  video without watching it.
- **Ways to run it:** the command line, `narrate studio` for writing scripts in
  a browser, `narrate serve` for your own programs over HTTP,
  `narakeet-api-client` for Narakeet's command line, a Docker image and a
  GitHub Action.
- **Linux and macOS:** install with `uv tool install`, from a checkout, or
  with Docker. On Linux narrate downloads its own ffmpeg; on macOS it uses
  Homebrew's `ffmpeg-full`. On Windows, use Docker.
- **The image** `ghcr.io/bricef/narrate` runs natively on x86-64 and arm64,
  Macs with Apple chips included. Each release has its own tags, such as
  `0.1.0` and `0.1`; `latest` follows `main`.
- **The GitHub Action:** pin it to a release, `bricef/narrate@v0.1.0`, with
  that release's image, `ghcr.io/bricef/narrate:0.1.0`.
- **`narrate doctor`** checks ffmpeg, Chromium, fonts, the voice models, the
  cache and an ElevenLabs key, and prints a fix for each problem.
  `narrate build` stops before it starts if it can't write to its cache.
- **An agent skill** (`skills/narrate/`) that teaches AI agents to make videos
  with narrate.
- **Licence:** MIT. The notices for the JavaScript the studio bundles are in
  `src/narrate/studio/static/vendor/NOTICES.md`, and the image keeps the
  licences of narrate, ffmpeg and mermaid.js in `/usr/share/doc/narrate/`.
- **For contributors:** a contributing guide, a code of conduct, a security
  policy for reporting vulnerabilities privately, and a dev container.

### Security

- **The studio and the service keep web pages out:** they answer only to
  `localhost`, `127.0.0.1`, `[::1]` and their `--host` address, and refuse
  changes sent from another site's page. Other sites can't frame them: they
  send `frame-ancestors 'self'` and `X-Frame-Options`. So a page open in your
  browser can't read your project or start builds through them.
- **`--allow-host`:** `narrate studio` and `narrate serve` answer to more host
  names with it, once for each. A service on `0.0.0.0` whose clients call it
  by its machine's name or address needs it.
- **Captured pages reach only public addresses:** `render:web` pages, HTML
  scenes, overlays and slides, and everything they load, can't reach this
  machine, a private network or a link-local address, such as a studio, a
  service, a router or a cloud's metadata address. A host name that resolves
  to a local address is refused too. A refused `render:web` capture stops the
  build, naming the address; anything else refused is noted.
- **`--allow-local-network`:** `narrate build` lets pages reach local
  addresses with it, or with `NARRATE_ALLOW_LOCAL_NETWORK=1`, to capture your
  own dev server. The variable also reaches the studio's builds.
  `narrate serve` takes the option for all its build jobs; a job can't turn
  it on itself.
- **The studio's preview sandboxes a project's pages:** HTML scenes, overlays
  and slides run in origins of their own. So a page in a project from someone
  else can't save over the script, switch the engine, start builds or read
  what the studio answers. Its own files still load as in a build, but it has
  no storage, such as `localStorage`. The studio serves project files
  sandboxed too.
- **`.env` sets only `ELEVENLABS_API_KEY`:** narrate ignores any other
  variable in a `.env`, and warns, naming it. Set the others in the
  environment. `NARRATE_DOTENV=0` stops narrate reading `.env` at all.
- **Projects bring no settings:** a ZIP's hidden files (`.env`, `.git`) are
  left out when it's unpacked, apart from `.narrate/`, as are those of a
  project `narakeet-api-client` downloads from GitHub. `narrate serve`
  refuses hidden files in a project, and inside `.narrate/` too. Its jobs
  read no `.env`: they run with the service's own environment.
- **Job outputs:** `narrate serve` serves only the files a job lists as its
  outputs, not the rest of the job's folder, and serves them sandboxed.

[Unreleased]: https://github.com/bricef/narrate/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/bricef/narrate/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bricef/narrate/releases/tag/v0.1.0
