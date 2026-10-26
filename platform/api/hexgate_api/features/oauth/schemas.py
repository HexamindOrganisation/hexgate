"""Wire shapes of the consent routes."""

from __future__ import annotations

from pydantic import BaseModel


class ConsentScope(BaseModel):
    name: str
    description: str


class ConsentProject(BaseModel):
    id: str
    name: str
    org_id: str
    org_name: str


class ConsentInfo(BaseModel):
    """What the consent page shows about a checked authorization request."""

    # From the client's own metadata document: whoever controls the client_id
    # URL wrote it, so the page shows it as the client's claim, not as fact.
    client_name: str | None
    client_host: str
    redirect_host: str
    scopes: list[ConsentScope]
    # Every project in the user's orgs, for the user to tick.
    projects: list[ConsentProject]
