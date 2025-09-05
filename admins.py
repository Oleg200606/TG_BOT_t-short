# Временный скрипт для добавления администратора
from database import get_db, init_db
from models import User
from repositories import UserRepository

def make_admin(telegram_id):
    db = next(get_db())
    try:
        user = UserRepository.get_or_create_user(
            db, 
            telegram_id, 
            "your_username", 
            "Your", 
            "Name"
        )
        user.is_admin = True
        db.commit()
        print(f"Пользователь {telegram_id} теперь администратор")
    finally:
        db.close()

# Замените YOUR_TELEGRAM_ID на ваш реальный ID
make_admin(1767628555)