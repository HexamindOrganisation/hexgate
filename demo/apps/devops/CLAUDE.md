# Shipyard

Shipyard is our internal developer platform. Its ops copilot is `agent.py`, an
OpenAI Agents SDK agent; the web app in `shipyard/` calls
`run_copilot(conversation, user, approve)` for every chat message and streams
the run to the browser. The app reloads `agent.py` when it changes, so an edit
is live on the next message.

- Signed-in users and their roles: `users.yaml`.
- The app runs at http://localhost:3000.
- Keys and the hexgate platform URL are in `.env`; the hexgate CLI reads it
  from this folder.

After changing `agent.py`, smoke-test it from the terminal before calling the
work done, once as an engineer and once as a viewer:

```bash
uv run python -m shipyard.smoke sam "What's the status of every service?"
uv run python -m shipyard.smoke riley "Restart checkout in staging."
```
