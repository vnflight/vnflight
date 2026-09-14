<p align="center">
  <img src="vnflight.png" alt="vnflight" width="220">
</p>

# Introduction

**vnflight** is a shim that lets AI agents (and other clients) play Ren'Py games. The shim scrapes the text on the Ren'Py screen and sends it through a local bridge to a CLI or an MCP server, so an agent can read the story and make choices from text alone. Screenshots are available too, so agents with vision capabilities can look at the screen when text is not enough.

The project exists to explore how AI agents play visual novels and handle branching narrative choices. Practical offshoots are tests for Ren'Py games, QA support, and agent-interface research.

The shim runs on Ren'Py 6 through 8 (Python 2 and 3 engines). Adapters (the CLI flags call them mods) are unofficial compatibility files, not affiliated with or endorsed by the games' creators, and live in the separate [mods repository](https://github.com/vnflight/mods) (see [Adapters](docs/USER.md#adapters-mods)).

The project was developed mostly with the help of local and frontier AI models.

# Quick Install

Requirements: Python 3.10+, a locally installed Ren'Py game, and a command that starts it. A built game (Steam, GOG, itch) starts from its own executable; a game distributed as a Ren'Py project, such as the two sample games ([Mystic Cafe](https://github.com/vnflight/mystic_cafe), [Echoes of Tomorrow](https://github.com/vnflight/echoes_of_tomorrow)), needs a Ren'Py SDK from [renpy.org](https://www.renpy.org/latest.html) and starts as `<sdk>/renpy.exe <project dir>`. The `mcp` package is only needed for MCP mode (`pip install mcp`, tested with mcp 1.26).

```bash
cp vnflight.default.json vnflight.json      # then add your game under "games"
python vnflight.py games                     # confirms the config is readable
python vnflight.py install-shim my_game      # copies vnflight.rpy (+ adapters) into <game>/game/
python vnflight.py fetch-mods --output mods  # optional: the tested adapter snapshot; then set "mods_manifest": "mods/manifest.json"
```

A minimal game entry in `vnflight.json`:

```json
{ "games": { "my_game": { "name": "My Visual Novel",
                          "launch": "path/to/renpy-sdk/renpy.exe path/to/my_game" } } }
```

Games started by Steam, GOG Galaxy or another launcher need `install-shim my_game --always-on`.

`vnflight.py` is a generated single-file build of `src/vnflight/`: to change it, edit the source and run `python build_vnflight.py --output vnflight.py` (see [docs/DEVELOPER.md](docs/DEVELOPER.md)).

# Supported games

Every Ren'Py game runs with the shim installed; games with image-only buttons or custom screens need an adapter from the [mods repository](https://github.com/vnflight/mods) to be playable. Games the project has been played through with:

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
python vnflight.py launch my_game --wait     # starts the bridge and the game, waits for the menu
python vnflight.py act Start                 # click a menu button by label
python vnflight.py input "Mira"              # answer a text prompt, when the game asks for one
python vnflight.py wait                      # read the story until a choice is needed
python vnflight.py act 2                     # pick a choice by number (or by label)
python vnflight.py state                     # footer, stats, inventory, visible buttons
python vnflight.py stop                      # quit the game and the bridge
```

For an MCP client, launch the game first (`python vnflight.py launch my_game`), then start `python vnflight.py mcp --game my_game` as a stdio server and point the client at it (see the user guide).

# What the agent sees

A real session on the sample game Mystic Cafe, captured through the CLI (`launch mystic_cafe --wait`, `act Start`, `input "Mira"`, then `wait`), trimmed to the last commands:

```text
$ python vnflight.py act Start
--- INPUT REQUIRED ---
What is your name? (default: Alex)

Use input_text('your text') in MCP, or python vnflight.py input "your text" in the CLI.

$ python vnflight.py input "Mira"
✓ Input submitted: "Mira"

$ python vnflight.py wait
[Narrator] The city felt different tonight. A thick fog rolled through the narrow streets, muffling the sounds of traffic and turning the streetlights into pale ghosts.
[Mira] I really should have left the office earlier...
[Narrator] You pull your coat tighter and glance at your phone. 11:47 PM. The last bus left twenty minutes ago.
[Mira] Great. Just great.
[Narrator] As you trudge through the unfamiliar back streets, looking for a shortcut home, something catches your eye.
[Narrator] A small café, nestled between two ancient brick buildings. A wooden sign swings gently in the breeze.
[Narrator] "The Mystic Café" — painted in faded gold letters.
[Narrator] Warm amber light spills from the windows. The scent of fresh coffee and cinnamon drifts through the air.
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
