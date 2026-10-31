FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# MODE=worker runs the automatic scraper loop; otherwise the web app starts.
CMD ["sh", "start.sh"]
