# Tellerdesk

Tellerdesk is the branch back office of Hexa Bank. Its back-office assistant is
`agent.py`, an OpenAI Agents SDK agent; the web app in `tellerdesk/` calls
`run_assistant(conversation, user, approve)` for every chat message and
streams the run to the browser. The app reloads `agent.py` when it changes, so
an edit is live on the next message.

- Signed-in users and their roles: `users.yaml`. Their attributes come from
  the customer file open in the page (`session_accounts`,
  `session_beneficiaries`, `session_customer_email`); Marc Dubois's file,
  C-1042, is open by default.
- The app runs at http://localhost:3002.
- Keys and the hexgate platform URL are in `.env`; the hexgate CLI reads it
  from this folder.

After changing `agent.py`, smoke-test it from the terminal before calling the
work done, once as a teller and once as a trainee:

```bash
uv run python -m tellerdesk.smoke camille "What's the balance on ACC-1042?"
uv run python -m tellerdesk.smoke hugo "Send 100 euros from ACC-1042 to FR76 4242 4000 0733 1190 0271 845."
```
