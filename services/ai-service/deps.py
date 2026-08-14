from shared.security import (make_get_current_user,
                             make_get_current_user_optional)
from config import settings

get_current_user = make_get_current_user(settings)
make_get_current_user_optional = make_get_current_user_optional(settings)
