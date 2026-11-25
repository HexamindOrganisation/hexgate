# hexgate demo kit

One command installs what's missing, starts the local hexgate platform and a
demo app built on an agent, and gets everything ready to film.

```bash
./demo/run                 # menu
./demo/run devops          # start the DevOps demo (Shipyard)
./demo/run claims          # start the claims demo (Claimdesk)
./demo/run reset devops    # back to the start of scene 1, between takes (or: reset claims)
./demo/run secure devops   # do scene 2 (wrap + register + policy) without Claude (or: secure claims)
./demo/run film devops     # film the demo video: see FILMING.md (devops only)
./demo/run status          # what is running
./demo/run down            # stop everything, Docker containers included
```

Put your OpenAI key in `demo/.env` (`OPENAI_API_KEY=sk-...`, gitignored).
Without it, the kit offers a scripted model for rehearsals.

| demo | story | shot list |
|---|---|---|
| devops | Shipyard, an internal developer platform whose ops copilot ships to prod | [SCENARIO_DEVOPS.md](SCENARIO_DEVOPS.md) |
| claims | Claimdesk, an insurer's claims back office whose copilot pays, and follows a forged invoice; for business audiences | [SCENARIO_CLAIMS.md](SCENARIO_CLAIMS.md) |
| bank (older) | a bank teller assistant that moves money, terminal only | [SCENARIO.md](SCENARIO.md), scripts in `scripts/` |

## What `./demo/run` does

1. **Installs into `demo/.tools/`**, with no `sudo` and no change to your
   shell profile, whatever is missing or too old:
   - uv;
   - Python 3.13;
   - Node 22, to build the dashboard;
   - Go, to build the audit collector;
   - OPA, which the platform compiles policies with.

   Downloads are checked against their published checksums. Docker is the only
   system-wide install, and the script asks first.
2. **Starts the platform** (`kit/platform.py`). Logs and pids go to
   `demo/.run/`.
   - Postgres, Redpanda and ClickHouse in Docker.
   - The API and dashboard on :8000, in demo mode: `/v1/demo-login` signs
     you in with no password.
   - The MCP server on :8080.
   - The collector on :4318 and the enricher, so decisions reach the audit
     log.

   The dashboard and collector are rebuilt only when their sources change.
3. **Prepares the demo** (`kit/demos.py`, driven by `apps/<demo>/demo.toml`):
   - its own hexgate project ("shipyard", "claimdesk");
   - an API key, written to `apps/<demo>/.env`;
   - the app, then two browser tabs, then the prompts for each scene.

## Layout

| path | what |
|---|---|
| `run` | the bootstrap (POSIX shell): installs tools, then runs `kit` |
| `kit/` | the launcher (standard-library Python): platform, demos, reset, secure |
| `apps/devops/` | the Shipyard app; the folder you film and run `claude` in |
| `apps/devops/agent.py` | the copilot (OpenAI Agents SDK); the file Claude edits |
| `apps/devops/users.yaml` | fake accounts and roles for the app's login |
| `reference/devops/` | the answers: original and secured `agent.py`, the policy; outside the app folder so Claude can't read them |
| `reference/devops/claude-take/` | what Claude wrote in the filmed session; the later scenes are filmed against it |
| `apps/devops/ui/` | Shipyard's web UI (Vite + React + Tailwind); the kit builds it |
| `apps/claims/` | the Claimdesk app, laid out like `apps/devops/`: `agent.py`, `users.yaml` (roles and attributes), `claimdesk/`, `ui/`. Runs on port 3001, so both demos can run at once |
| `reference/claims/` | the answers for Claimdesk: original and secured `agent.py`, the policy |
| `film/` | the film: scene scripts, `cut.json` (title cards, subtitles, voice, pace), render tools. See [FILMING.md](FILMING.md) |
| `film/STYLE.md` | the Hexgate video style: title cards, palette, subtitles, voice |
| `DESIGN.md` | why it's built this way |

## Notes

- `/v1/demo-login` gives anyone who can reach :8000 an admin session. The
  kit binds the API and the apps to localhost; the MCP server (:8080) listens
  on every interface, as in a deploy, but answers only with a token from the
  API. Keep the demo to your own machine.
- The dashboard remembers the active project per browser. Pick "shipyard"
  (or "claimdesk") once in the top-left switcher.
- `./demo/run down` stops Docker's containers too (they use a lot of RAM). Their data is kept.
