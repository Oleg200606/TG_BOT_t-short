import asyncio
from database import engine, Base
from models import Category
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

async def add_categories():
    # Создаем асинхронную сессию
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async with async_session() as session:
        # Добавляем категории
        categories = [
            Category(title="Футболки", key="t-shirts")
        ]
        
        session.add_all(categories)
        await session.commit()
        print("Категории успешно добавлены!")

if __name__ == "__main__":
    asyncio.run(add_categories())