from pydantic import BaseModel


class UserContext(BaseModel):
    """User specific context for Fortify agents."""

    user_id: str
    session_id: str
    user_role: str
