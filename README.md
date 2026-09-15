# Introduction

<p align="center">
  <img src="docs/vnflight.png" alt="vnflight" width="220">
</p>

[![tests](https://github.com/vnflight/vnflight/actions/workflows/tests.yml/badge.svg)](https://github.com/vnflight/vnflight/actions/workflows/tests.yml)

**vnflight** is a shim that lets AI agents (and other clients) play Ren'Py games. The shim scrapes the text on the Ren'Py screen and sends it through a local bridge to a CLI or an MCP server, so an agent can read the story and make choices from text alone. Screenshots are available too, so agents with vision capabilities can look at the screen when text is not enough.

The project exists to explore how AI agents play visual novels and handle branching narrative choices. It can also be used for QA of Ren'Py games.

The bridge runs locally by default. A connected AI client may send the story text and screenshots it receives to its model provider; that depends on the client and its configuration.

The shim targets Ren'Py 6 through 8 (Python 2 and 3 engines). Adapters (the CLI flags call them mods) are executable Python compatibility code that runs inside the game; install only adapters you trust. They live in the separate [mods repository](https://github.com/vnflight/mods) (see [Adapters](docs/USER.md#adapters-mods)).

The project was developed mostly with the help of local and frontier AI models.

# Quick Install

Requirements: Python 3.10+, a locally installed Ren'Py game, and a command that starts it. A built game (Steam, GOG, itch) starts from its own executable; a game distributed as a Ren'Py project, such as the two sample games ([Mystic Cafe](https://github.com/vnflight/mystic_cafe), [Echoes of Tomorrow](https://github.com/vnflight/echoes_of_tomorrow)), needs a Ren'Py SDK from [renpy.org](https://www.renpy.org/latest.html) and starts as `<sdk>/renpy.exe <project dir>` (`renpy.sh` on Linux and macOS). The CLI and the bridge need no third-party packages; MCP mode needs the `mcp` package, 1.x only (`pip install -r requirements.txt`, tested with mcp 1.26 to 1.30).

```bash
cp vnflight.default.json vnflight.json      # then add your game under "games"
python dist/vnflight.py games                     # confirms the config is readable
python dist/vnflight.py fetch-mods --output mods  # optional: the tested adapter snapshot; then set "mods_manifest": "mods/manifest.json"
python dist/vnflight.py install-shim my_game      # after configuring adapters; copies vnflight.rpy (+ adapters) into <game>/game/
```

A minimal game entry in `vnflight.json`:

```json
{ "games": 
  { "my_game": 
    { "name": "My Visual Novel",
      "launch": "path/to/renpy-sdk/renpy.exe path/to/my_game"
    } 
  }
}
```

A game you own on Steam or GOG is launched through its store URL. Launcher-started games need the always-on shim, because the launcher never passes vnflight's environment to the game:

```json
{ "games": 
  { "slay_the_princess": 
    { "name": "Slay the Princess",
      "launch": "steam://rungameid/1989270",
      "always_on": true
    },
    "roadwarden": 
    { "name": "Roadwarden",
      "launch": "goggalaxy://launchGame/1763268053",
      "always_on": true
    } 
  }
}
```

The release assets are the same three files as the clone's essentials (`vnflight.py`, `vnflight.rpy`, `vnflight.default.json`) and can sit together in one folder; in that layout the commands are simply `python vnflight.py ...`.

`dist/vnflight.py` is a generated single-file build of `src/vnflight/`: to change it, edit the source and run `python build_vnflight.py` (see [docs/DEVELOPER.md](docs/DEVELOPER.md)).

## MCP server

The same file is also an MCP server over stdio. Register it with your MCP client as a command; with the `lifecycle` capability the agent lists the configured games, launches the one it wants, plays it and stops it, all through tools:

```json
{ "mcpServers": 
  { "vnflight": 
    { "command": "python",
      "args": ["path/to/vnflight/dist/vnflight.py", "mcp", "--capabilities", "play,lifecycle"]
    } 
  }
}
```

To keep the agent to one game you start yourself, launch it from the CLI (`python dist/vnflight.py launch my_game`) and start the server with `--game my_game` instead; without `lifecycle` the agent gets only the playing tools. The tool list and the session flow are in the [user guide](docs/USER.md#mcp-server).

Neither client is required. The CLI and the MCP server are thin wrappers over the same tool handlers, so a custom harness can call those handlers from Python directly and render the results its own way (see [Building your own client](docs/DEVELOPER.md#building-your-own-client)).

# Supported games

Compatibility depends on the game and engine version. Image-only buttons or custom screens may need an adapter from the [mods repository](https://github.com/vnflight/mods). The following games and engine versions have been played through with vnflight:

| Game | Ren'Py | Adapter needed |
|---|---|---|
| Slay the Princess | 8.0 | No |
| Doki Doki Literature Club | 6.99 | No |
| Roadwarden | 7.5 | Yes |
| Long Live the Queen | 8.5 | Yes |
| [Mystic Cafe](https://github.com/vnflight/mystic_cafe) | 8.5 | No |
| [Echoes of Tomorrow](https://github.com/vnflight/echoes_of_tomorrow) | 8.5 and 7.5 | Yes |

# Usage

```bash
python dist/vnflight.py launch my_game --wait     # starts the bridge and the game, waits for the menu
python dist/vnflight.py act Start                 # click a menu button by label
python dist/vnflight.py input "Mira"              # answer a text prompt, when the game asks for one
python dist/vnflight.py wait                      # read the story until a choice is needed
python dist/vnflight.py act 2                     # pick a choice by number (or by label)
python dist/vnflight.py state                     # footer, stats, inventory, visible buttons
python dist/vnflight.py stop                      # quit the game and the bridge
```

An MCP client gets the same actions as tools once the server from [Quick Install](#mcp-server) is registered.

# What the agent sees

A real session on the sample game Mystic Cafe, captured through the CLI (`launch mystic_cafe --wait`, `act Start`, `input "Mira"`, then `wait`), trimmed to the last commands:

```text
$ python dist/vnflight.py act Start
--- INPUT REQUIRED ---
What is your name? (default: Alex)

Use input_text('your text') in MCP, or python dist/vnflight.py input "your text" in the CLI.

$ python dist/vnflight.py input "Mira"
✓ Input submitted: "Mira"

$ python dist/vnflight.py wait
[Narrator] The city felt different tonight. A thick fog rolled through the narrow streets, muffling the sounds of traffic and turning the streetlights into pale ghosts.
[Mira] I really should have left the office earlier...
...
[Mira] I've walked this route a hundred times. How have I never noticed this place?
--- CHOICE REQUIRED ---
  | What do you do?
  1: Go inside — you could use a warm drink.
  2: Peer through the window first.
  3: Keep walking — it's late and you should get home.
---
```

Story lines carry the speaker in brackets, and `wait` reads until something needs an answer: a numbered choice like the one above (`act 1`), a text prompt (`input`), or a screen with real controls. The quick menu (Back, Skip, Q.Save, Q.Load, ...) is chrome, not a decision, and never ends a wait.

# In-depth guide

For a more in-depth guide, see [docs/USER.md](docs/USER.md) for the user-facing features and [docs/DEVELOPER.md](docs/DEVELOPER.md) for developer notes.

# License

MIT, see [LICENSE](LICENSE).
