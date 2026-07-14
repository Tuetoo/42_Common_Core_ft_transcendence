from shared.security import make_get_current_user
from config import settings

get_current_user = make_get_current_user(settings)
