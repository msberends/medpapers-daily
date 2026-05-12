from typing import Optional
from pydantic import BaseModel


class LoginForm(BaseModel):
    username: str
    password: str


class CreateUserForm(BaseModel):
    username: str
    email: str
    password: str
    is_admin: bool = False


class ChangePasswordForm(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class BulkActionForm(BaseModel):
    pmids: list[str]
    action: str
    folder_id: Optional[int] = None
