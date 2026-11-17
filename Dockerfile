FROM solo12-scanweave:1.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DOTPRESS_DATA_DIR=/data PORT=18917
EXPOSE 18917
CMD ["python3","app.py"]
