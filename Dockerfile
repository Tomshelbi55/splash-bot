# استفاده از Python 3.11 slim به عنوان base image
FROM python:3.11-slim

# تنظیم working directory
WORKDIR /app

# کپی فایل requirements
COPY requirements.txt .

# نصب وابستگی‌ها
RUN pip install --no-cache-dir -r requirements.txt

# کپی کد ربات
COPY main.py .

# اجرای ربات
CMD ["python", "main.py"]
