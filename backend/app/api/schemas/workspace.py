from pydantic import BaseModel, Field


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(default="My Workspace", min_length=1, max_length=120)
