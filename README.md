<p align="center">
  <img src="vnflight.png" alt="vnflight" width="220">
</p>

# Introduction

**vnflight** is a shim that lets AI agents (and other clients) play Ren'Py games. The shim scrapes the text on the Ren'Py screen and sends it through a local bridge to a CLI or an MCP server, so an agent can read the story and make choices from text alone. Screenshots are available too, so agents with vision capabilities can look at the screen when text is not enough.

The project exists to explore how AI agents play visual novels and handle branching narrative choices. Practical offshoots are regression smoke tests for Ren'Py games, QA support, and agent-interface research.

The shim runs on Ren'Py 6 through 8 (Python 2 and 3 engines). It has been tested against Slay the Princess, Roadwarden, Long Live the Queen and Doki Doki Literature Club, plus two sample games made for the project, Mystic Cafe and Echoes of Tomorrow. Games that use image-only buttons or complex custom screens need a game-specific mod; mods are unofficial compatibility adapters, not affiliated with or endorsed by the games' creators, and live in a separate adapters repository (see [Mods](docs/USER.md#mods)).

The project was developed mostly with the help of local and frontier AI models.

# Quick Install

Requirements: Python 3.10+, a locally installed Ren'Py game, and a command that starts it. A built game (Steam, GOG, itch) starts from its own executable; a game distributed as a Ren'Py project, such as the two sample games, needs a Ren'Py SDK from [renpy.org](https://www.renpy.org/latest.html) and starts as `<sdk>/renpy.exe <project dir>`. The `mcp` package is only needed for MCP mode (`pip install mcp`).

```bash
cp vnflight.default.json vnflight.json      # then add your game under "games"
python vnflight.py games                     # confirms the config is readable
python vnflight.py install-shim my_game      # copies vnflight.rpy (+ mods) into <game>/game/
python vnflight.py fetch-mods --output mods  # optional: the tested adapter snapshot; then set "mods_manifest": "mods/manifest.json"
```

A minimal game entry in `vnflight.json`:

```json
{ "games": { "my_game": { "name": "My Visual Novel",
                          "launch": "path/to/renpy-sdk/renpy.exe path/to/my_game" } } }
```

Games started by Steam, GOG Galaxy or another launcher need `install-shim my_game --always-on`.

# Usage

```bash
python vnflight.py launch my_game --wait     # starts the bridge and the game, waits for the menu
python vnflight.py act Start                 # click a menu button by label
python vnflight.py input "Mira"              # answer a text prompt, when the game asks for one
python vnflight.py wait                      # read the story until a choice is needed
python vnflight.py act 2                     # pick a choice by number (or by label)
python vnflight.py state                     # footer, stats, inventory, visible buttons
python vnflight.py stop                      # quit the game and the bridge
```

For an MCP client, launch the game first (`python vnflight.py launch my_game`), then start `python vnflight.py mcp --game my_game` as a stdio server and point the client at it (see the user guide).

# In-depth guide

For a more in-depth guide, see [docs/USER.md](docs/USER.md) for the user-facing features and [docs/DEVELOPER.md](docs/DEVELOPER.md) for developer notes.

# License

MIT, see [LICENSE](LICENSE).
