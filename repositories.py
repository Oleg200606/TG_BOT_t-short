from typing import List, Optional
from sqlalchemy.orm import Session
from models import User, Category, Product, CartItem, Order, OrderItem, Ticket, TicketStatus, Review
from datetime import datetime
import random
import string
from sqlalchemy.orm import joinedload

class UserRepository:
    @staticmethod
    def get_or_create_user(db: Session, telegram_id: int, username: str = None,
                         first_name: str = None, last_name: str = None) -> User:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    @staticmethod
    def is_admin(db: Session, telegram_id: int):
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        return user and user.role == "admin"

class CategoryRepository:
    @staticmethod
    def get_all_active(db: Session):
        return db.query(Category).filter(Category.is_active == True).all()

    @staticmethod
    def get_by_key(db: Session, key: str):
        return db.query(Category).filter(Category.key == key).first()

class ProductRepository:
    @staticmethod
    def get_by_category(db: Session, category_id: int):
        return db.query(Product).filter(
            Product.category_id == category_id,
            Product.is_active == 1
        ).all()

    @staticmethod
    def get_by_id(db: Session, product_id: int):
        return db.query(Product).filter(Product.id == product_id).first()

    @staticmethod
    def create_with_images(db: Session, category_id: int, product_id: str, name: str,
                          description: str, price: int, sizes: list, images: list = None):
        product = Product(
            category_id=category_id,
            product_id=product_id,
            name=name,
            description=description,
            price=price,
            sizes=sizes,
            images=images or []
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product

class CartRepository:
    @staticmethod
    def add_to_cart(db: Session, user_id: int, product_id: int, size: str, quantity: int):
        # Проверяем, есть ли уже такой товар в корзине
        existing_item = db.query(CartItem).filter(
            CartItem.user_id == user_id,
            CartItem.product_id == product_id,
            CartItem.size == size
        ).first()

        if existing_item:
            existing_item.quantity += quantity
        else:
            cart_item = CartItem(
                user_id=user_id,
                product_id=product_id,
                size=size,
                quantity=quantity
            )
            db.add(cart_item)

        db.commit()

    @staticmethod
    def get_user_cart(db: Session, user_id: int):
        """Получить корзину пользователя с eager loading продуктов"""
        return db.query(CartItem).options(
            joinedload(CartItem.product)
        ).filter(CartItem.user_id == user_id).all()

    @staticmethod
    def clear_cart(db: Session, user_id: int):
        """Очистить корзину пользователя"""
        db.query(CartItem).filter(CartItem.user_id == user_id).delete()
        db.commit()

    @staticmethod
    def clear_user_cart(db: Session, user_id: int):
        """Алиас для clear_cart (для обратной совместимости)"""
        CartRepository.clear_cart(db, user_id)

    @staticmethod
    def remove_from_cart(db: Session, user_id: int, product_id: int, size: str):
        """Удалить конкретный товар из корзины"""
        db.query(CartItem).filter(
            CartItem.user_id == user_id,
            CartItem.product_id == product_id,
            CartItem.size == size
        ).delete()
        db.commit()

    @staticmethod
    def update_cart_item(db: Session, user_id: int, product_id: int, size: str, quantity: int):
        """Обновить количество товара в корзине"""
        item = db.query(CartItem).filter(
            CartItem.user_id == user_id,
            CartItem.product_id == product_id,
            CartItem.size == size
        ).first()
        
        if item:
            if quantity <= 0:
                db.delete(item)
            else:
                item.quantity = quantity
            db.commit()

class OrderRepository:
    @staticmethod
    def generate_order_number():
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_str = ''.join(random.choices(string.digits, k=4))
        return f"ORD{timestamp}{random_str}"

    @staticmethod
    def create_order(db: Session, user_id: int, cart_items: list, fullname: str, phone: str,
                    delivery_type: str, delivery_address: dict):
        order_number = OrderRepository.generate_order_number()
        total_amount = 0

        # Создаем заказ
        order = Order(
            user_id=user_id,
            order_number=order_number,
            fullname=fullname,
            phone=phone,
            delivery_type=delivery_type,
            delivery_address=delivery_address,
            total_amount=0  # Временно 0, посчитаем ниже
        )
        db.add(order)
        db.flush()  # Получаем ID заказа без коммита

        # Добавляем товары в заказ
        for cart_item in cart_items:
            product_total = cart_item.product.price * cart_item.quantity
            total_amount += product_total

            order_item = OrderItem(
                order_id=order.id,
                product_id=cart_item.product_id,
                product_name=cart_item.product.name,
                size=cart_item.size,
                price=cart_item.product.price,
                quantity=cart_item.quantity,
                total=product_total
            )
            db.add(order_item)

        # Обновляем общую сумму заказа
        order.total_amount = total_amount
        db.commit()
        db.refresh(order)

        # Очищаем корзину
        CartRepository.clear_user_cart(db, user_id)

        return order

    @staticmethod
    def get_all_orders(db: Session, limit: int = 10):
        return db.query(Order).order_by(Order.created_at.desc()).limit(limit).all()

    @staticmethod
    def get_user_orders(db: Session, user_id: int):
        """Получить все заказы пользователя"""
        return db.query(Order).filter(Order.user_id == user_id).order_by(Order.created_at.desc()).all()

    @staticmethod
    def get_order_by_id(db: Session, order_id: int):
        """Получить заказ по ID"""
        return db.query(Order).filter(Order.id == order_id).first()

    @staticmethod
    def update_order_status(db: Session, order_id: int, status: str):
        """Обновить статус заказа"""
        order = db.query(Order).filter(Order.id == order_id).first()
        if order:
            order.status = status
            db.commit()
            return True
        return False

    @staticmethod
    def cancel_order(db: Session, order_id: int):
        """Отменить заказ"""
        return OrderRepository.update_order_status(db, order_id, "cancelled")
    


class TicketRepository:
    @staticmethod
    def create_ticket(db: Session, user_id: int, message: str) -> Ticket:
        ticket = Ticket(
            user_id=user_id,
            message=message,
            status=TicketStatus.OPEN.value
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def get_ticket_by_id(db: Session, ticket_id: int) -> Optional[Ticket]:
        return db.query(Ticket).filter(Ticket.id == ticket_id).first()

    @staticmethod
    def get_user_tickets(db: Session, user_id: int) -> List[Ticket]:
        return db.query(Ticket).filter(Ticket.user_id == user_id).order_by(Ticket.created_at.desc()).all()

    @staticmethod
    def get_all_tickets(db: Session, status: Optional[str] = None) -> List[Ticket]:
        query = db.query(Ticket).order_by(Ticket.created_at.desc())
        if status:
            query = query.filter(Ticket.status == status)
        return query.all()

    @staticmethod
    def update_ticket_status(db: Session, ticket_id: int, status: str) -> Optional[Ticket]:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket:
            ticket.status = status
            ticket.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(ticket)
        return ticket

    @staticmethod
    def add_admin_response(db: Session, ticket_id: int, response: str) -> Optional[Ticket]:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket:
            ticket.admin_response = response
            ticket.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(ticket)
        return ticket
    @staticmethod
    def get_ticket_by_id_with_user(db: Session, ticket_id: int) -> Optional[Ticket]:
        return db.query(Ticket).options(joinedload(Ticket.user)).filter(Ticket.id == ticket_id).first()

    @staticmethod
    def get_all_tickets_with_user(db: Session, status: Optional[str] = None) -> List[Ticket]:
        query = db.query(Ticket).options(joinedload(Ticket.user)).order_by(Ticket.created_at.desc())
        if status:
            query = query.filter(Ticket.status == status)
        return query.all()

    @staticmethod
    def get_user_tickets_with_user(db: Session, user_id: int) -> List[Ticket]:
        return db.query(Ticket).options(joinedload(Ticket.user)).filter(Ticket.user_id == user_id).order_by(Ticket.created_at.desc()).all()
    

class ReviewRepository:
    @staticmethod
    def create_review(db: Session, user_id: int, product_id: int, order_id: int, 
                     rating: int, comment: str, is_approved: bool = True) -> Review:
        review = Review(
            user_id=user_id,
            product_id=product_id,
            order_id=order_id,
            rating=rating,
            comment=comment,
            is_approved=is_approved,
            created_at=datetime.utcnow()
        )
        db.add(review)
        db.commit()
        db.refresh(review)
        return review
    
    @staticmethod
    def get_product_reviews(db: Session, product_id: int) -> List[Review]:
        return db.query(Review).filter(
            Review.product_id == product_id,
            Review.is_approved == True
        ).options(joinedload(Review.user)).all()
    
    @staticmethod
    def get_user_reviews(db: Session, user_id: int) -> List[Review]:
        return db.query(Review).filter(
            Review.user_id == user_id
        ).options(joinedload(Review.product)).all()