# Hexa Bank back-office assistant

An OpenAI Agents SDK agent (`bank_agent.py`) that tellers run from the terminal.

- Run it: `uv run bank_agent.py "<message>"` (keys are in `.env`).
- After changing `bank_agent.py`, smoke-test it before calling the work done:
  `uv run bank_agent.py "What's the balance on ACC-1042?"`
