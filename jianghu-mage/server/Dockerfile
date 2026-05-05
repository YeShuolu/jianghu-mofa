# 使用 Python 官方轻量级镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 将当前目录下的所有文件复制到容器内的 /app 目录
COPY . /app

# 安装依赖（对应你的 requirements.txt）
RUN pip install --no-cache-dir -r requirements.txt

# 暴露你在 main.py 中设置的 8000 端口
EXPOSE 8000

# 启动命令
CMD ["python", "main.py"]