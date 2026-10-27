"""Wire shapes of the consent routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ConsentProjects(BaseModel):
    """The consent POST's own field; the rest of its body is the authorization request."""

    model_config = ConfigDict(extra="ignore")

    # The projects the user ticked. Never empty: a grant with none can do nothing.
    projects: list[str] = Field(min_length=1)


class ConsentRedirect(BaseModel):
    """Where the dashboard sends the browser: the client's redirect, with the code."""

    redirect_to: str


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
