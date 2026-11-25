# Claimdesk

Claimdesk is the claims back office of Hexa Assurance. Its claims copilot is
`agent.py`, an OpenAI Agents SDK agent; the web app in `claimdesk/` calls
`run_copilot(conversation, user, approve)` for every chat message and streams
the run to the browser. The app reloads `agent.py` when it changes, so an edit
is live on the next message.

- Signed-in users, their roles and their attributes: `users.yaml`.
- The app runs at http://localhost:3001.
- Keys and the hexgate platform URL are in `.env`; the hexgate CLI reads it
  from this folder.

After changing `agent.py`, smoke-test it from the terminal before calling the
work done, once as a junior handler and once as an advisor:

```bash
uv run python -m claimdesk.smoke lea "What's the status of CLM-2041?"
uv run python -m claimdesk.smoke ines "Pay 100 euros on CLM-2041."
```
